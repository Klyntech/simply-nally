"""Static GitHub tools used when remote MCP discovery fails but a vault credential exists.

These call the GitHub REST API with the user's OAuth token so "list my repos"
works even if https://api.githubcopilot.com/mcp/ is unreachable or rejects the token.
"""

from __future__ import annotations

import json
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
    r = requests.get(
        f"{_API}{path}",
        headers=_headers(token),
        params=params or {},
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


def _gh_post(token: str, path: str, body: dict) -> Any:
    r = requests.post(
        f"{_API}{path}",
        headers=_headers(token),
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Error: GitHub API {r.status_code}: {r.text[:500]}"
    return r.json()


class _GitHubListRepos(Tool):
    def __init__(self, user_id: str | None, name: str = "mcp_github_list_repos") -> None:
        super().__init__(
            name=name,
            description="List GitHub repositories for the authenticated user (owned, member, or all).",
            parameters={
                "visibility": {
                    "type": "string",
                    "description": "all | public | private",
                    "required": False,
                },
                "affiliation": {
                    "type": "string",
                    "description": "owner, collaborator, organization_member (comma-separated)",
                    "required": False,
                },
                "per_page": {
                    "type": "integer",
                    "description": "Results per page (max 100)",
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return "Error: AUTH_REQUIRED: No GitHub credential. Connect via /mcp → Connect GitHub."
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
        if not lines:
            return "No repositories found."
        return f"Found {len(lines)} repo(s):\n" + "\n".join(lines)


class _GitHubSearchRepos(Tool):
    def __init__(self, user_id: str | None, name: str = "mcp_github_search_repositories") -> None:
        super().__init__(
            name=name,
            description="Search GitHub repositories. Use user:USERNAME to scope to a user.",
            parameters={
                "query": {
                    "type": "string",
                    "description": "GitHub search query (e.g. user:Klyntech or language:python stars:>10)",
                    "required": True,
                },
                "per_page": {
                    "type": "integer",
                    "description": "Results per page (max 30)",
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return "Error: AUTH_REQUIRED: No GitHub credential. Connect via /mcp → Connect GitHub."
        q = kwargs.get("query") or ""
        if not q.strip():
            return "Error: query is required"
        data = _gh_get(
            token,
            "/search/repositories",
            {"q": q, "per_page": min(int(kwargs.get("per_page") or 10), 30)},
        )
        if isinstance(data, str):
            return data
        items = data.get("items") or []
        lines = []
        for repo in items:
            lines.append(
                f"- {repo.get('full_name')} ⭐{repo.get('stargazers_count', 0)}\n"
                f"  {repo.get('html_url')}"
            )
        total = data.get("total_count", len(items))
        if not lines:
            return f"No repositories matched query: {q}"
        return f"Search results ({total} total, showing {len(lines)}):\n" + "\n".join(lines)



class _GitHubCreateRepo(Tool):
    def __init__(self, user_id: str | None, name: str = "mcp_github_create_repository") -> None:
        super().__init__(
            name=name,
            description="Create a new GitHub repository under the authenticated user.",
            parameters={
                "name": {
                    "type": "string",
                    "description": "Repository name (e.g. TESTNALLY)",
                    "required": True,
                },
                "description": {
                    "type": "string",
                    "description": "Optional short description",
                    "required": False,
                },
                "private": {
                    "type": "boolean",
                    "description": "If true, create a private repo (default false = public)",
                    "required": False,
                },
                "auto_init": {
                    "type": "boolean",
                    "description": "If true, initialize with a README (default true)",
                    "required": False,
                },
            },
        )
        self.user_id = user_id

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return "Error: AUTH_REQUIRED: No GitHub credential. Connect via /mcp → Connect GitHub."
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
            f"Created repository {data.get('full_name')}\n"
            f"URL: {data.get('html_url')}\n"
            f"Clone: {data.get('clone_url')}\n"
            f"Private: {data.get('private')}"
        )


def register_github_fallback_tools(registry: ToolRegistry, user_id: str | None) -> int:
    """Register static GitHub tools + aliases for old mcp__ naming.

    Returns number of tools registered.
    """
    if not user_id:
        return 0
    if not _token_for(user_id):
        logger.info("github fallback skipped — no credential for user %s", user_id[:8])
        return 0

    tools = [
        _GitHubListRepos(user_id, "mcp_github_list_repos"),
        _GitHubSearchRepos(user_id, "mcp_github_search_repositories"),
        _GitHubCreateRepo(user_id, "mcp_github_create_repository"),
        # Aliases for models that still emit the old double-underscore names
        _GitHubListRepos(user_id, "mcp__github__list_repos"),
        _GitHubSearchRepos(user_id, "mcp__github__search_repositories"),
        _GitHubListRepos(user_id, "mcp__github__list_repositories"),
        _GitHubCreateRepo(user_id, "mcp__github__create_repository"),
        _GitHubCreateRepo(user_id, "mcp_github_create_repo"),
        _GitHubCreateRepo(user_id, "mcp__github__create_repo"),
    ]
    n = 0
    for t in tools:
        try:
            if t.name not in registry:
                registry.register(t)
                n += 1
        except ValueError:
            pass
        except Exception as exc:
            logger.warning("register fallback %s failed: %s", t.name, exc)
    logger.info("GitHub fallback tools registered: %d for user %s", n, user_id[:8])
    return n
