"""Static Notion tools when remote MCP OAuth fails.

Uses the Notion REST API with either:
  1. Vault credential for provider "notion" (OAuth token if connect worked)
  2. NOTION_TOKEN env (internal integration secret — recommended fallback)

Share target pages/databases with the integration in Notion for access.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from nally.tools.base import Tool, ToolRegistry

logger = logging.getLogger(__name__)

_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _token_for(user_id: str | None) -> str | None:
    if user_id:
        try:
            from nally.vault import get_vault

            cred = get_vault().get_valid(user_id, "notion")
            if cred and cred.access_token:
                return cred.access_token
        except Exception as exc:
            logger.debug("vault notion token: %s", exc)
        try:
            from nally.oauth.token_store import TokenStore

            t = TokenStore().get_valid(user_id, "notion")
            if t:
                return t.access_token
        except Exception:
            pass
    # Env fallback: internal integration secret
    env = os.getenv("NOTION_TOKEN", "").strip()
    if env:
        return env
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _req(method: str, token: str, path: str, **kwargs: Any) -> Any:
    r = requests.request(
        method,
        f"{_API}{path}",
        headers=_headers(token),
        timeout=30,
        **kwargs,
    )
    if r.status_code >= 400:
        return f"Error: Notion API {r.status_code}: {r.text[:600]}"
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


def _auth_err() -> str:
    return (
        "Error: AUTH_REQUIRED: No Notion credential. "
        "Set NOTION_TOKEN (internal integration secret) on Render, "
        "or connect Notion via /mcp after OAuth works."
    )


def _rich_text(arr: list | None) -> str:
    if not arr:
        return ""
    parts = []
    for t in arr:
        if isinstance(t, dict):
            parts.append(t.get("plain_text") or "")
    return "".join(parts)


def _title_from_props(props: dict) -> str:
    for v in (props or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return _rich_text(v.get("title"))
    return ""


class _BaseNotion(Tool):
    def __init__(self, user_id: str | None, name: str, description: str, parameters: dict) -> None:
        super().__init__(name=name, description=description, parameters=parameters)
        self.user_id = user_id


class _NotionSearch(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_search") -> None:
        super().__init__(
            user_id,
            name,
            "Search Notion pages and databases by text query.",
            {
                "query": {"type": "string", "description": "Search text", "required": False},
                "filter": {
                    "type": "string",
                    "description": "page | database (optional)",
                    "required": False,
                },
                "page_size": {"type": "integer", "description": "Max 50", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        body: dict[str, Any] = {"page_size": min(int(kwargs.get("page_size") or 20), 50)}
        q = (kwargs.get("query") or "").strip()
        if q:
            body["query"] = q
        f = (kwargs.get("filter") or "").strip().lower()
        if f in ("page", "database"):
            body["filter"] = {"value": f, "property": "object"}
        data = _req("POST", token, "/search", json=body)
        if isinstance(data, str):
            return data
        lines = []
        for r in data.get("results") or []:
            obj = r.get("object")
            rid = r.get("id", "")
            if obj == "page":
                title = _title_from_props(r.get("properties") or {}) or "(untitled page)"
                url = r.get("url") or ""
                lines.append(f"- page: {title}\n  id: {rid}\n  {url}")
            elif obj == "database":
                title = _rich_text(r.get("title") or []) or "(untitled database)"
                url = r.get("url") or ""
                lines.append(f"- database: {title}\n  id: {rid}\n  {url}")
            else:
                lines.append(f"- {obj}: {rid}")
        return f"Found {len(lines)} result(s):\n" + "\n".join(lines) if lines else "No results."


class _NotionGetPage(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_get_page") -> None:
        super().__init__(
            user_id,
            name,
            "Get a Notion page by id (UUID).",
            {"page_id": {"type": "string", "description": "Page UUID", "required": True}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        page_id = (kwargs.get("page_id") or "").strip()
        if not page_id:
            return "Error: page_id is required"
        data = _req("GET", token, f"/pages/{page_id}")
        if isinstance(data, str):
            return data
        title = _title_from_props(data.get("properties") or {})
        return (
            f"Page: {title or '(untitled)'}\n"
            f"id: {data.get('id')}\n"
            f"url: {data.get('url')}\n"
            f"created: {data.get('created_time')}\n"
            f"last_edited: {data.get('last_edited_time')}"
        )


class _NotionGetPageContent(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_get_block_children") -> None:
        super().__init__(
            user_id,
            name,
            "List block children (content) of a page or block.",
            {
                "block_id": {
                    "type": "string",
                    "description": "Page or block UUID",
                    "required": True,
                },
                "page_size": {"type": "integer", "description": "Max 50", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        block_id = (kwargs.get("block_id") or "").strip()
        if not block_id:
            return "Error: block_id is required"
        params = {"page_size": min(int(kwargs.get("page_size") or 30), 50)}
        data = _req("GET", token, f"/blocks/{block_id}/children", params=params)
        if isinstance(data, str):
            return data
        lines = []
        for b in data.get("results") or []:
            btype = b.get("type")
            payload = b.get(btype) or {}
            text = _rich_text(payload.get("rich_text") or payload.get("text") or [])
            if not text and btype == "child_page":
                text = (payload.get("title") or "")[:120]
            lines.append(f"- [{btype}] {text or b.get('id')}")
        return f"{len(lines)} block(s):\n" + "\n".join(lines) if lines else "No blocks."


class _NotionQueryDatabase(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_query_database") -> None:
        super().__init__(
            user_id,
            name,
            "Query a Notion database by id.",
            {
                "database_id": {"type": "string", "description": "Database UUID", "required": True},
                "page_size": {"type": "integer", "description": "Max 50", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        db_id = (kwargs.get("database_id") or "").strip()
        if not db_id:
            return "Error: database_id is required"
        body = {"page_size": min(int(kwargs.get("page_size") or 20), 50)}
        data = _req("POST", token, f"/databases/{db_id}/query", json=body)
        if isinstance(data, str):
            return data
        lines = []
        for r in data.get("results") or []:
            title = _title_from_props(r.get("properties") or {}) or "(untitled)"
            lines.append(f"- {title}\n  id: {r.get('id')}\n  {r.get('url')}")
        return f"{len(lines)} row(s):\n" + "\n".join(lines) if lines else "No rows."


class _NotionCreatePage(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_create_page") -> None:
        super().__init__(
            user_id,
            name,
            "Create a page under a parent page or database.",
            {
                "parent_page_id": {
                    "type": "string",
                    "description": "Parent page UUID (xor with parent_database_id)",
                    "required": False,
                },
                "parent_database_id": {
                    "type": "string",
                    "description": "Parent database UUID",
                    "required": False,
                },
                "title": {"type": "string", "description": "Page title", "required": True},
                "content": {
                    "type": "string",
                    "description": "Optional plain-text paragraph body",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        title = (kwargs.get("title") or "").strip()
        if not title:
            return "Error: title is required"
        parent_page = (kwargs.get("parent_page_id") or "").strip()
        parent_db = (kwargs.get("parent_database_id") or "").strip()
        if not parent_page and not parent_db:
            return "Error: parent_page_id or parent_database_id is required"
        if parent_page:
            parent = {"type": "page_id", "page_id": parent_page}
            props: dict[str, Any] = {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            }
        else:
            parent = {"type": "database_id", "database_id": parent_db}
            # Common title property name; callers with custom schemas may need MCP
            props = {"Name": {"title": [{"type": "text", "text": {"content": title}}]}}
        body: dict[str, Any] = {"parent": parent, "properties": props}
        content = (kwargs.get("content") or "").strip()
        if content:
            body["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
                    },
                }
            ]
        data = _req("POST", token, "/pages", json=body)
        if isinstance(data, str):
            return data
        return f"Created page: {title}\nid: {data.get('id')}\nurl: {data.get('url')}"


class _NotionListUsers(_BaseNotion):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_list_users") -> None:
        super().__init__(
            user_id,
            name,
            "List users in the Notion workspace (integration must have user capability).",
            {"page_size": {"type": "integer", "description": "Max 50", "required": False}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = _token_for(self.user_id)
        if not token:
            return _auth_err()
        params = {"page_size": min(int(kwargs.get("page_size") or 20), 50)}
        data = _req("GET", token, "/users", params=params)
        if isinstance(data, str):
            return data
        lines = []
        for u in data.get("results") or []:
            lines.append(
                f"- {u.get('name') or '(no name)'} ({u.get('type')}) id={u.get('id')}"
            )
        return f"{len(lines)} user(s):\n" + "\n".join(lines) if lines else "No users."


_TOOL_FACTORIES = [
    (_NotionSearch, ["mcp_notion_search", "mcp__notion__search"]),
    (_NotionGetPage, ["mcp_notion_get_page", "mcp__notion__get_page"]),
    (_NotionGetPageContent, ["mcp_notion_get_block_children", "mcp__notion__get_block_children", "mcp_notion_get_page_content"]),
    (_NotionQueryDatabase, ["mcp_notion_query_database", "mcp__notion__query_database"]),
    (_NotionCreatePage, ["mcp_notion_create_page", "mcp__notion__create_page"]),
    (_NotionListUsers, ["mcp_notion_list_users", "mcp__notion__list_users"]),
]


def register_notion_fallback_tools(registry: ToolRegistry, user_id: str | None) -> int:
    """Register Notion REST tools if a token is available (vault or NOTION_TOKEN)."""
    if not _token_for(user_id):
        logger.info("notion fallback skipped — no token (vault or NOTION_TOKEN)")
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
                logger.warning("register notion fallback %s failed: %s", name, exc)
    logger.info("Notion fallback tools registered: %d", n)
    return n
