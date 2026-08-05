"""What the real APIs are documented to do.

§3A.4 forbids grading a request T1 or T2 without "a documented expectation of the real
API's behaviour". This module is that expectation, written down separately from the
grader so it can be checked against the vendors' documentation without reading any
result.

Each entry names an endpoint, the statuses its documentation defines, and the best tier
a match can earn:

* ``T1`` — the model returns the same status *and* the fields the client's control flow
  reads. Claimed only where every field the tools in this study actually touch is
  modelled.
* ``T2`` — status and decision-relevant semantics match, but the model omits incidental
  fields the real API sends (avatar URLs, ``expand`` blocks, pagination links). No tool
  in the sample branches on them; they are still absent, so the match is not exact.

A route that is not listed here cannot be graded better than T3, and a request the model
could not route at all is a MISS. Adding a route to this table is a claim about the
vendor's documentation, not about the model.

References are to the published API documentation:

* GitHub REST API — https://docs.github.com/en/rest
* Jira Cloud platform REST API v2 —
  https://developer.atlassian.com/cloud/jira/platform/rest/v2/
"""

from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Tuple

__all__ = ["Expectation", "normalise", "lookup", "EXPECTATIONS"]


class Expectation(NamedTuple):
    api: str
    method: str
    route: str
    statuses: Dict[int, str]     # documented status -> what it means
    best_tier: str               # "T1" or "T2"
    reference: str


# --- route normalisation -----------------------------------------------------

_GITHUB_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^/repos/[^/]+/[^/]+$"), "/repos/{owner}/{repo}"),
    (re.compile(r"^/repos/[^/]+/[^/]+/forks$"), "/repos/{owner}/{repo}/forks"),
    (re.compile(r"^/repos/[^/]+/[^/]+/branches/.+$"),
     "/repos/{owner}/{repo}/branches/{branch}"),
    (re.compile(r"^/repos/[^/]+/[^/]+/git/refs$"), "/repos/{owner}/{repo}/git/refs"),
    (re.compile(r"^/repos/[^/]+/[^/]+/git/refs/heads/.+$"),
     "/repos/{owner}/{repo}/git/refs/heads/{branch}"),
    (re.compile(r"^/repos/[^/]+/[^/]+/contents/.+$"),
     "/repos/{owner}/{repo}/contents/{path}"),
    (re.compile(r"^/repos/[^/]+/[^/]+/issues$"), "/repos/{owner}/{repo}/issues"),
    (re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+/comments$"),
     "/repos/{owner}/{repo}/issues/{number}/comments"),
    (re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+$"),
     "/repos/{owner}/{repo}/issues/{number}"),
    (re.compile(r"^/repos/[^/]+/[^/]+/pulls$"), "/repos/{owner}/{repo}/pulls"),
    (re.compile(r"^/repos/[^/]+/[^/]+/pulls/\d+/merge$"),
     "/repos/{owner}/{repo}/pulls/{number}/merge"),
    (re.compile(r"^/repos/[^/]+/[^/]+/pulls/\d+$"), "/repos/{owner}/{repo}/pulls/{number}"),
    (re.compile(r"^/user$"), "/user"),
]

_JIRA_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^/rest/api/\d/serverInfo$"), "/rest/api/2/serverInfo"),
    (re.compile(r"^/rest/api/\d/field$"), "/rest/api/2/field"),
    (re.compile(r"^/rest/api/\d/project$"), "/rest/api/2/project"),
    (re.compile(r"^/rest/api/\d/project/[^/]+$"), "/rest/api/2/project/{key}"),
    (re.compile(r"^/rest/api/\d/search(/jql)?$"), "/rest/api/2/search"),
    (re.compile(r"^/rest/api/\d/user/search$"), "/rest/api/2/user/search"),
    (re.compile(r"^/rest/api/\d/issue$"), "/rest/api/2/issue"),
    (re.compile(r"^/rest/api/\d/issue/[^/]+/comment$"), "/rest/api/2/issue/{key}/comment"),
    (re.compile(r"^/rest/api/\d/issue/[^/]+/worklog$"), "/rest/api/2/issue/{key}/worklog"),
    (re.compile(r"^/rest/api/\d/issue/[^/]+/transitions$"),
     "/rest/api/2/issue/{key}/transitions"),
    (re.compile(r"^/rest/api/\d/issue/[^/]+$"), "/rest/api/2/issue/{key}"),
]


def normalise(api: str, path: str) -> Optional[str]:
    """Collapse a concrete path to its route template, or None if unrecognised."""
    rules = _GITHUB_RULES if api == "github" else _JIRA_RULES
    for pattern, template in rules:
        if pattern.match(path):
            return template
    return None


# --- the table ---------------------------------------------------------------

GH = "https://docs.github.com/en/rest"
JR = "https://developer.atlassian.com/cloud/jira/platform/rest/v2/"

EXPECTATIONS: Tuple[Expectation, ...] = (
    # -- GitHub ---------------------------------------------------------------
    Expectation("github", "GET", "/repos/{owner}/{repo}",
                {200: "repository returned", 404: "no such repository or no access"},
                # The tools read full_name, default_branch and url. Everything else the
                # real payload carries (topics, licence, counters) is absent.
                "T2", f"{GH}/repos/repos#get-a-repository"),
    Expectation("github", "GET", "/repos/{owner}/{repo}/branches/{branch}",
                {200: "branch returned, including its head commit sha",
                 404: "no such branch"},
                "T2", f"{GH}/branches/branches#get-a-branch"),
    Expectation("github", "POST", "/repos/{owner}/{repo}/git/refs",
                {201: "reference created",
                 422: "reference already exists, or the sha is invalid"},
                # camel branches on the exact message "Reference already exists".
                "T1", f"{GH}/git/refs#create-a-reference"),
    Expectation("github", "GET", "/repos/{owner}/{repo}/contents/{path}",
                {200: "file returned, base64 encoded, with its blob sha",
                 404: "no such file on that ref"},
                "T1", f"{GH}/repos/contents#get-repository-content"),
    Expectation("github", "PUT", "/repos/{owner}/{repo}/contents/{path}",
                {200: "existing file updated", 201: "file created",
                 409: "the supplied sha does not match the file being replaced",
                 422: "file exists and no sha was supplied, or validation failed"},
                "T1", f"{GH}/repos/contents#create-or-update-file-contents"),
    Expectation("github", "DELETE", "/repos/{owner}/{repo}/contents/{path}",
                {200: "file deleted", 404: "no such file",
                 409: "the supplied sha does not match", 422: "validation failed"},
                "T1", f"{GH}/repos/contents#delete-a-file"),
    Expectation("github", "POST", "/repos/{owner}/{repo}/issues",
                {201: "issue created; no idempotency key exists on this endpoint",
                 422: "validation failed"},
                "T2", f"{GH}/issues/issues#create-an-issue"),
    Expectation("github", "GET", "/repos/{owner}/{repo}/issues",
                {200: "issues listed"},
                "T2", f"{GH}/issues/issues#list-repository-issues"),
    Expectation("github", "POST", "/repos/{owner}/{repo}/pulls",
                {201: "pull request created",
                 422: "validation failed, e.g. no commits between base and head"},
                "T2", f"{GH}/pulls/pulls#create-a-pull-request"),
    Expectation("github", "PUT", "/repos/{owner}/{repo}/pulls/{number}/merge",
                {200: "merged", 405: "not mergeable, including already merged",
                 409: "head changed since the merge was requested"},
                "T1", f"{GH}/pulls/pulls#merge-a-pull-request"),
    Expectation("github", "POST", "/repos/{owner}/{repo}/forks",
                {202: "fork accepted; creation is asynchronous"},
                "T2", f"{GH}/repos/forks#create-a-fork"),

    # -- Jira -----------------------------------------------------------------
    Expectation("jira", "GET", "/rest/api/2/serverInfo",
                {200: "server version information"},
                "T2", f"{JR}api-group-server-info/"),
    Expectation("jira", "GET", "/rest/api/2/field",
                {200: "field metadata listed"},
                "T2", f"{JR}api-group-issue-fields/"),
    Expectation("jira", "GET", "/rest/api/2/project/{key}",
                {200: "project returned", 404: "no such project or no access"},
                "T2", f"{JR}api-group-projects/"),
    Expectation("jira", "POST", "/rest/api/2/issue",
                {201: "issue created; the endpoint has no idempotency key",
                 400: "validation failed, with per-field messages in errors"},
                "T1", f"{JR}api-group-issues/#api-rest-api-2-issue-post"),
    Expectation("jira", "GET", "/rest/api/2/issue/{key}",
                {200: "issue returned",
                 404: "no such issue, or it was deleted, or no access"},
                "T2", f"{JR}api-group-issues/#api-rest-api-2-issue-issueidorkey-get"),
    Expectation("jira", "PUT", "/rest/api/2/issue/{key}",
                {204: "updated, no content returned", 400: "validation failed",
                 404: "no such issue"},
                "T1", f"{JR}api-group-issues/#api-rest-api-2-issue-issueidorkey-put"),
    Expectation("jira", "DELETE", "/rest/api/2/issue/{key}",
                {204: "deleted, no content returned", 404: "no such issue"},
                "T1", f"{JR}api-group-issues/#api-rest-api-2-issue-issueidorkey-delete"),
    Expectation("jira", "POST", "/rest/api/2/issue/{key}/comment",
                {201: "comment created", 400: "empty or invalid body",
                 404: "no such issue"},
                "T1", f"{JR}api-group-issue-comments/"),
    Expectation("jira", "POST", "/rest/api/2/issue/{key}/worklog",
                {201: "worklog created", 400: "invalid duration", 404: "no such issue"},
                "T1", f"{JR}api-group-issue-worklogs/"),
    Expectation("jira", "GET", "/rest/api/2/issue/{key}/transitions",
                {200: "transitions available from the issue's current status",
                 404: "no such issue"},
                "T1", f"{JR}api-group-issues/#api-rest-api-2-issue-issueidorkey-transitions-get"),
    Expectation("jira", "POST", "/rest/api/2/issue/{key}/transitions",
                {204: "transitioned, no content returned",
                 400: "the transition is not valid from the current status",
                 404: "no such issue"},
                "T1", f"{JR}api-group-issues/#api-rest-api-2-issue-issueidorkey-transitions-post"),
    Expectation("jira", "GET", "/rest/api/2/search",
                # The model understands equality on project and status and nothing
                # else, so a match here is never better than behavioural.
                {200: "issues matching the JQL query"},
                "T2", f"{JR}api-group-issue-search/"),
    Expectation("jira", "GET", "/rest/api/2/user/search",
                {200: "users matching the query"},
                "T2", f"{JR}api-group-user-search/"),
)

_INDEX: Dict[Tuple[str, str, str], Expectation] = {
    (e.api, e.method, e.route): e for e in EXPECTATIONS
}


def lookup(api: str, method: str, path: str) -> Optional[Expectation]:
    """The documented expectation for a concrete request, if there is one."""
    route = normalise(api, path)
    if route is None:
        return None
    return _INDEX.get((api, method.upper(), route))
