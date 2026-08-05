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

import base64
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

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


def _decode(content: str) -> str:
    """Read a file body sent over HTTP.

    The contents endpoint takes base64, and every client library encodes before
    sending. Some callers post plain text anyway; rather than storing mojibake we fall
    back to treating an undecodable body as the text it appears to be.
    """
    try:
        return base64.b64decode(content, validate=True).decode("utf-8")
    except Exception:
        return content


class GitHubMock:
    """In-memory GitHub. Each method mirrors one real endpoint."""

    def __init__(self, repos: Optional[List[str]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 base_url: str = "https://api.github.com"):
        self._clock = clock or time.time
        # Real GitHub payloads carry absolute URLs and clients follow them rather than
        # rebuilding paths. When this model is served on an ephemeral port the caller
        # sets ``base_url`` after the port is known; see part_b/server.py.
        self.base_url = base_url.rstrip("/")
        self.repos: Dict[str, Repository] = {
            name: Repository(full_name=name) for name in (repos or ["acme/webapp"])
        }
        self.issues: Dict[str, Dict[int, Issue]] = {n: {} for n in self.repos}
        self.comments: Dict[str, Dict[int, Comment]] = {n: {} for n in self.repos}
        # repo -> branch -> path -> file. Keyed by branch because agents routinely read
        # a file on one ref and write it on another, and a model that collapsed the two
        # would report success for exactly the sequence that fails in production.
        self.contents: Dict[str, Dict[str, Dict[str, ContentFile]]] = {
            n: {r.default_branch: {}} for n, r in self.repos.items()
        }
        self.pulls: Dict[str, Dict[int, PullRequest]] = {n: {} for n in self.repos}
        # Branch name -> head sha. A repository always has its default branch.
        self.branches: Dict[str, Dict[str, str]] = {
            n: {r.default_branch: _blob_sha(n, "initial")} for n, r in self.repos.items()
        }
        self.forks: Dict[str, str] = {}          # fork full_name -> upstream full_name
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

    def get_repo(self, full_name: str) -> Repository:
        """GET /repos/{owner}/{repo}

        Every client library in the sample calls this before it can do anything else:
        it is how they obtain the handle that all later writes hang off. The model went
        without it until a real client was pointed at it, because the unit tests call
        the Python methods directly and never need a handle.
        """
        return self._repo(full_name)

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

    def _files(self, repo: str, branch: Optional[str]) -> Dict[str, ContentFile]:
        """The file map for one branch, defaulting to the repository's default branch."""
        r = self._repo(repo)
        name = branch or r.default_branch
        files = self.contents[repo].get(name)
        if files is None:
            raise GitHubError("Not Found", http_status=404)
        return files

    def get_contents(self, repo: str, path: str,
                     ref: Optional[str] = None) -> ContentFile:
        """GET /repos/{owner}/{repo}/contents/{path}?ref={ref}

        Without ``ref`` this reads the default branch -- which is what a client that
        forgets to pass one gets, and the reason a sha read here does not necessarily
        match the file on the branch the client is about to write to.
        """
        f = self._files(repo, ref).get(path)
        if f is None:
            raise GitHubError("Not Found", http_status=404)
        return f

    def put_contents(self, repo: str, path: str, content: str, message: str,
                     sha: Optional[str] = None,
                     branch: Optional[str] = None) -> ContentFile:
        """PUT /repos/{owner}/{repo}/contents/{path}

        Creating requires no sha. Updating requires the sha of the version being
        replaced: a stale one is a 409, which is how GitHub stops an agent that read
        before its own write from clobbering it.
        """
        files = self._files(repo, branch)
        if not message or not message.strip():
            raise GitHubError("Validation Failed", http_status=422)

        existing = files.get(path)
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
        files[path] = new
        return new

    def delete_contents(self, repo: str, path: str, message: str, sha: str,
                        branch: Optional[str] = None) -> Dict[str, Any]:
        """DELETE /repos/{owner}/{repo}/contents/{path}"""
        files = self._files(repo, branch)
        existing = files.get(path)
        if existing is None:
            raise GitHubError("Not Found", http_status=404)
        if not message or not message.strip():
            raise GitHubError("Validation Failed", http_status=422)
        if sha != existing.sha:
            raise GitHubError(f"{path} does not match {existing.sha}", http_status=409)
        del files[path]
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

    # -- branches, refs and forks --------------------------------------------
    #
    # Adding a file through the API is not one call. The clients in the sample fork,
    # read the base branch head, create a ref from it, write contents and open a pull
    # request -- five endpoints, each of which can fail on its own. A model that only
    # implements the write itself cannot show what an agent does when step two fails.

    def _register(self, full_name: str, default_branch: str = "main") -> Repository:
        repo = Repository(full_name=full_name, default_branch=default_branch)
        self.repos[full_name] = repo
        self.issues.setdefault(full_name, {})
        self.comments.setdefault(full_name, {})
        self.contents.setdefault(full_name, {default_branch: {}})
        self.pulls.setdefault(full_name, {})
        self.branches.setdefault(full_name, {default_branch: _blob_sha(full_name, "initial")})
        return repo

    def get_branch(self, repo: str, name: str) -> Dict[str, Any]:
        """GET /repos/{owner}/{repo}/branches/{branch}"""
        self._repo(repo)
        sha = self.branches[repo].get(name)
        if sha is None:
            raise GitHubError("Branch not found", http_status=404)
        return {"name": name, "commit": {"sha": sha, "url": self._url("repos", repo,
                                                                      "commits", sha)}}

    def get_ref(self, repo: str, branch: str) -> Dict[str, Any]:
        """GET /repos/{owner}/{repo}/git/refs/heads/{branch}"""
        self._repo(repo)
        sha = self.branches[repo].get(branch)
        if sha is None:
            raise GitHubError("Not Found", http_status=404)
        return {"ref": f"refs/heads/{branch}",
                "url": self._url("repos", repo, "git/refs/heads", branch),
                "object": {"sha": sha, "type": "commit",
                           "url": self._url("repos", repo, "git/commits", sha)}}

    def post_ref(self, repo: str, ref: str, sha: str) -> Dict[str, Any]:
        """POST /repos/{owner}/{repo}/git/refs

        Creating a ref that already exists is 422, not a second branch. An agent that
        retries a whole add-file sequence hits this on its second attempt.
        """
        self._repo(repo)
        if not ref.startswith("refs/heads/"):
            raise GitHubError("Validation Failed", http_status=422)
        branch = ref[len("refs/heads/"):]
        if branch in self.branches[repo]:
            raise GitHubError("Reference already exists", http_status=422)
        if not sha:
            raise GitHubError("Validation Failed", http_status=422)
        self.branches[repo][branch] = sha
        # A new branch starts as a copy of whatever it was cut from, so files written
        # on it diverge from the parent rather than sharing storage with it.
        source = next((b for b, s in self.branches[repo].items()
                       if s == sha and b != branch), self._repo(repo).default_branch)
        self.contents[repo][branch] = dict(self.contents[repo].get(source, {}))
        return self.get_ref(repo, branch)

    def patch_ref(self, repo: str, branch: str, sha: str) -> Dict[str, Any]:
        """PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}"""
        self._repo(repo)
        if branch not in self.branches[repo]:
            raise GitHubError("Not Found", http_status=404)
        self.branches[repo][branch] = sha
        return self.get_ref(repo, branch)

    def post_fork(self, repo: str, owner: str = "agent-bot") -> Repository:
        """POST /repos/{owner}/{repo}/forks

        Returns 202 Accepted: forking is asynchronous on the real API, so a client that
        immediately reads the fork can legitimately get a 404. The model creates it
        synchronously, which is the *kinder* of the two behaviours -- worth remembering
        when reading any result that depends on the fork being ready.
        """
        upstream = self._repo(repo)
        name = repo.split("/", 1)[1]
        full = f"{owner}/{name}"
        if full not in self.repos:
            self._register(full, upstream.default_branch)
            self.forks[full] = repo
        return self.repos[full]

    # -- HTTP surface --------------------------------------------------------
    #
    # The Python methods above speak in entities and plain text. The real API speaks in
    # JSON documents that carry absolute URLs, ISO-8601 timestamps and base64 file
    # contents. That translation belongs here, at the boundary, exactly where GitHub
    # itself does it -- which is why ``put_contents`` still takes readable text while a
    # request arriving over HTTP is decoded first.

    def _url(self, *parts: Any) -> str:
        return "/".join([self.base_url] + [str(p).strip("/") for p in parts])

    @staticmethod
    def _ts(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _repo_json(self, repo: Repository) -> Dict[str, Any]:
        owner, _, name = repo.full_name.partition("/")
        return {
            "id": abs(hash(repo.full_name)) % 10_000_000,
            "name": name,
            "full_name": repo.full_name,
            "owner": {"login": owner, "type": "Organization",
                      "url": self._url("users", owner)},
            "private": repo.private,
            "default_branch": repo.default_branch,
            "fork": False,
            "archived": False,
            "url": self._url("repos", repo.full_name),
            "html_url": f"https://github.com/{repo.full_name}",
        }

    def _issue_json(self, repo: str, issue: Issue) -> Dict[str, Any]:
        base = self._url("repos", repo, "issues", issue.number)
        return {
            "id": issue.number,
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "user": {"login": issue.user, "url": self._url("users", issue.user)},
            "created_at": self._ts(issue.created_at),
            "updated_at": self._ts(issue.created_at),
            "comments": issue.comments,
            "labels": [{"name": n} for n in issue.labels],
            "url": base,
            "comments_url": base + "/comments",
            "repository_url": self._url("repos", repo),
            "html_url": f"https://github.com/{repo}/issues/{issue.number}",
        }

    def _comment_json(self, repo: str, c: Comment) -> Dict[str, Any]:
        return {
            "id": c.id,
            "body": c.body,
            "user": {"login": c.user, "url": self._url("users", c.user)},
            "created_at": self._ts(c.created_at),
            "updated_at": self._ts(c.created_at),
            "url": self._url("repos", repo, "issues/comments", c.id),
            "issue_url": self._url("repos", repo, "issues", c.issue_number),
            "html_url": f"https://github.com/{repo}/issues/{c.issue_number}#issuecomment-{c.id}",
        }

    def _content_json(self, repo: str, f: ContentFile) -> Dict[str, Any]:
        # GitHub returns file bodies base64-encoded, and clients decode them. A model
        # that returned plain text would make every client's decode step fail.
        return {
            "type": "file",
            "name": f.path.rsplit("/", 1)[-1],
            "path": f.path,
            "sha": f.sha,
            "size": f.size,
            "encoding": "base64",
            "content": base64.b64encode(f.content.encode("utf-8")).decode("ascii"),
            "url": self._url("repos", repo, "contents", f.path),
            "git_url": self._url("repos", repo, "git/blobs", f.sha),
            "html_url": f"https://github.com/{repo}/blob/main/{f.path}",
            "download_url": f"https://raw.githubusercontent.com/{repo}/main/{f.path}",
        }

    def _commit_json(self, repo: str, sha: str, message: str) -> Dict[str, Any]:
        return {
            "sha": sha,
            "message": message,
            "url": self._url("repos", repo, "git/commits", sha),
            "html_url": f"https://github.com/{repo}/commit/{sha}",
            "author": {"name": "agent-bot", "email": "agent-bot@example.invalid",
                       "date": self._ts(self._now())},
            "committer": {"name": "agent-bot", "email": "agent-bot@example.invalid",
                          "date": self._ts(self._now())},
        }

    def _pull_json(self, repo: str, pr: PullRequest) -> Dict[str, Any]:
        return {
            "id": pr.number,
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "merged": pr.merged,
            "merge_commit_sha": pr.merge_commit_sha,
            "head": {"ref": pr.head, "sha": _blob_sha(repo, pr.head)},
            "base": {"ref": pr.base, "sha": _blob_sha(repo, pr.base)},
            "created_at": self._ts(pr.created_at),
            "url": self._url("repos", repo, "pulls", pr.number),
            "issue_url": self._url("repos", repo, "issues", pr.number),
            "html_url": f"https://github.com/{repo}/pull/{pr.number}",
        }

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
        except GitHubError as exc:
            return exc.http_status, exc.to_dict()

        if result is None:
            return 404, {"message": "Not Found"}
        return result

    def _dispatch(self, verb: str, p: List[str], body: Dict[str, Any],
                  query: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, Any]]:
        query = query or {}
        if not p:
            return None

        # /user -- clients call this to resolve the authenticated login.
        if p == ["user"] and verb == "GET":
            return 200, {"login": "agent-bot", "type": "User",
                         "url": self._url("users", "agent-bot")}

        if p[0] != "repos" or len(p) < 3:
            return None
        repo = f"{p[1]}/{p[2]}"
        rest = p[3:]

        if not rest:
            if verb == "GET":
                return 200, self._repo_json(self.get_repo(repo))
            return None

        if rest[0] == "issues":
            if len(rest) == 1:
                if verb == "POST":
                    issue = self.post_issue(repo, body.get("title", ""), body.get("body"),
                                            body.get("labels"))
                    return 201, self._issue_json(repo, issue)
                if verb == "GET":
                    return 200, [self._issue_json(repo, i)
                                 for i in self.list_issues(repo, "all")]
            if len(rest) == 2 and rest[1].isdigit():
                n = int(rest[1])
                if verb == "GET":
                    return 200, self._issue_json(repo, self.get_issue(repo, n))
                if verb == "PATCH":
                    issue = self.patch_issue(repo, n, body.get("state"), body.get("title"))
                    return 200, self._issue_json(repo, issue)
            if len(rest) == 3 and rest[1].isdigit() and rest[2] == "comments":
                n = int(rest[1])
                if verb == "POST":
                    c = self.post_issue_comment(repo, n, body.get("body", ""))
                    return 201, self._comment_json(repo, c)
                if verb == "GET":
                    return 200, [self._comment_json(repo, c)
                                 for c in self.comments[repo].values()
                                 if c.issue_number == n]

        if rest[0] == "contents" and len(rest) >= 2:
            path = "/".join(rest[1:])
            if verb == "GET":
                f = self.get_contents(repo, path, query.get("ref"))
                return 200, self._content_json(repo, f)
            if verb == "PUT":
                text = _decode(body.get("content", ""))
                f = self.put_contents(repo, path, text, body.get("message", ""),
                                      body.get("sha"), body.get("branch"))
                # GitHub wraps a write in {content, commit}; it does not return the
                # file alone. A client reading response["content"]["sha"] gets None
                # from a model that returns the bare file, and only notices later.
                return 201, {"content": self._content_json(repo, f),
                             "commit": self._commit_json(repo, f.sha,
                                                         body.get("message", ""))}
            if verb == "DELETE":
                sha = body.get("sha", "")
                self.delete_contents(repo, path, body.get("message", ""), sha,
                                     body.get("branch"))
                return 200, {"content": None,
                             "commit": self._commit_json(repo, sha,
                                                         body.get("message", ""))}

        if rest[0] == "forks" and len(rest) == 1 and verb == "POST":
            fork = self.post_fork(repo)
            return 202, self._repo_json(fork)

        if rest[0] == "branches" and len(rest) >= 2 and verb == "GET":
            return 200, self.get_branch(repo, "/".join(rest[1:]))

        if rest[0] == "git" and len(rest) >= 2 and rest[1] == "refs":
            if len(rest) == 2 and verb == "POST":
                return 201, self.post_ref(repo, body.get("ref", ""), body.get("sha", ""))
            if len(rest) >= 4 and rest[2] == "heads":
                branch = "/".join(rest[3:])
                if verb == "GET":
                    return 200, self.get_ref(repo, branch)
                if verb == "PATCH":
                    return 200, self.patch_ref(repo, branch, body.get("sha", ""))

        if rest[0] == "pulls":
            if verb == "POST" and len(rest) == 1:
                pr = self.post_pull(repo, body.get("title", ""), body.get("head", ""),
                                    body.get("base", "main"))
                return 201, self._pull_json(repo, pr)
            if len(rest) == 2 and rest[1].isdigit() and verb == "GET":
                return 200, self._pull_json(repo, self.get_pull(repo, int(rest[1])))
            if len(rest) == 3 and rest[1].isdigit() and rest[2] == "merge" and verb == "PUT":
                return 200, self.put_merge(repo, int(rest[1]))

        return None

    def __repr__(self) -> str:
        issues = sum(len(v) for v in self.issues.values())
        pulls = sum(len(v) for v in self.pulls.values())
        files = sum(len(f) for branches in self.contents.values()
                    for f in branches.values())
        return f"<GitHubMock issues={issues} pulls={pulls} files={files}>"
