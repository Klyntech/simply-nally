"""Static GitHub tools used when remote MCP discovery fails but a vault credential exists.

Calls the GitHub REST API with the user's OAuth token so common repo operations
work even if the remote GitHub MCP endpoint is unreachable.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from nally.tools.base import Tool, ToolRegistry

logger = logging.getLogger(__name__)

_API = "https://api.github.com"


def _token_for(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        from nally.vault import get_vault

        cred = get_vault().get_valid(user_id, "github")
        if cred and cred.access_token:
            return cred.access_token
    except Exception as exc:
        logger.debug("vault github token: %s", exc)
    try:
        from nally.oauth.token_store import TokenStore

        t = TokenStore().get_valid(user_id, "github")
        if t:
            return t.access_token
    except Exception:
        pass
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(token: str, path: str, params: dict | None = None) -> Any:
    r = requests.get(f"{_API}{path}", headers=_headers(token), params=params or {}, timeout=30)
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


def _gh_post(token: str, path: str, body: dict) -> Any:
    r = requests.post(f"{_API}{path}", headers=_headers(token), json=body, timeout=30)
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


def _gh_patch(token: str, path: str, body: dict) -> Any:
    r = requests.patch(f"{_API}{path}", headers=_headers(token), json=body, timeout=30)
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


def _gh_put(token: str, path: str, body: dict) -> Any:
    r = requests.put(f"{_API}{path}", headers=_headers(token), json=body, timeout=30)
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


def _need_token(user_id: str | None) -> str | None:
    token = _token_for(user_id)
    if not token:
        return None
    return token


def _auth_err() -> str:
    return "Error: AUTH_REQUIRED: No GitHub credential. Connect via /mcp → Connect GitHub."


def _parse_owner_repo(full_or_name: str, default_owner: str | None = None) -> tuple[str, str] | str:
    s = (full_or_name or "").strip().strip("/")
    if "/" in s:
        owner, repo = s.split("/", 1)
        return owner, repo
    if default_owner:
        return default_owner, s
    return f"Error: provide owner/repo (got '{full_or_name}')"


class _BaseGH(Tool):
    def __init__(self, user_id: str | None, name: str, description: str, parameters: dict) -> None:
        super().__init__(name=name, description=description, parameters=parameters)
        self.user_id = user_id


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _GitHubListRepos(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_repos") -> None:
        super().__init__(
            user_id,
            name,
            "List GitHub repositories for the authenticated user.",
            {
                "visibility": {"type": "string", "description": "all | public | private", "required": False},
                "affiliation": {
                    "type": "string",
                    "description": "owner, collaborator, organization_member",
                    "required": False,
                },
                "per_page": {"type": "integer", "description": "Max 100", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        params = {
            "visibility": kwargs.get("visibility") or "all",
            "affiliation": kwargs.get("affiliation") or "owner,collaborator,organization_member",
            "per_page": min(int(kwargs.get("per_page") or 30), 100),
            "sort": "updated",
        }
        data = _gh_get(token, "/user/repos", params)
        if isinstance(data, str):
            return data
        lines = []
        for repo in data:
            priv = "private" if repo.get("private") else "public"
            desc = (repo.get("description") or "").strip()
            lines.append(
                f"- {repo.get('full_name')} ({priv})"
                + (f" — {desc}" if desc else "")
                + f"\n  {repo.get('html_url')}"
            )
        return f"Found {len(lines)} repo(s):\n" + "\n".join(lines) if lines else "No repositories found."


class _GitHubSearchRepos(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_search_repositories") -> None:
        super().__init__(
            user_id,
            name,
            "Search GitHub repositories. Use user:USERNAME to scope to a user.",
            {
                "query": {"type": "string", "description": "Search query", "required": True},
                "per_page": {"type": "integer", "description": "Max 30", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        q = (kwargs.get("query") or "").strip()
        if not q:
            return "Error: query is required"
        data = _gh_get(token, "/search/repositories", {"q": q, "per_page": min(int(kwargs.get("per_page") or 10), 30)})
        if isinstance(data, str):
            return data
        items = data.get("items") or []
        lines = [
            f"- {r.get('full_name')} ⭐{r.get('stargazers_count', 0)}\n  {r.get('html_url')}" for r in items
        ]
        total = data.get("total_count", len(items))
        return f"Search results ({total} total, showing {len(lines)}):\n" + "\n".join(lines) if lines else f"No match: {q}"


class _GitHubCreateRepo(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_create_repository") -> None:
        super().__init__(
            user_id,
            name,
            "Create a new GitHub repository under the authenticated user.",
            {
                "name": {"type": "string", "description": "Repository name", "required": True},
                "description": {"type": "string", "description": "Short description", "required": False},
                "private": {"type": "boolean", "description": "Private repo if true", "required": False},
                "auto_init": {"type": "boolean", "description": "Init with README (default true)", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        name = (kwargs.get("name") or "").strip()
        if not name:
            return "Error: name is required"
        body = {
            "name": name,
            "description": kwargs.get("description") or "",
            "private": bool(kwargs.get("private", False)),
            "auto_init": bool(kwargs.get("auto_init", True)),
        }
        data = _gh_post(token, "/user/repos", body)
        if isinstance(data, str):
            return data
        return (
            f"Created {data.get('full_name')}\n"
            f"URL: {data.get('html_url')}\n"
            f"Clone: {data.get('clone_url')}\n"
            f"Private: {data.get('private')}"
        )


class _GitHubGetRepo(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_get_repository") -> None:
        super().__init__(
            user_id,
            name,
            "Get details for a GitHub repository (owner/repo).",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        if not owner or not repo:
            return "Error: owner and repo are required"
        data = _gh_get(token, f"/repos/{owner}/{repo}")
        if isinstance(data, str):
            return data
        return (
            f"{data.get('full_name')} ({'private' if data.get('private') else 'public'})\n"
            f"Description: {data.get('description') or '(none)'}\n"
            f"Default branch: {data.get('default_branch')}\n"
            f"Stars: {data.get('stargazers_count')}  Forks: {data.get('forks_count')}\n"
            f"Language: {data.get('language')}\n"
            f"URL: {data.get('html_url')}\n"
            f"Clone: {data.get('clone_url')}"
        )


class _GitHubListIssues(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_issues") -> None:
        super().__init__(
            user_id,
            name,
            "List issues in a repository (excludes PRs by default filter in display).",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "state": {"type": "string", "description": "open | closed | all", "required": False},
                "per_page": {"type": "integer", "description": "Max 50", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        if not owner or not repo:
            return "Error: owner and repo required"
        data = _gh_get(
            token,
            f"/repos/{owner}/{repo}/issues",
            {
                "state": kwargs.get("state") or "open",
                "per_page": min(int(kwargs.get("per_page") or 20), 50),
            },
        )
        if isinstance(data, str):
            return data
        lines = []
        for it in data:
            if it.get("pull_request"):
                continue
            labels = ", ".join(l.get("name", "") for l in (it.get("labels") or []))
            lines.append(
                f"- #{it.get('number')} {it.get('title')} [{it.get('state')}]"
                + (f" ({labels})" if labels else "")
                + f"\n  {it.get('html_url')}"
            )
        return f"{len(lines)} issue(s):\n" + "\n".join(lines) if lines else "No issues found."


class _GitHubCreateIssue(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_create_issue") -> None:
        super().__init__(
            user_id,
            name,
            "Create an issue in a repository.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "title": {"type": "string", "description": "Issue title", "required": True},
                "body": {"type": "string", "description": "Issue body markdown", "required": False},
                "labels": {"type": "string", "description": "Comma-separated labels", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        title = (kwargs.get("title") or "").strip()
        if not owner or not repo or not title:
            return "Error: owner, repo, and title are required"
        body: dict[str, Any] = {"title": title, "body": kwargs.get("body") or ""}
        labels = kwargs.get("labels")
        if labels:
            body["labels"] = [x.strip() for x in str(labels).split(",") if x.strip()]
        data = _gh_post(token, f"/repos/{owner}/{repo}/issues", body)
        if isinstance(data, str):
            return data
        return f"Created issue #{data.get('number')}: {data.get('title')}\n{data.get('html_url')}"


class _GitHubGetFile(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_get_file_contents") -> None:
        super().__init__(
            user_id,
            name,
            "Get file contents from a repository path.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "path": {"type": "string", "description": "File path in repo", "required": True},
                "ref": {"type": "string", "description": "Branch/tag/commit (optional)", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo, path = (
            (kwargs.get("owner") or "").strip(),
            (kwargs.get("repo") or "").strip(),
            (kwargs.get("path") or "").strip(),
        )
        if not owner or not repo or not path:
            return "Error: owner, repo, and path are required"
        params = {}
        if kwargs.get("ref"):
            params["ref"] = kwargs["ref"]
        data = _gh_get(token, f"/repos/{owner}/{repo}/contents/{path}", params)
        if isinstance(data, str):
            return data
        if isinstance(data, list):
            names = [f"- {x.get('type')}: {x.get('path')}" for x in data]
            return f"Directory {path} ({len(names)} entries):\n" + "\n".join(names)
        encoding = data.get("encoding")
        content = data.get("content") or ""
        if encoding == "base64":
            try:
                content = base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception as exc:
                return f"Error decoding file: {exc}"
        if len(content) > 15000:
            content = content[:15000] + f"\n... [truncated, {len(content)} chars total]"
        return f"{data.get('path')} (sha {data.get('sha', '')[:7]})\n\n{content}"


class _GitHubPushFile(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_create_or_update_file") -> None:
        super().__init__(
            user_id,
            name,
            "Create or update a file in a repository via the Contents API.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "path": {"type": "string", "description": "File path", "required": True},
                "content": {"type": "string", "description": "File text content", "required": True},
                "message": {"type": "string", "description": "Commit message", "required": True},
                "branch": {"type": "string", "description": "Branch (default: repo default)", "required": False},
                "sha": {
                    "type": "string",
                    "description": "Blob SHA if updating existing file (required for update)",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner = (kwargs.get("owner") or "").strip()
        repo = (kwargs.get("repo") or "").strip()
        path = (kwargs.get("path") or "").strip()
        content = kwargs.get("content")
        message = (kwargs.get("message") or "").strip()
        if not owner or not repo or not path or content is None or not message:
            return "Error: owner, repo, path, content, and message are required"
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(str(content).encode("utf-8")).decode("ascii"),
        }
        if kwargs.get("branch"):
            body["branch"] = kwargs["branch"]
        sha = kwargs.get("sha")
        if not sha:
            # Try to fetch existing sha for update
            existing = _gh_get(
                token,
                f"/repos/{owner}/{repo}/contents/{path}",
                {"ref": kwargs["branch"]} if kwargs.get("branch") else None,
            )
            if isinstance(existing, dict) and existing.get("sha"):
                sha = existing["sha"]
        if sha:
            body["sha"] = sha
        data = _gh_put(token, f"/repos/{owner}/{repo}/contents/{path}", body)
        if isinstance(data, str):
            return data
        commit = (data.get("commit") or {})
        content_info = data.get("content") or {}
        return (
            f"Wrote {content_info.get('path')}\n"
            f"Commit: {commit.get('sha', '')[:7]} — {commit.get('message')}\n"
            f"URL: {content_info.get('html_url') or commit.get('html_url')}"
        )


class _GitHubListCommits(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_commits") -> None:
        super().__init__(
            user_id,
            name,
            "List recent commits on a repository.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "sha": {"type": "string", "description": "Branch or commit SHA", "required": False},
                "per_page": {"type": "integer", "description": "Max 30", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        if not owner or not repo:
            return "Error: owner and repo required"
        params: dict[str, Any] = {"per_page": min(int(kwargs.get("per_page") or 10), 30)}
        if kwargs.get("sha"):
            params["sha"] = kwargs["sha"]
        data = _gh_get(token, f"/repos/{owner}/{repo}/commits", params)
        if isinstance(data, str):
            return data
        lines = []
        for c in data:
            sha = (c.get("sha") or "")[:7]
            msg = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
            author = ((c.get("commit") or {}).get("author") or {}).get("name") or "?"
            lines.append(f"- {sha} {msg} ({author})")
        return f"{len(lines)} commit(s):\n" + "\n".join(lines) if lines else "No commits."


class _GitHubListBranches(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_branches") -> None:
        super().__init__(
            user_id,
            name,
            "List branches in a repository.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        if not owner or not repo:
            return "Error: owner and repo required"
        data = _gh_get(token, f"/repos/{owner}/{repo}/branches", {"per_page": 50})
        if isinstance(data, str):
            return data
        lines = [f"- {b.get('name')} ({(b.get('commit') or {}).get('sha', '')[:7]})" for b in data]
        return f"{len(lines)} branch(es):\n" + "\n".join(lines) if lines else "No branches."


class _GitHubCreatePR(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_create_pull_request") -> None:
        super().__init__(
            user_id,
            name,
            "Create a pull request.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "title": {"type": "string", "description": "PR title", "required": True},
                "head": {"type": "string", "description": "Branch with changes", "required": True},
                "base": {"type": "string", "description": "Target branch (e.g. main)", "required": True},
                "body": {"type": "string", "description": "PR description", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner = (kwargs.get("owner") or "").strip()
        repo = (kwargs.get("repo") or "").strip()
        title = (kwargs.get("title") or "").strip()
        head = (kwargs.get("head") or "").strip()
        base = (kwargs.get("base") or "").strip()
        if not all([owner, repo, title, head, base]):
            return "Error: owner, repo, title, head, and base are required"
        body = {
            "title": title,
            "head": head,
            "base": base,
            "body": kwargs.get("body") or "",
        }
        data = _gh_post(token, f"/repos/{owner}/{repo}/pulls", body)
        if isinstance(data, str):
            return data
        return f"Created PR #{data.get('number')}: {data.get('title')}\n{data.get('html_url')}"


class _GitHubListPRs(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_pull_requests") -> None:
        super().__init__(
            user_id,
            name,
            "List pull requests in a repository.",
            {
                "owner": {"type": "string", "description": "Repo owner", "required": True},
                "repo": {"type": "string", "description": "Repo name", "required": True},
                "state": {"type": "string", "description": "open | closed | all", "required": False},
                "per_page": {"type": "integer", "description": "Max 30", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        owner, repo = (kwargs.get("owner") or "").strip(), (kwargs.get("repo") or "").strip()
        if not owner or not repo:
            return "Error: owner and repo required"
        data = _gh_get(
            token,
            f"/repos/{owner}/{repo}/pulls",
            {
                "state": kwargs.get("state") or "open",
                "per_page": min(int(kwargs.get("per_page") or 15), 30),
            },
        )
        if isinstance(data, str):
            return data
        lines = [
            f"- #{p.get('number')} {p.get('title')} [{p.get('state')}] "
            f"{p.get('head', {}).get('ref')} → {p.get('base', {}).get('ref')}\n  {p.get('html_url')}"
            for p in data
        ]
        return f"{len(lines)} PR(s):\n" + "\n".join(lines) if lines else "No pull requests."


class _GitHubGetMe(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_get_me") -> None:
        super().__init__(
            user_id,
            name,
            "Get the authenticated GitHub user profile.",
            {},
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        data = _gh_get(token, "/user")
        if isinstance(data, str):
            return data
        return (
            f"Login: {data.get('login')}\n"
            f"Name: {data.get('name')}\n"
            f"URL: {data.get('html_url')}\n"
            f"Public repos: {data.get('public_repos')}  "
            f"Private: {data.get('total_private_repos', '?')}\n"
            f"Followers: {data.get('followers')}  Following: {data.get('following')}"
        )


class _GitHubSearchCode(_BaseGH):
    def __init__(self, user_id: str | None, name: str = "mcp_github_search_code") -> None:
        super().__init__(
            user_id,
            name,
            "Search code on GitHub. Example: repo:owner/name filename:README extension:md",
            {
                "query": {"type": "string", "description": "Code search query", "required": True},
                "per_page": {"type": "integer", "description": "Max 20", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _need_token(self.user_id)
        if not token:
            return _auth_err()
        q = (kwargs.get("query") or "").strip()
        if not q:
            return "Error: query is required"
        data = _gh_get(
            token,
            "/search/code",
            {"q": q, "per_page": min(int(kwargs.get("per_page") or 10), 20)},
        )
        if isinstance(data, str):
            return data
        items = data.get("items") or []
        lines = [
            f"- {i.get('repository', {}).get('full_name')}/{i.get('path')}\n  {i.get('html_url')}"
            for i in items
        ]
        total = data.get("total_count", len(items))
        return f"Code search ({total} total, showing {len(lines)}):\n" + "\n".join(lines) if lines else f"No code match: {q}"


# Canonical tools + aliases models may invent
_TOOL_FACTORIES = [
    (_GitHubListRepos, ["mcp_github_list_repos", "mcp__github__list_repos", "mcp__github__list_repositories"]),
    (_GitHubSearchRepos, ["mcp_github_search_repositories", "mcp__github__search_repositories"]),
    (_GitHubCreateRepo, ["mcp_github_create_repository", "mcp__github__create_repository", "mcp_github_create_repo", "mcp__github__create_repo"]),
    (_GitHubGetRepo, ["mcp_github_get_repository", "mcp__github__get_repository", "mcp_github_get_repo"]),
    (_GitHubListIssues, ["mcp_github_list_issues", "mcp__github__list_issues"]),
    (_GitHubCreateIssue, ["mcp_github_create_issue", "mcp__github__create_issue"]),
    (_GitHubGetFile, ["mcp_github_get_file_contents", "mcp__github__get_file_contents", "mcp_github_get_file"]),
    (_GitHubPushFile, ["mcp_github_create_or_update_file", "mcp__github__create_or_update_file", "mcp_github_push_file"]),
    (_GitHubListCommits, ["mcp_github_list_commits", "mcp__github__list_commits"]),
    (_GitHubListBranches, ["mcp_github_list_branches", "mcp__github__list_branches"]),
    (_GitHubCreatePR, ["mcp_github_create_pull_request", "mcp__github__create_pull_request", "mcp_github_create_pr"]),
    (_GitHubListPRs, ["mcp_github_list_pull_requests", "mcp__github__list_pull_requests", "mcp_github_list_prs"]),
    (_GitHubGetMe, ["mcp_github_get_me", "mcp__github__get_me"]),
    (_GitHubSearchCode, ["mcp_github_search_code", "mcp__github__search_code"]),
]


def register_github_fallback_tools(registry: ToolRegistry, user_id: str | None) -> int:
    """Register static GitHub tools + name aliases. Returns count registered."""
    if not user_id:
        return 0
    if not _token_for(user_id):
        logger.info("github fallback skipped — no credential for user %s", user_id[:8])
        return 0

    n = 0
    for cls, names in _TOOL_FACTORIES:
        for name in names:
            try:
                if name not in registry:
                    registry.register(cls(user_id, name))
                    n += 1
            except ValueError:
                pass
            except Exception as exc:
                logger.warning("register fallback %s failed: %s", name, exc)
    logger.info("GitHub fallback tools registered: %d for user %s", n, user_id[:8])
    return n
