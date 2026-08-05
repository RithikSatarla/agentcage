"""The Jira model's rules.

Each test names a behaviour the real API has and an agent can get wrong. The point of
the model is that these hold across a sequence of calls, which is exactly what a
recorded response cannot do.
"""

from __future__ import annotations

import pytest

from part_b.jira_mock import JiraError, JiraMock

FIELDS = {"project": {"key": "KAN"}, "summary": "Login returns 500",
          "description": "on submit", "issuetype": {"name": "Bug"}}


@pytest.fixture
def jira():
    return JiraMock(projects=["KAN"], clock=lambda: 1_700_000_000)


# --- creating ----------------------------------------------------------------

def test_create_returns_a_key_in_the_project(jira):
    created = jira.create_issue(FIELDS)
    assert created["key"] == "KAN-1"
    assert created["self"].endswith(f"/rest/api/2/issue/{created['id']}")


def test_creating_the_same_issue_twice_makes_two_issues(jira):
    """There is no idempotency key on this endpoint. The duplicate is real."""
    first = jira.create_issue(FIELDS)
    second = jira.create_issue(FIELDS)
    assert first["key"] != second["key"]
    assert len(jira.issues) == 2


def test_create_into_an_unknown_project_is_rejected(jira):
    with pytest.raises(JiraError) as exc:
        jira.create_issue({**FIELDS, "project": {"key": "NOPE"}})
    assert exc.value.http_status == 400
    assert "project" in exc.value.errors


def test_create_without_a_summary_is_rejected(jira):
    with pytest.raises(JiraError) as exc:
        jira.create_issue({**FIELDS, "summary": "   "})
    assert exc.value.http_status == 400
    assert "summary" in exc.value.errors


def test_a_project_can_be_named_by_id_as_well_as_key(jira):
    """python-jira resolves the project and posts back its numeric id."""
    pid = jira.projects["KAN"]["id"]
    created = jira.create_issue({**FIELDS, "project": {"id": pid}})
    assert created["key"] == "KAN-1"


# --- reading and deleting ----------------------------------------------------

def test_an_issue_is_reachable_by_key_or_by_id(jira):
    created = jira.create_issue(FIELDS)
    assert jira.get_issue(created["key"])["key"] == created["key"]
    assert jira.get_issue(created["id"])["key"] == created["key"]


def test_a_deleted_issue_is_gone(jira):
    created = jira.create_issue(FIELDS)
    jira.delete_issue(created["key"])
    with pytest.raises(JiraError) as exc:
        jira.get_issue(created["key"])
    assert exc.value.http_status == 404


def test_deleting_twice_is_a_404_not_a_second_delete(jira):
    created = jira.create_issue(FIELDS)
    jira.delete_issue(created["key"])
    with pytest.raises(JiraError) as exc:
        jira.delete_issue(created["key"])
    assert exc.value.http_status == 404


def test_updating_a_deleted_issue_is_a_404(jira):
    created = jira.create_issue(FIELDS)
    jira.delete_issue(created["key"])
    with pytest.raises(JiraError) as exc:
        jira.update_issue(created["key"], {"summary": "still here?"})
    assert exc.value.http_status == 404


def test_an_invalid_due_date_is_rejected(jira):
    created = jira.create_issue(FIELDS)
    with pytest.raises(JiraError) as exc:
        jira.update_issue(created["key"], {"duedate": "05/08/2026"})
    assert exc.value.http_status == 400


# --- comments and worklogs ---------------------------------------------------

def test_an_empty_comment_is_rejected(jira):
    created = jira.create_issue(FIELDS)
    with pytest.raises(JiraError) as exc:
        jira.add_comment(created["key"], "   ")
    assert exc.value.http_status == 400


def test_commenting_on_a_missing_issue_is_a_404(jira):
    with pytest.raises(JiraError) as exc:
        jira.add_comment("KAN-999", "any progress?")
    assert exc.value.http_status == 404


def test_a_worklog_duration_must_be_a_duration(jira):
    created = jira.create_issue(FIELDS)
    with pytest.raises(JiraError) as exc:
        jira.add_worklog(created["key"], "quite a while")
    assert exc.value.http_status == 400
    assert jira.add_worklog(created["key"], "2h")["timeSpent"] == "2h"


# --- transitions -------------------------------------------------------------

def test_available_transitions_change_after_a_write(jira):
    """The response to this endpoint is only correct until the issue moves."""
    created = jira.create_issue(FIELDS)
    key = created["key"]
    before = {t["name"] for t in jira.transitions(key)}
    assert before == {"In Progress", "Done"}

    done = next(t["id"] for t in jira.transitions(key) if t["name"] == "Done")
    jira.transition_issue(key, done)

    after = {t["name"] for t in jira.transitions(key)}
    assert after == {"To Do"}
    assert after != before


def test_a_transition_not_available_from_here_is_rejected(jira):
    created = jira.create_issue(FIELDS)
    key = created["key"]
    done = next(t["id"] for t in jira.transitions(key) if t["name"] == "Done")
    jira.transition_issue(key, done)
    with pytest.raises(JiraError) as exc:
        jira.transition_issue(key, done)     # no longer offered from "Done"
    assert exc.value.http_status == 400


# --- search ------------------------------------------------------------------

def test_search_filters_on_project_and_status(jira):
    jira.create_issue(FIELDS)
    second = jira.create_issue({**FIELDS, "summary": "Checkout is wrong"})
    done = next(t["id"] for t in jira.transitions(second["key"]) if t["name"] == "Done")
    jira.transition_issue(second["key"], done)

    assert len(jira.search("project = KAN")) == 2
    assert [i["key"] for i in jira.search('status = "Done"')] == [second["key"]]
    assert jira.search("project = OTHER") == []


# --- the HTTP surface --------------------------------------------------------

def test_writes_that_return_no_content_say_so(jira):
    created = jira.create_issue(FIELDS)
    key = created["key"]
    status, body = jira.handle("PUT", f"/rest/api/2/issue/{key}",
                               {"fields": {"summary": "renamed"}})
    assert (status, body) == (204, None)
    status, body = jira.handle("DELETE", f"/rest/api/2/issue/{key}")
    assert (status, body) == (204, None)


def test_handle_never_raises_for_a_rejected_request(jira):
    status, body = jira.handle("POST", "/rest/api/2/issue",
                               {"fields": {"project": {"key": "NOPE"},
                                           "summary": "x"}})
    assert status == 400
    assert "project" in body["errors"]


def test_an_error_body_always_carries_both_keys(jira):
    """Clients read whichever of errorMessages/errors they were written against."""
    _, body = jira.handle("GET", "/rest/api/2/issue/KAN-404")
    assert "errorMessages" in body and "errors" in body


def test_an_unknown_route_is_a_404_not_a_crash(jira):
    assert jira.handle("GET", "/rest/api/2/nonsense")[0] == 404
    assert jira.handle("POST", "/totally/elsewhere")[0] == 404


def test_the_query_string_reaches_the_search_endpoint(jira):
    jira.create_issue(FIELDS)
    status, body = jira.handle("GET", "/rest/api/2/search?jql=project%20%3D%20KAN")
    assert status == 200
    assert body["total"] == 1
