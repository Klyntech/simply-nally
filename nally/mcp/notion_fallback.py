"""Notion REST fallback — full agent capabilities without MCP OAuth.

Token sources (first wins):
  1. Vault credential for provider "notion"
  2. NOTION_TOKEN env (internal integration secret)

IMPORTANT — Notion access model:
  Internal integrations only see pages/databases that were *shared with the
  integration*. To cover a whole workspace, share top-level pages (or the
  workspace home) with the integration; children are then reachable via search
  and block APIs. There is no API to "see everything" without share/OAuth grant.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from nally.tools.base import Tool, ToolRegistry

logger = logging.getLogger(__name__)

_API = "https://api.notion.com/v1"
_NOTION_VERSION = os.getenv("NOTION_API_VERSION", "2022-06-28").strip() or "2022-06-28"


def _token_for(user_id: str | None) -> str | None:
    if user_id:
        try:
            from nally.vault import get_vault

            cred = get_vault().get_valid(user_id, "notion")
            if cred and cred.access_token:
                return cred.access_token
        except Exception as exc:
            logger.debug("vault notion: %s", exc)
        try:
            from nally.oauth.token_store import TokenStore

            t = TokenStore().get_valid(user_id, "notion")
            if t:
                return t.access_token
        except Exception:
            pass
    return os.getenv("NOTION_TOKEN", "").strip() or None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _req(method: str, token: str, path: str, **kwargs: Any) -> Any:
    r = requests.request(method, f"{_API}{path}", headers=_headers(token), timeout=45, **kwargs)
    if r.status_code >= 400:
        return f"Error: Notion API {r.status_code}: {r.text[:800]}"
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


def _auth_err() -> str:
    return (
        "Error: AUTH_REQUIRED: No Notion token. "
        "Set NOTION_TOKEN to an internal integration secret, share pages with that "
        "integration in Notion, then retry. (MCP browser OAuth is optional.)"
    )


def _rich_text(arr: list | None) -> str:
    if not arr:
        return ""
    return "".join((t.get("plain_text") or "") for t in arr if isinstance(t, dict))


def _title_from_props(props: dict) -> str:
    for v in (props or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return _rich_text(v.get("title"))
    return ""


def _rt(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content[:2000]}}]


class _Base(Tool):
    def __init__(self, user_id: str | None, name: str, description: str, parameters: dict) -> None:
        super().__init__(name=name, description=description, parameters=parameters)
        self.user_id = user_id

    def _tok(self) -> str | None:
        return _token_for(self.user_id)


# ----- search / inventory ---------------------------------------------------


class _Search(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_search") -> None:
        super().__init__(
            user_id,
            name,
            "Search all Notion pages and databases visible to the integration. "
            "Empty query lists recent accessible items.",
            {
                "query": {"type": "string", "description": "Search text (optional)", "required": False},
                "filter": {"type": "string", "description": "page | database", "required": False},
                "page_size": {"type": "integer", "description": "1-100", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        body: dict[str, Any] = {"page_size": min(int(kwargs.get("page_size") or 50), 100)}
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
                title = _title_from_props(r.get("properties") or {}) or "(untitled)"
                lines.append(f"- page | {title}\n  id={rid}\n  {r.get('url')}")
            elif obj == "database":
                title = _rich_text(r.get("title") or []) or "(untitled db)"
                lines.append(f"- database | {title}\n  id={rid}\n  {r.get('url')}")
            else:
                lines.append(f"- {obj} id={rid}")
        has_more = data.get("has_more")
        footer = f"\n(has_more={has_more}, next_cursor={data.get('next_cursor')})" if has_more else ""
        if not lines:
            return (
                "No Notion pages/databases visible. "
                "Share pages with the integration: open page → ⋯ → Connections → add integration."
                + footer
            )
        return f"Found {len(lines)} item(s):{footer}\n" + "\n".join(lines)


class _AccessHelp(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_access_status") -> None:
        super().__init__(
            user_id,
            name,
            "Check Notion token and how many pages/databases the integration can see. "
            "Explains how to grant access to more of the workspace.",
            {},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        data = _req("POST", token, "/search", json={"page_size": 100})
        if isinstance(data, str):
            return data
        results = data.get("results") or []
        pages = sum(1 for r in results if r.get("object") == "page")
        dbs = sum(1 for r in results if r.get("object") == "database")
        return (
            f"Notion token OK.\n"
            f"Visible in first search page: {pages} page(s), {dbs} database(s) "
            f"(has_more={data.get('has_more')}).\n\n"
            "Notion does not allow 'all pages in workspace' without sharing.\n"
            "To expand access:\n"
            "1. Open each top-level page (or workspace home) in Notion\n"
            "2. ⋯ → Connections → connect your integration\n"
            "3. Prefer sharing parent pages so children are included\n"
            "4. Re-run search after sharing"
        )


# ----- pages ----------------------------------------------------------------


class _GetPage(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_get_page") -> None:
        super().__init__(
            user_id,
            name,
            "Get Notion page metadata by id.",
            {"page_id": {"type": "string", "description": "Page UUID", "required": True}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        pid = (kwargs.get("page_id") or "").strip()
        if not pid:
            return "Error: page_id required"
        data = _req("GET", token, f"/pages/{pid}")
        if isinstance(data, str):
            return data
        title = _title_from_props(data.get("properties") or {})
        return (
            f"Page: {title or '(untitled)'}\n"
            f"id: {data.get('id')}\nurl: {data.get('url')}\n"
            f"archived: {data.get('archived')}\n"
            f"created: {data.get('created_time')}\n"
            f"last_edited: {data.get('last_edited_time')}"
        )


class _GetBlocks(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_get_block_children") -> None:
        super().__init__(
            user_id,
            name,
            "Read page/block content (child blocks).",
            {
                "block_id": {"type": "string", "description": "Page or block UUID", "required": True},
                "page_size": {"type": "integer", "description": "Max 100", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        bid = (kwargs.get("block_id") or "").strip()
        if not bid:
            return "Error: block_id required"
        data = _req(
            "GET",
            token,
            f"/blocks/{bid}/children",
            params={"page_size": min(int(kwargs.get("page_size") or 50), 100)},
        )
        if isinstance(data, str):
            return data
        lines = []
        for b in data.get("results") or []:
            btype = b.get("type")
            payload = b.get(btype) or {}
            text = _rich_text(payload.get("rich_text") or payload.get("text") or [])
            if not text and btype == "child_page":
                text = str(payload.get("title") or "")[:200]
            if not text and btype == "child_database":
                text = str(payload.get("title") or "")[:200]
            lines.append(f"- [{btype}] {text or b.get('id')}")
        return f"{len(lines)} block(s):\n" + "\n".join(lines) if lines else "No blocks."


class _AppendBlocks(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_append_block_children") -> None:
        super().__init__(
            user_id,
            name,
            "Append paragraph text blocks to a page or block.",
            {
                "block_id": {"type": "string", "description": "Parent page/block UUID", "required": True},
                "content": {
                    "type": "string",
                    "description": "Text to append (split into paragraphs on blank lines)",
                    "required": True,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        bid = (kwargs.get("block_id") or "").strip()
        content = (kwargs.get("content") or "").strip()
        if not bid or not content:
            return "Error: block_id and content required"
        children = []
        for para in content.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            children.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rt(para)},
                }
            )
        if not children:
            return "Error: no content paragraphs"
        data = _req("PATCH", token, f"/blocks/{bid}/children", json={"children": children[:100]})
        if isinstance(data, str):
            return data
        return f"Appended {len(data.get('results') or children)} block(s) to {bid}."


class _CreatePage(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_create_page") -> None:
        super().__init__(
            user_id,
            name,
            "Create a page under a parent page or as a database row.",
            {
                "parent_page_id": {"type": "string", "description": "Parent page UUID", "required": False},
                "parent_database_id": {"type": "string", "description": "Parent database UUID", "required": False},
                "title": {"type": "string", "description": "Title", "required": True},
                "content": {"type": "string", "description": "Optional body paragraph", "required": False},
                "title_property": {
                    "type": "string",
                    "description": "Database title property name (default Name)",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        title = (kwargs.get("title") or "").strip()
        if not title:
            return "Error: title required"
        parent_page = (kwargs.get("parent_page_id") or "").strip()
        parent_db = (kwargs.get("parent_database_id") or "").strip()
        if not parent_page and not parent_db:
            return "Error: parent_page_id or parent_database_id required"
        if parent_page:
            parent = {"type": "page_id", "page_id": parent_page}
            props: dict[str, Any] = {"title": {"title": _rt(title)}}
        else:
            parent = {"type": "database_id", "database_id": parent_db}
            prop_name = (kwargs.get("title_property") or "Name").strip() or "Name"
            props = {prop_name: {"title": _rt(title)}}
        body: dict[str, Any] = {"parent": parent, "properties": props}
        content = (kwargs.get("content") or "").strip()
        if content:
            body["children"] = [
                {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(content)}}
            ]
        data = _req("POST", token, "/pages", json=body)
        if isinstance(data, str):
            return data
        return f"Created page '{title}'\nid: {data.get('id')}\nurl: {data.get('url')}"


class _UpdatePage(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_update_page") -> None:
        super().__init__(
            user_id,
            name,
            "Update page title and/or archive flag.",
            {
                "page_id": {"type": "string", "description": "Page UUID", "required": True},
                "title": {"type": "string", "description": "New title (optional)", "required": False},
                "archived": {"type": "boolean", "description": "Archive if true", "required": False},
                "title_property": {
                    "type": "string",
                    "description": "Property name for title (default title)",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        pid = (kwargs.get("page_id") or "").strip()
        if not pid:
            return "Error: page_id required"
        body: dict[str, Any] = {}
        if kwargs.get("title") is not None and str(kwargs.get("title")).strip():
            prop = (kwargs.get("title_property") or "title").strip() or "title"
            body["properties"] = {prop: {"title": _rt(str(kwargs["title"]).strip())}}
        if kwargs.get("archived") is not None:
            body["archived"] = bool(kwargs["archived"])
        if not body:
            return "Error: provide title and/or archived"
        data = _req("PATCH", token, f"/pages/{pid}", json=body)
        if isinstance(data, str):
            return data
        return f"Updated page {data.get('id')}\nurl: {data.get('url')}\narchived: {data.get('archived')}"


# ----- databases ------------------------------------------------------------


class _GetDatabase(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_get_database") -> None:
        super().__init__(
            user_id,
            name,
            "Get database schema/metadata.",
            {"database_id": {"type": "string", "description": "Database UUID", "required": True}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        did = (kwargs.get("database_id") or "").strip()
        if not did:
            return "Error: database_id required"
        data = _req("GET", token, f"/databases/{did}")
        if isinstance(data, str):
            return data
        title = _rich_text(data.get("title") or [])
        props = data.get("properties") or {}
        prop_lines = [f"  - {name}: {meta.get('type')}" for name, meta in props.items()]
        return (
            f"Database: {title or '(untitled)'}\n"
            f"id: {data.get('id')}\nurl: {data.get('url')}\n"
            f"Properties:\n" + "\n".join(prop_lines)
        )


class _QueryDatabase(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_query_database") -> None:
        super().__init__(
            user_id,
            name,
            "Query database rows.",
            {
                "database_id": {"type": "string", "description": "Database UUID", "required": True},
                "page_size": {"type": "integer", "description": "Max 100", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        did = (kwargs.get("database_id") or "").strip()
        if not did:
            return "Error: database_id required"
        body = {"page_size": min(int(kwargs.get("page_size") or 30), 100)}
        data = _req("POST", token, f"/databases/{did}/query", json=body)
        if isinstance(data, str):
            return data
        lines = []
        for r in data.get("results") or []:
            title = _title_from_props(r.get("properties") or {}) or "(untitled)"
            lines.append(f"- {title}\n  id={r.get('id')}\n  {r.get('url')}")
        return f"{len(lines)} row(s):\n" + "\n".join(lines) if lines else "No rows."


class _CreateComment(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_create_comment") -> None:
        super().__init__(
            user_id,
            name,
            "Add a comment on a page.",
            {
                "page_id": {"type": "string", "description": "Page UUID", "required": True},
                "content": {"type": "string", "description": "Comment text", "required": True},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        pid = (kwargs.get("page_id") or "").strip()
        content = (kwargs.get("content") or "").strip()
        if not pid or not content:
            return "Error: page_id and content required"
        body = {
            "parent": {"page_id": pid},
            "rich_text": _rt(content),
        }
        data = _req("POST", token, "/comments", json=body)
        if isinstance(data, str):
            return data
        return f"Comment created id={data.get('id')} on page {pid}"


class _ListUsers(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_notion_list_users") -> None:
        super().__init__(
            user_id,
            name,
            "List workspace users (requires user capabilities on integration).",
            {"page_size": {"type": "integer", "description": "Max 100", "required": False}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        data = _req(
            "GET",
            token,
            "/users",
            params={"page_size": min(int(kwargs.get("page_size") or 50), 100)},
        )
        if isinstance(data, str):
            return data
        lines = [
            f"- {u.get('name') or '(no name)'} ({u.get('type')}) id={u.get('id')}"
            for u in (data.get("results") or [])
        ]
        return f"{len(lines)} user(s):\n" + "\n".join(lines) if lines else "No users."


_TOOL_FACTORIES = [
    (_Search, ["mcp_notion_search", "mcp__notion__search"]),
    (_AccessHelp, ["mcp_notion_access_status", "mcp__notion__access_status"]),
    (_GetPage, ["mcp_notion_get_page", "mcp__notion__get_page"]),
    (_GetBlocks, ["mcp_notion_get_block_children", "mcp__notion__get_block_children", "mcp_notion_get_page_content"]),
    (_AppendBlocks, ["mcp_notion_append_block_children", "mcp__notion__append_block_children", "mcp_notion_append_blocks"]),
    (_CreatePage, ["mcp_notion_create_page", "mcp__notion__create_page"]),
    (_UpdatePage, ["mcp_notion_update_page", "mcp__notion__update_page"]),
    (_GetDatabase, ["mcp_notion_get_database", "mcp__notion__get_database"]),
    (_QueryDatabase, ["mcp_notion_query_database", "mcp__notion__query_database"]),
    (_CreateComment, ["mcp_notion_create_comment", "mcp__notion__create_comment"]),
    (_ListUsers, ["mcp_notion_list_users", "mcp__notion__list_users"]),
]


def register_notion_fallback_tools(registry: ToolRegistry, user_id: str | None) -> int:
    if not _token_for(user_id):
        logger.info("notion fallback skipped — no token")
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
                logger.warning("notion fallback %s: %s", name, exc)
    logger.info("Notion fallback tools registered: %d", n)
    return n
