"""A stateful in-memory model of the write surface of the GitHub REST API.

Built because the pre-registered Part B frame turned out to be empty: no tool in the
Part A analysed set performs a payment operation, so the Stripe model has no target in
the population we measured. Version-control writes are the category that does exist, and
the least covered of any (15 tools, 46.7% never referenced by a test).

The failure modes this makes visible are the version-control analogues of a double
refund:

* a retried issue creation makes **two** issues, because GitHub has no idempotency key;
* a file update carrying a stale ``sha`` is rejected 409, so an agent that cached a read
  from before its own write cannot silently clobber it;
* merging an already-merged pull request is 405, not another merge.

Every rule is enforced here and asserted by the test suite. Identifiers come from
counters and the clock is injectable, so a run is byte-for-byte reproducible.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

__all__ = [
    "GitHubError",
    "Repository",
    "Issue",
    "Comment",
    "ContentFile",
    "PullRequest",
    "GitHubMock",
]


class GitHubError(Exception):
    """A GitHub-shaped API error carrying the status it would be returned with."""

    def __init__(self, message: str, *, http_status: int = 404,
                 errors: Optional[List[Dict[str, str]]] = None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.errors = errors or []

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": self.message}
        if self.errors:
            body["errors"] = self.errors
        return body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GitHubError {self.http_status}: {self.message}>"


# --- entities ----------------------------------------------------------------


class Repository(BaseModel):
    full_name: str
    default_branch: str = "main"
    private: bool = False


class Issue(BaseModel):
    number: int
    title: str
    body: Optional[str] = None
    state: str = "open"
    user: str = "agent-bot"
    created_at: int
    comments: int = 0
    labels: List[str] = Field(default_factory=list)


class Comment(BaseModel):
    id: int
    issue_number: int
    body: str
    user: str = "agent-bot"
    created_at: int


class ContentFile(BaseModel):
    path: str
    content: str
    sha: str
    size: int


class PullRequest(BaseModel):
    number: int
    title: str
    head: str
    base: str
    state: str = "open"
    merged: bool = False
    merge_commit_sha: Optional[str] = None
    created_at: int


def _blob_sha(path: str, content: str) -> str:
    """Deterministic stand-in for a git blob sha."""
    return hashlib.sha1(f"{path}\x00{content}".encode("utf-8")).hexdigest()


class GitHubMock:
    """In-memory GitHub. Each method mirrors one real endpoint."""

    def __init__(self, repos: Optional[List[str]] = None,
                 clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.time
        self.repos: Dict[str, Repository] = {
            name: Repository(full_name=name) for name in (repos or ["acme/webapp"])
        }
        self.issues: Dict[str, Dict[int, Issue]] = {n: {} for n in self.repos}
        self.comments: Dict[str, Dict[int, Comment]] = {n: {} for n in self.repos}
        self.contents: Dict[str, Dict[str, ContentFile]] = {n: {} for n in self.repos}
        self.pulls: Dict[str, Dict[int, PullRequest]] = {n: {} for n in self.repos}
        self._counters: Dict[str, int] = {}

    # -- helpers -------------------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _next(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _repo(self, full_name: str) -> Repository:
        repo = self.repos.get(full_name)
        if repo is None:
            raise GitHubError("Not Found", http_status=404)
        return repo

    # -- issues --------------------------------------------------------------

    def post_issue(self, repo: str, title: str, body: Optional[str] = None,
                   labels: Optional[List[str]] = None) -> Issue:
        """POST /repos/{owner}/{repo}/issues

        Note there is no idempotency key: calling this twice with identical arguments
        creates two issues, exactly as the real API does. That is the defect a replay
        fixture cannot show you.
        """
        self._repo(repo)
        if not title or not title.strip():
            raise GitHubError(
                "Validation Failed", http_status=422,
                errors=[{"resource": "Issue", "field": "title", "code": "missing_field"}],
            )
        number = self._next(f"{repo}:number")
        issue = Issue(number=number, title=title, body=body,
                      created_at=self._now(), labels=labels or [])
        self.issues[repo][number] = issue
        return issue

    def get_issue(self, repo: str, number: int) -> Issue:
        """GET /repos/{owner}/{repo}/issues/{number}"""
        self._repo(repo)
        issue = self.issues[repo].get(number)
        if issue is None:
            raise GitHubError("Not Found", http_status=404)
        return issue

    def list_issues(self, repo: str, state: str = "open") -> List[Issue]:
        """GET /repos/{owner}/{repo}/issues"""
        self._repo(repo)
        return [i for i in self.issues[repo].values()
                if state == "all" or i.state == state]

    def patch_issue(self, repo: str, number: int, state: Optional[str] = None,
                    title: Optional[str] = None) -> Issue:
        """PATCH /repos/{owner}/{repo}/issues/{number}"""
        issue = self.get_issue(repo, number)
        if state is not None:
            if state not in ("open", "closed"):
                raise GitHubError("Validation Failed", http_status=422)
            issue.state = state
        if title is not None:
            issue.title = title
        return issue

    def post_issue_comment(self, repo: str, number: int, body: str) -> Comment:
        """POST /repos/{owner}/{repo}/issues/{number}/comments"""
        issue = self.get_issue(repo, number)
        if not body or not body.strip():
            raise GitHubError("Validation Failed", http_status=422)
        cid = self._next(f"{repo}:comment")
        comment = Comment(id=cid, issue_number=number, body=body, created_at=self._now())
        self.comments[repo][cid] = comment
        issue.comments += 1
        return comment

    # -- contents ------------------------------------------------------------

    def get_contents(self, repo: str, path: str) -> ContentFile:
        """GET /repos/{owner}/{repo}/contents/{path}"""
        self._repo(repo)
        f = self.contents[repo].get(path)
        if f is None:
            raise GitHubError("Not Found", http_status=404)
        return f

    def put_contents(self, repo: str, path: str, content: str, message: str,
                     sha: Optional[str] = None) -> ContentFile:
        """PUT /repos/{owner}/{repo}/contents/{path}

        Creating requires no sha. Updating requires the sha of the version being
        replaced: a stale one is a 409, which is how GitHub stops an agent that read
        before its own write from clobbering it.
        """
        self._repo(repo)
        if not message or not message.strip():
            raise GitHubError("Validation Failed", http_status=422)

        existing = self.contents[repo].get(path)
        if existing is None:
            if sha is not None:
                raise GitHubError("Not Found", http_status=404)
        else:
            if sha is None:
                raise GitHubError(
                    f'"{path}" already exists; you must supply its sha to update it',
                    http_status=422,
                )
            if sha != existing.sha:
                raise GitHubError(
                    f"{path} does not match {existing.sha}", http_status=409
                )

        new = ContentFile(path=path, content=content,
                          sha=_blob_sha(path, content), size=len(content))
        self.contents[repo][path] = new
        return new

    def delete_contents(self, repo: str, path: str, message: str, sha: str) -> Dict[str, Any]:
        """DELETE /repos/{owner}/{repo}/contents/{path}"""
        existing = self.get_contents(repo, path)
        if not message or not message.strip():
            raise GitHubError("Validation Failed", http_status=422)
        if sha != existing.sha:
            raise GitHubError(f"{path} does not match {existing.sha}", http_status=409)
        del self.contents[repo][path]
        return {"commit": {"message": message}, "content": None}

    # -- pull requests -------------------------------------------------------

    def post_pull(self, repo: str, title: str, head: str, base: str = "main") -> PullRequest:
        """POST /repos/{owner}/{repo}/pulls"""
        self._repo(repo)
        if head == base:
            raise GitHubError(
                "Validation Failed", http_status=422,
                errors=[{"resource": "PullRequest", "field": "head",
                         "code": "invalid", "message": "No commits between base and head"}],
            )
        number = self._next(f"{repo}:number")
        pr = PullRequest(number=number, title=title, head=head, base=base,
                         created_at=self._now())
        self.pulls[repo][number] = pr
        return pr

    def get_pull(self, repo: str, number: int) -> PullRequest:
        """GET /repos/{owner}/{repo}/pulls/{number}"""
        self._repo(repo)
        pr = self.pulls[repo].get(number)
        if pr is None:
            raise GitHubError("Not Found", http_status=404)
        return pr

    def put_merge(self, repo: str, number: int) -> Dict[str, Any]:
        """PUT /repos/{owner}/{repo}/pulls/{number}/merge

        A second merge is 405, not another merge. This is the version-control analogue
        of refunding an already refunded charge.
        """
        pr = self.get_pull(repo, number)
        if pr.merged:
            raise GitHubError("Pull Request is not mergeable", http_status=405)
        pr.merged = True
        pr.state = "closed"
        pr.merge_commit_sha = _blob_sha(f"{repo}#{number}", "merge")
        return {"merged": True, "sha": pr.merge_commit_sha,
                "message": "Pull Request successfully merged"}

    # -- HTTP surface --------------------------------------------------------

    def handle(self, method: str, path: str,
               body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        """Route one request. Returns ``(status, json_body)`` and never raises."""
        body = dict(body or {})
        verb = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]

        try:
            result = self._dispatch(verb, parts, body)
        except GitHubError as exc:
            return exc.http_status, exc.to_dict()

        if result is None:
            return 404, {"message": "Not Found"}
        status = 201 if verb == "POST" else 200
        payload = result.model_dump() if isinstance(result, BaseModel) else result
        return status, payload

    def _dispatch(self, verb: str, p: List[str], body: Dict[str, Any]) -> Any:
        # /repos/{owner}/{repo}/...
        if len(p) < 4 or p[0] != "repos":
            return None
        repo = f"{p[1]}/{p[2]}"
        rest = p[3:]

        if rest[0] == "issues":
            if verb == "POST" and len(rest) == 1:
                return self.post_issue(repo, body.get("title", ""), body.get("body"),
                                       body.get("labels"))
            if verb == "GET" and len(rest) == 1:
                return {"items": [i.model_dump() for i in self.list_issues(repo)]}
            if len(rest) == 2 and rest[1].isdigit():
                n = int(rest[1])
                if verb == "GET":
                    return self.get_issue(repo, n)
                if verb == "PATCH":
                    return self.patch_issue(repo, n, body.get("state"), body.get("title"))
            if len(rest) == 3 and rest[1].isdigit() and rest[2] == "comments":
                if verb == "POST":
                    return self.post_issue_comment(repo, int(rest[1]), body.get("body", ""))

        if rest[0] == "contents" and len(rest) >= 2:
            path = "/".join(rest[1:])
            if verb == "GET":
                return self.get_contents(repo, path)
            if verb == "PUT":
                return self.put_contents(repo, path, body.get("content", ""),
                                         body.get("message", ""), body.get("sha"))
            if verb == "DELETE":
                return self.delete_contents(repo, path, body.get("message", ""),
                                            body.get("sha", ""))

        if rest[0] == "pulls":
            if verb == "POST" and len(rest) == 1:
                return self.post_pull(repo, body.get("title", ""), body.get("head", ""),
                                      body.get("base", "main"))
            if len(rest) == 2 and rest[1].isdigit() and verb == "GET":
                return self.get_pull(repo, int(rest[1]))
            if len(rest) == 3 and rest[1].isdigit() and rest[2] == "merge" and verb == "PUT":
                return self.put_merge(repo, int(rest[1]))

        return None

    def __repr__(self) -> str:
        issues = sum(len(v) for v in self.issues.values())
        pulls = sum(len(v) for v in self.pulls.values())
        files = sum(len(v) for v in self.contents.values())
        return f"<GitHubMock issues={issues} pulls={pulls} files={files}>"
