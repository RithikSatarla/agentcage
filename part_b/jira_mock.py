"""A stateful in-memory model of the write surface of the Jira REST API (v2).

The substituted Part B frame (`vcs_write`, see PROTOCOL.md §3A.10) is not one API. Of
its fifteen tools only three speak to GitHub; four more speak to issue trackers, and
three of those are Jira clients written independently in three different projects. That
makes Jira the single best-represented write API in the measured population, and testing
three independent implementations against one model is a stronger check than testing one
implementation three times.

The behaviours modelled here are the ones that decide whether an agent is correct:

* creating an issue is **not idempotent** — the same call twice makes two issues, and
  Jira has no request-level idempotency key to prevent it;
* an issue that has been deleted is gone — a later read, update or delete is a 404,
  not a silent success;
* a transition that is not available from the current status is rejected, so an agent
  cannot move an issue to a state the workflow does not allow;
* a create against an unknown project fails validation with a field-level error, in the
  ``errors`` shape clients actually parse.

Identifiers come from counters and the clock is injectable, so a run is reproducible.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

__all__ = ["JiraError", "JiraMock"]

# Jira renders timestamps with milliseconds and a numeric offset, and clients parse
# them; an ISO string ending in "Z" is rejected by some of them.
_TS = "%Y-%m-%dT%H:%M:%S.000+0000"

_WORKFLOW = {
    "To Do": [("11", "In Progress"), ("31", "Done")],
    "In Progress": [("21", "To Do"), ("31", "Done")],
    "Done": [("21", "To Do")],
}


class JiraError(Exception):
    """A Jira-shaped error carrying the status it would be returned with."""

    def __init__(self, message: str, *, http_status: int = 404,
                 errors: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.errors = errors or {}

    def to_dict(self) -> Dict[str, Any]:
        # Jira always returns both keys, and clients read whichever they were written
        # against. Emitting only one of them makes half of them report "unknown error".
        return {"errorMessages": [self.message] if not self.errors else [],
                "errors": self.errors}


class JiraMock:
    """In-memory Jira. Each method mirrors one real endpoint."""

    def __init__(self, projects: Optional[List[str]] = None,
                 users: Optional[List[Tuple[str, str]]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 base_url: str = "https://example.atlassian.net"):
        self._clock = clock or time.time
        self.base_url = base_url.rstrip("/")
        self.projects: Dict[str, Dict[str, Any]] = {
            key: {"id": str(10_000 + i), "key": key, "name": f"{key} project"}
            for i, key in enumerate(projects or ["KAN"])
        }
        self.users: Dict[str, Dict[str, str]] = {}
        for i, (account, display) in enumerate(users or [("agent-bot", "Agent Bot")]):
            self.users[account] = {"accountId": account, "displayName": display,
                                   "emailAddress": f"{account}@example.invalid",
                                   "active": "true"}
        self.issues: Dict[str, Dict[str, Any]] = {}
        self._by_id: Dict[str, str] = {}      # numeric id -> issue key
        self.comments: Dict[str, List[Dict[str, Any]]] = {}
        self.worklogs: Dict[str, List[Dict[str, Any]]] = {}
        self.deleted: List[str] = []          # keys that existed and no longer do
        self._counters: Dict[str, int] = {}

    # -- helpers -------------------------------------------------------------

    def _now(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime(_TS)

    def _next(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _issue(self, ref: str) -> Dict[str, Any]:
        """Resolve an issue by key (``KAN-1``) or by numeric id (``10001``).

        Real Jira accepts either in the path and clients use both -- python-jira reads
        by key and then deletes by id, so a model that indexed only by key answers the
        read and 404s the delete that follows it.
        """
        issue = self.issues.get(ref)
        if issue is None:
            key = self._by_id.get(str(ref))
            issue = self.issues.get(key) if key else None
        if issue is None:
            # Jira does not distinguish "never existed" from "deleted", and neither
            # does this: an agent holding a key to something it already deleted gets
            # the same 404 either way.
            raise JiraError("Issue does not exist or you do not have permission to "
                            "see it.", http_status=404)
        return issue

    def _project(self, ref: Optional[str]) -> Optional[Dict[str, Any]]:
        """Resolve a project by key (``KAN``) or numeric id (``10000``).

        Clients do not necessarily send back what they were given. python-jira looks
        the project up by key and then posts ``{"project": {"id": "10000"}}``, so a
        model that only knew keys rejected every issue those clients tried to create.
        """
        if ref is None:
            return None
        ref = str(ref)
        if ref in self.projects:
            return self.projects[ref]
        return next((p for p in self.projects.values() if p["id"] == ref), None)

    def _user(self, account_id: str) -> Dict[str, str]:
        user = self.users.get(account_id)
        if user is None:
            raise JiraError("The user does not exist.", http_status=400)
        return user

    # -- issues --------------------------------------------------------------

    def create_issue(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """POST /rest/api/2/issue

        There is no idempotency key on this endpoint. Two identical calls make two
        issues, both of which are real. An agent that retries after a timeout it never
        saw the response to has just filed the same bug twice.
        """
        given = fields.get("project")
        if isinstance(given, dict):
            given = given.get("key") or given.get("id")
        project = self._project(given)
        if project is None:
            raise JiraError("Validation failed", http_status=400,
                            errors={"project": "project is required"})
        pkey = project["key"]
        summary = fields.get("summary")
        if not summary or not str(summary).strip():
            raise JiraError("Validation failed", http_status=400,
                            errors={"summary": "You must specify a summary of the issue."})
        itype = (fields.get("issuetype") or {}).get("name") or "Task"

        n = self._next(f"project:{pkey}")
        key = f"{pkey}-{n}"
        iid = str(self._next("id") + 10_000)
        assignee = fields.get("assignee")
        self.issues[key] = {
            "id": iid,
            "key": key,
            "summary": str(summary),
            "description": fields.get("description"),
            "issuetype": itype,
            "project": pkey,
            "status": "To Do",
            "assignee": (assignee or {}).get("accountId") if assignee else None,
            "reporter": next(iter(self.users), None),
            "duedate": fields.get("duedate"),
            "created": self._now(),
            "updated": self._now(),
        }
        self._by_id[iid] = key
        self.comments[key] = []
        self.worklogs[key] = []
        return {"id": iid, "key": key,
                "self": f"{self.base_url}/rest/api/2/issue/{iid}"}

    def get_issue(self, key: str) -> Dict[str, Any]:
        """GET /rest/api/2/issue/{key}"""
        return self._issue_json(self._issue(key))

    def update_issue(self, key: str, fields: Dict[str, Any]) -> None:
        """PUT /rest/api/2/issue/{key} -- 204 No Content on success."""
        issue = self._issue(key)
        if "summary" in fields:
            summary = fields["summary"]
            if not summary or not str(summary).strip():
                raise JiraError("Validation failed", http_status=400,
                                errors={"summary": "You must specify a summary."})
            issue["summary"] = str(summary)
        if "description" in fields:
            issue["description"] = fields["description"]
        if "duedate" in fields:
            due = fields["duedate"]
            if due is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due)):
                raise JiraError("Validation failed", http_status=400,
                                errors={"duedate": "Error parsing date/time."})
            issue["duedate"] = due
        if "assignee" in fields:
            a = fields["assignee"]
            if a is None:
                issue["assignee"] = None
            else:
                account = a.get("accountId") if isinstance(a, dict) else a
                self._user(account)
                issue["assignee"] = account
        issue["updated"] = self._now()

    def delete_issue(self, ref: str) -> None:
        """DELETE /rest/api/2/issue/{key} -- 204 No Content on success."""
        issue = self._issue(ref)
        key = issue["key"]
        del self.issues[key]
        self._by_id.pop(issue["id"], None)
        self.comments.pop(key, None)
        self.worklogs.pop(key, None)
        self.deleted.append(key)

    # -- comments, worklogs, transitions -------------------------------------

    def add_comment(self, ref: str, body: str) -> Dict[str, Any]:
        """POST /rest/api/2/issue/{key}/comment"""
        issue = self._issue(ref)
        key = issue["key"]
        if body is None or not str(body).strip():
            raise JiraError("Validation failed", http_status=400,
                            errors={"body": "Comment body can not be empty!"})
        cid = str(self._next("comment"))
        comment = {"id": cid, "body": str(body), "created": self._now(),
                   "updated": self._now(),
                   "author": self._user_json(self.issues[key]["reporter"]),
                   "self": f"{self.base_url}/rest/api/2/issue/{key}/comment/{cid}"}
        self.comments[key].append(comment)
        return comment

    def add_worklog(self, ref: str, time_spent: Optional[str],
                    comment: Optional[str] = None) -> Dict[str, Any]:
        """POST /rest/api/2/issue/{key}/worklog"""
        issue = self._issue(ref)
        key = issue["key"]
        if not time_spent:
            raise JiraError("Validation failed", http_status=400,
                            errors={"timeSpent": "Time spent is required."})
        if not re.fullmatch(r"(\s*\d+(\.\d+)?\s*[wdhm]\s*)+", str(time_spent)):
            raise JiraError("Validation failed", http_status=400,
                            errors={"timeSpent": "Invalid time duration entered."})
        wid = str(self._next("worklog"))
        entry = {"id": wid, "timeSpent": str(time_spent), "comment": comment,
                 "created": self._now(), "started": self._now(),
                 "author": self._user_json(self.issues[key]["reporter"]),
                 "self": f"{self.base_url}/rest/api/2/issue/{key}/worklog/{wid}"}
        self.worklogs[key].append(entry)
        return entry

    def transitions(self, key: str) -> List[Dict[str, Any]]:
        """GET /rest/api/2/issue/{key}/transitions

        Only the transitions available *from the current status* are listed. This is
        the part a replay fixture cannot reproduce: the list changes as the issue
        moves, so a recorded response is correct exactly once.
        """
        issue = self._issue(key)
        return [{"id": tid, "name": name, "to": self._status_json(name)}
                for tid, name in _WORKFLOW.get(issue["status"], [])]

    def transition_issue(self, key: str, transition_id: str) -> None:
        """POST /rest/api/2/issue/{key}/transitions -- 204 No Content."""
        issue = self._issue(key)
        available = dict((tid, name) for tid, name in _WORKFLOW.get(issue["status"], []))
        name = available.get(str(transition_id))
        if name is None:
            raise JiraError("Validation failed", http_status=400,
                            errors={"transition": "The transition is not valid "
                                                  "for the current issue status."})
        issue["status"] = name
        issue["updated"] = self._now()

    # -- search --------------------------------------------------------------

    def search(self, jql: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """GET /rest/api/2/search

        JQL support is deliberately shallow: equality on ``project`` and ``status``,
        and nothing else. A query this cannot represent returns every issue, which is
        wrong, and any request that depends on richer filtering must be graded a
        fidelity gap rather than a match. Saying so here is cheaper than discovering
        it in a result table.
        """
        issues = list(self.issues.values())
        for field, value in re.findall(r'(\w+)\s*=\s*"?([\w\s-]+?)"?(?:\s+AND\s+|\s*$|\s+ORDER\s+)',
                                       jql or "", flags=re.IGNORECASE):
            f = field.lower()
            if f == "project":
                issues = [i for i in issues if i["project"] == value.strip()]
            elif f == "status":
                issues = [i for i in issues if i["status"].lower() == value.strip().lower()]
        return [self._issue_json(i) for i in issues[:max_results]]

    def search_users(self, query: str) -> List[Dict[str, str]]:
        """GET /rest/api/2/user/search"""
        q = (query or "").lower()
        return [self._user_json(a) for a, u in self.users.items()
                if q in u["displayName"].lower() or q in u["emailAddress"].lower()]

    # -- serialisation -------------------------------------------------------

    def _user_json(self, account_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if account_id is None:
            return None
        u = self.users.get(account_id)
        if u is None:
            return None
        return {"accountId": u["accountId"], "displayName": u["displayName"],
                "emailAddress": u["emailAddress"], "active": True,
                "self": f"{self.base_url}/rest/api/2/user?accountId={u['accountId']}"}

    @staticmethod
    def _status_json(name: str) -> Dict[str, Any]:
        category = {"To Do": ("new", 2), "In Progress": ("indeterminate", 4),
                    "Done": ("done", 3)}.get(name, ("new", 2))
        return {"id": str(abs(hash(name)) % 1000), "name": name,
                "statusCategory": {"key": category[0], "id": category[1]}}

    def _issue_json(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        key = issue["key"]
        project = self.projects[issue["project"]]
        comments = self.comments.get(key, [])
        return {
            "id": issue["id"],
            "key": key,
            "self": f"{self.base_url}/rest/api/2/issue/{issue['id']}",
            "fields": {
                "summary": issue["summary"],
                "description": issue["description"],
                "project": {"id": project["id"], "key": project["key"],
                            "name": project["name"]},
                "issuetype": {"id": "10000", "name": issue["issuetype"],
                              "subtask": False},
                "status": self._status_json(issue["status"]),
                "assignee": self._user_json(issue["assignee"]),
                "reporter": self._user_json(issue["reporter"]),
                "duedate": issue["duedate"],
                "created": issue["created"],
                "updated": issue["updated"],
                "labels": [],
                "comment": {"comments": comments, "total": len(comments),
                            "startAt": 0, "maxResults": len(comments)},
                "worklog": {"worklogs": self.worklogs.get(key, []),
                            "total": len(self.worklogs.get(key, []))},
            },
        }

    # -- HTTP surface --------------------------------------------------------

    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        """Route one request. Returns ``(status, json_body)`` and never raises."""
        body = dict(body or {})
        verb = method.upper()
        raw_path, _, raw_query = path.partition("?")
        parts = [p for p in raw_path.strip("/").split("/") if p]
        query = {k: v[0] for k, v in parse_qs(raw_query).items() if v}

        try:
            result = self._dispatch(verb, parts, body, query)
        except JiraError as exc:
            return exc.http_status, exc.to_dict()

        if result is None:
            return 404, {"errorMessages": ["Not Found"], "errors": {}}
        return result

    def _dispatch(self, verb: str, p: List[str], body: Dict[str, Any],
                  query: Dict[str, str]) -> Optional[Tuple[int, Any]]:
        # /rest/api/{2,3}/...
        if len(p) < 3 or p[0] != "rest" or p[1] != "api":
            return None
        rest = p[3:]
        if not rest:
            return None

        if rest[0] == "serverInfo" and verb == "GET":
            return 200, {"baseUrl": self.base_url, "version": "9.4.0",
                         "versionNumbers": [9, 4, 0], "deploymentType": "Cloud",
                         "buildNumber": 940000, "serverTitle": "AgentCage Jira model"}

        if rest[0] == "field" and verb == "GET":
            return 200, [
                {"id": "summary", "name": "Summary", "custom": False,
                 "navigable": True, "searchable": True, "clauseNames": ["summary"],
                 "schema": {"type": "string", "system": "summary"}},
                {"id": "description", "name": "Description", "custom": False,
                 "navigable": True, "searchable": True, "clauseNames": ["description"],
                 "schema": {"type": "string", "system": "description"}},
                {"id": "duedate", "name": "Due Date", "custom": False,
                 "navigable": True, "searchable": True, "clauseNames": ["duedate"],
                 "schema": {"type": "date", "system": "duedate"}},
                {"id": "assignee", "name": "Assignee", "custom": False,
                 "navigable": True, "searchable": True, "clauseNames": ["assignee"],
                 "schema": {"type": "user", "system": "assignee"}},
            ]

        if rest[0] == "project" and verb == "GET":
            if len(rest) == 1:
                return 200, list(self.projects.values())
            proj = self._project(rest[1])
            if proj is None:
                raise JiraError("No project could be found with that key.",
                                http_status=404)
            return 200, proj

        if rest[0] == "search":
            jql = query.get("jql") if verb == "GET" else body.get("jql", "")
            try:
                mx = int(query.get("maxResults") or body.get("maxResults") or 50)
            except (TypeError, ValueError):
                mx = 50
            found = self.search(jql or "", mx)
            return 200, {"startAt": 0, "maxResults": mx, "total": len(found),
                         "issues": found, "expand": "schema,names"}

        if rest[0] == "user" and len(rest) >= 2 and rest[1] == "search" and verb == "GET":
            return 200, self.search_users(query.get("query", ""))

        if rest[0] == "issue":
            if len(rest) == 1 and verb == "POST":
                return 201, self.create_issue(body.get("fields") or {})
            if len(rest) >= 2:
                key = rest[1]
                if len(rest) == 2:
                    if verb == "GET":
                        return 200, self.get_issue(key)
                    if verb == "PUT":
                        self.update_issue(key, body.get("fields") or {})
                        return 204, None
                    if verb == "DELETE":
                        self.delete_issue(key)
                        return 204, None
                if len(rest) == 3 and rest[2] == "comment":
                    if verb == "POST":
                        return 201, self.add_comment(key, body.get("body", ""))
                    if verb == "GET":
                        cs = self.comments.get(self._issue(key)["key"], [])
                        return 200, {"comments": cs, "total": len(cs),
                                     "startAt": 0, "maxResults": len(cs)}
                if len(rest) == 3 and rest[2] == "worklog" and verb == "POST":
                    return 201, self.add_worklog(key, body.get("timeSpent"),
                                                 body.get("comment"))
                if len(rest) == 3 and rest[2] == "transitions":
                    if verb == "GET":
                        return 200, {"expand": "transitions",
                                     "transitions": self.transitions(key)}
                    if verb == "POST":
                        tid = (body.get("transition") or {}).get("id")
                        self.transition_issue(key, tid)
                        return 204, None

        return None

    def __repr__(self) -> str:
        return (f"<JiraMock issues={len(self.issues)} deleted={len(self.deleted)} "
                f"comments={sum(len(c) for c in self.comments.values())}>")
