"""State-mutation tests for the GitHub model.

These mirror test_stripe_mock.py: the point is that state is real, and that the
version-control analogues of a double refund are refused.
"""

import pytest

from part_b.github_mock import GitHubError, GitHubMock, Issue, PullRequest


@pytest.fixture
def gh():
    return GitHubMock(clock=lambda: 1_750_000_000)


# --- the defect a fixture cannot show: retry makes two ------------------------


def test_retrying_issue_creation_makes_two_issues(gh):
    """GitHub has no idempotency key, so a retry genuinely duplicates."""
    first = gh.post_issue("acme/webapp", title="Flaky test in CI")
    second = gh.post_issue("acme/webapp", title="Flaky test in CI")

    assert isinstance(first, Issue)
    assert first.number != second.number
    assert len(gh.list_issues("acme/webapp")) == 2


def test_merging_twice_is_refused(gh):
    pr = gh.post_pull("acme/webapp", title="Add refunds", head="fix/refunds")
    assert gh.put_merge("acme/webapp", pr.number)["merged"] is True

    with pytest.raises(GitHubError) as excinfo:
        gh.put_merge("acme/webapp", pr.number)

    assert excinfo.value.http_status == 405
    assert gh.get_pull("acme/webapp", pr.number).merged is True


def test_stale_sha_update_is_refused(gh):
    """The stale-read defect: an agent that cached a sha before its own write."""
    created = gh.put_contents("acme/webapp", "src/app.py", "v1", "add app")
    stale_sha = created.sha

    gh.put_contents("acme/webapp", "src/app.py", "v2", "update", sha=stale_sha)

    with pytest.raises(GitHubError) as excinfo:
        gh.put_contents("acme/webapp", "src/app.py", "v3", "update", sha=stale_sha)

    assert excinfo.value.http_status == 409
    assert "does not match" in excinfo.value.message
    assert gh.get_contents("acme/webapp", "src/app.py").content == "v2"


# --- reads observe writes -----------------------------------------------------


def test_comment_updates_the_issue_and_the_read_sees_it(gh):
    issue = gh.post_issue("acme/webapp", title="Bug")
    assert gh.get_issue("acme/webapp", issue.number).comments == 0

    gh.post_issue_comment("acme/webapp", issue.number, body="on it")

    assert gh.get_issue("acme/webapp", issue.number).comments == 1


def test_closing_an_issue_is_visible_to_later_reads(gh):
    issue = gh.post_issue("acme/webapp", title="Bug")
    gh.patch_issue("acme/webapp", issue.number, state="closed")

    assert gh.get_issue("acme/webapp", issue.number).state == "closed"
    assert gh.list_issues("acme/webapp", state="open") == []
    assert len(gh.list_issues("acme/webapp", state="all")) == 1


def test_delete_then_read_is_404(gh):
    f = gh.put_contents("acme/webapp", "old.py", "x", "add")
    gh.delete_contents("acme/webapp", "old.py", "remove", sha=f.sha)

    with pytest.raises(GitHubError) as excinfo:
        gh.get_contents("acme/webapp", "old.py")
    assert excinfo.value.http_status == 404


# --- validation ---------------------------------------------------------------


def test_creating_a_file_that_exists_without_sha_is_refused(gh):
    gh.put_contents("acme/webapp", "a.py", "v1", "add")
    with pytest.raises(GitHubError) as excinfo:
        gh.put_contents("acme/webapp", "a.py", "v2", "add again")
    assert excinfo.value.http_status == 422
    assert gh.get_contents("acme/webapp", "a.py").content == "v1"


def test_delete_with_wrong_sha_leaves_the_file(gh):
    gh.put_contents("acme/webapp", "a.py", "v1", "add")
    with pytest.raises(GitHubError) as excinfo:
        gh.delete_contents("acme/webapp", "a.py", "remove", sha="deadbeef")
    assert excinfo.value.http_status == 409
    assert gh.get_contents("acme/webapp", "a.py").content == "v1"


def test_empty_title_is_422(gh):
    with pytest.raises(GitHubError) as excinfo:
        gh.post_issue("acme/webapp", title="   ")
    assert excinfo.value.http_status == 422
    assert excinfo.value.errors[0]["field"] == "title"


def test_pull_from_base_to_itself_is_422(gh):
    with pytest.raises(GitHubError) as excinfo:
        gh.post_pull("acme/webapp", title="noop", head="main", base="main")
    assert excinfo.value.http_status == 422


def test_unknown_repo_is_404(gh):
    with pytest.raises(GitHubError) as excinfo:
        gh.post_issue("nobody/nothing", title="hi")
    assert excinfo.value.http_status == 404


def test_issue_and_pull_numbers_share_one_sequence(gh):
    """GitHub numbers issues and pull requests from the same counter."""
    issue = gh.post_issue("acme/webapp", title="Bug")
    pr = gh.post_pull("acme/webapp", title="Fix", head="fix")
    assert pr.number == issue.number + 1
    assert isinstance(pr, PullRequest)


# --- HTTP surface -------------------------------------------------------------


def test_handle_routes_create_comment_and_read(gh):
    status, issue = gh.handle("POST", "/repos/acme/webapp/issues", {"title": "Bug"})
    assert status == 201

    n = issue["number"]
    status, _ = gh.handle("POST", f"/repos/acme/webapp/issues/{n}/comments", {"body": "hi"})
    assert status == 201

    status, read = gh.handle("GET", f"/repos/acme/webapp/issues/{n}")
    assert status == 200 and read["comments"] == 1


def test_handle_returns_the_error_envelope_not_an_exception(gh):
    status, pr = gh.handle("POST", "/repos/acme/webapp/pulls",
                           {"title": "x", "head": "f", "base": "main"})
    gh.handle("PUT", f"/repos/acme/webapp/pulls/{pr['number']}/merge")

    status, body = gh.handle("PUT", f"/repos/acme/webapp/pulls/{pr['number']}/merge")

    assert status == 405
    assert body["message"] == "Pull Request is not mergeable"


def test_handle_unknown_route_is_404(gh):
    status, body = gh.handle("GET", "/repos/acme/webapp/widgets")
    assert status == 404
    assert body["message"] == "Not Found"
