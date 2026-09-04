"""Gmail REST fallback when remote Gmail MCP discovery fails.

Uses vault OAuth token for provider "gmail" (or GMAIL_TOKEN env).
Requires scopes: gmail.readonly and ideally gmail.compose / gmail.modify.
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.text import MIMEText
from typing import Any

import requests

from nally.tools.base import Tool, ToolRegistry

logger = logging.getLogger(__name__)

_API = "https://gmail.googleapis.com/gmail/v1"


def _token_for(user_id: str | None) -> str | None:
    if user_id:
        try:
            from nally.vault import get_vault

            cred = get_vault().get_valid(user_id, "gmail")
            if cred and cred.access_token:
                return cred.access_token
        except Exception as exc:
            logger.debug("vault gmail: %s", exc)
        try:
            from nally.oauth.token_store import TokenStore

            t = TokenStore().get_valid(user_id, "gmail")
            if t:
                return t.access_token
        except Exception:
            pass
    for env in ("GMAIL_TOKEN", "GMAIL_OAUTH_TOKEN", "GOOGLE_GMAIL_TOKEN"):
        v = os.getenv(env, "").strip()
        if v:
            return v
    return None


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _req(method: str, token: str, path: str, **kwargs: Any) -> Any:
    r = requests.request(
        method,
        f"{_API}{path}",
        headers=_headers(token),
        timeout=30,
        **kwargs,
    )
    if r.status_code >= 400:
        return f"Error: Gmail API {r.status_code}: {r.text[:600]}"
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


def _auth_err() -> str:
    return (
        "Error: AUTH_REQUIRED: No Gmail token. Connect via /mcp → Connect Gmail "
        "(scopes gmail.readonly + gmail.compose)."
    )


def _header_map(payload: dict) -> dict[str, str]:
    headers = {}
    for h in (payload or {}).get("headers") or []:
        name = (h.get("name") or "").lower()
        if name:
            headers[name] = h.get("value") or ""
    return headers


def _decode_body(payload: dict) -> str:
    """Extract plain text (or html stripped lightly) from a message payload."""
    if not payload:
        return ""
    body = payload.get("body") or {}
    data = body.get("data")
    if data:
        try:
            raw = base64.urlsafe_b64decode(data + "==")
            return raw.decode("utf-8", errors="replace")
        except Exception:
            pass
    parts = payload.get("parts") or []
    texts = []
    for p in parts:
        mime = (p.get("mimeType") or "").lower()
        if mime == "text/plain":
            d = (p.get("body") or {}).get("data")
            if d:
                try:
                    texts.append(base64.urlsafe_b64decode(d + "==").decode("utf-8", errors="replace"))
                except Exception:
                    pass
        elif mime.startswith("multipart/"):
            nested = _decode_body(p)
            if nested:
                texts.append(nested)
    if texts:
        return "\n".join(texts)
    # fallback html
    for p in parts:
        if (p.get("mimeType") or "").lower() == "text/html":
            d = (p.get("body") or {}).get("data")
            if d:
                try:
                    return base64.urlsafe_b64decode(d + "==").decode("utf-8", errors="replace")[:8000]
                except Exception:
                    pass
    return ""


class _Base(Tool):
    def __init__(self, user_id: str | None, name: str, description: str, parameters: dict) -> None:
        super().__init__(name=name, description=description, parameters=parameters)
        self.user_id = user_id

    def _tok(self) -> str | None:
        return _token_for(self.user_id)


class _ListMessages(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_list_messages") -> None:
        super().__init__(
            user_id,
            name,
            "List recent Gmail messages (inbox by default). Returns id, subject, from, date, snippet.",
            {
                "query": {
                    "type": "string",
                    "description": "Gmail search query (e.g. newer_than:7d, from:boss@, is:unread)",
                    "required": False,
                },
                "max_results": {"type": "integer", "description": "1-25", "required": False},
                "label_ids": {
                    "type": "string",
                    "description": "Comma-separated label ids (default INBOX)",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        max_results = min(int(kwargs.get("max_results") or 10), 25)
        params: dict[str, Any] = {"maxResults": max_results}
        q = (kwargs.get("query") or "").strip()
        if q:
            params["q"] = q
        labels = (kwargs.get("label_ids") or "INBOX").strip()
        if labels:
            # Gmail API wants repeated labelIds; requests will encode list
            params["labelIds"] = [x.strip() for x in labels.split(",") if x.strip()]
        data = _req("GET", token, "/users/me/messages", params=params)
        if isinstance(data, str):
            return data
        msgs = data.get("messages") or []
        if not msgs:
            return "No messages found."
        lines = []
        for m in msgs:
            mid = m.get("id")
            meta = _req(
                "GET",
                token,
                f"/users/me/messages/{mid}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            if isinstance(meta, str):
                lines.append(f"- id={mid} (metadata error)")
                continue
            headers = _header_map(meta.get("payload") or {})
            lines.append(
                f"- id={mid}\n"
                f"  From: {headers.get('from', '?')}\n"
                f"  Subject: {headers.get('subject', '(no subject)')}\n"
                f"  Date: {headers.get('date', '?')}\n"
                f"  Snippet: {(meta.get('snippet') or '')[:160]}"
            )
        return f"{len(lines)} message(s):\n" + "\n".join(lines)


class _GetMessage(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_get_message") -> None:
        super().__init__(
            user_id,
            name,
            "Get full Gmail message by id (headers + body text).",
            {"message_id": {"type": "string", "description": "Gmail message id", "required": True}},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        mid = (kwargs.get("message_id") or "").strip()
        if not mid:
            return "Error: message_id required"
        data = _req("GET", token, f"/users/me/messages/{mid}", params={"format": "full"})
        if isinstance(data, str):
            return data
        headers = _header_map(data.get("payload") or {})
        body = _decode_body(data.get("payload") or {})
        if len(body) > 12000:
            body = body[:12000] + "\n... [truncated]"
        return (
            f"From: {headers.get('from', '?')}\n"
            f"To: {headers.get('to', '?')}\n"
            f"Subject: {headers.get('subject', '(no subject)')}\n"
            f"Date: {headers.get('date', '?')}\n"
            f"Labels: {', '.join(data.get('labelIds') or [])}\n\n"
            f"{body or data.get('snippet') or '(empty body)'}"
        )


class _SearchMessages(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_search_messages") -> None:
        super().__init__(
            user_id,
            name,
            "Search Gmail with a query string (same as Gmail search box).",
            {
                "query": {"type": "string", "description": "Gmail search query", "required": True},
                "max_results": {"type": "integer", "description": "1-25", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        # Reuse list with required query
        q = (kwargs.get("query") or "").strip()
        if not q:
            return "Error: query required"
        return _ListMessages(self.user_id).execute(
            query=q, max_results=kwargs.get("max_results") or 10, label_ids=""
        )


class _SendMessage(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_send_message") -> None:
        super().__init__(
            user_id,
            name,
            "Send an email via Gmail (requires gmail.compose or gmail.send scope).",
            {
                "to": {"type": "string", "description": "Recipient email", "required": True},
                "subject": {"type": "string", "description": "Subject", "required": True},
                "body": {"type": "string", "description": "Plain text body", "required": True},
                "cc": {"type": "string", "description": "CC (optional)", "required": False},
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        to = (kwargs.get("to") or "").strip()
        subject = (kwargs.get("subject") or "").strip()
        body = kwargs.get("body")
        if not to or not subject or body is None:
            return "Error: to, subject, and body are required"
        msg = MIMEText(str(body), _charset="utf-8")
        msg["to"] = to
        msg["subject"] = subject
        if kwargs.get("cc"):
            msg["cc"] = str(kwargs["cc"]).strip()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        data = _req("POST", token, "/users/me/messages/send", json={"raw": raw})
        if isinstance(data, str):
            return data
        return f"Sent message id={data.get('id')} to {to}"


class _GetProfile(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_get_profile") -> None:
        super().__init__(
            user_id,
            name,
            "Get Gmail profile (email address, messages total).",
            {},
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        data = _req("GET", token, "/users/me/profile")
        if isinstance(data, str):
            return data
        return (
            f"Email: {data.get('emailAddress')}\n"
            f"Messages total: {data.get('messagesTotal')}\n"
            f"Threads total: {data.get('threadsTotal')}\n"
            f"History id: {data.get('historyId')}"
        )


class _ModifyLabels(_Base):
    def __init__(self, user_id: str | None, name: str = "mcp_gmail_modify_message") -> None:
        super().__init__(
            user_id,
            name,
            "Add/remove labels on a message (e.g. mark read by removing UNREAD).",
            {
                "message_id": {"type": "string", "description": "Message id", "required": True},
                "add_labels": {
                    "type": "string",
                    "description": "Comma-separated label ids to add",
                    "required": False,
                },
                "remove_labels": {
                    "type": "string",
                    "description": "Comma-separated label ids to remove (e.g. UNREAD)",
                    "required": False,
                },
            },
        )

    def execute(self, **kwargs: Any) -> str:
        token = self._tok()
        if not token:
            return _auth_err()
        mid = (kwargs.get("message_id") or "").strip()
        if not mid:
            return "Error: message_id required"
        body: dict[str, Any] = {}
        add = (kwargs.get("add_labels") or "").strip()
        rem = (kwargs.get("remove_labels") or "").strip()
        if add:
            body["addLabelIds"] = [x.strip() for x in add.split(",") if x.strip()]
        if rem:
            body["removeLabelIds"] = [x.strip() for x in rem.split(",") if x.strip()]
        if not body:
            return "Error: provide add_labels and/or remove_labels"
        data = _req("POST", token, f"/users/me/messages/{mid}/modify", json=body)
        if isinstance(data, str):
            return data
        return f"Updated message {mid}. Labels: {', '.join(data.get('labelIds') or [])}"


_TOOL_FACTORIES = [
    (_ListMessages, ["mcp_gmail_list_messages", "mcp__gmail__list_messages", "mcp_gmail_list_emails"]),
    (_GetMessage, ["mcp_gmail_get_message", "mcp__gmail__get_message", "mcp_gmail_read_email"]),
    (_SearchMessages, ["mcp_gmail_search_messages", "mcp__gmail__search_messages", "mcp_gmail_search"]),
    (_SendMessage, ["mcp_gmail_send_message", "mcp__gmail__send_message", "mcp_gmail_send_email"]),
    (_GetProfile, ["mcp_gmail_get_profile", "mcp__gmail__get_profile"]),
    (_ModifyLabels, ["mcp_gmail_modify_message", "mcp__gmail__modify_message"]),
]


def register_gmail_fallback_tools(registry: ToolRegistry, user_id: str | None) -> int:
    if not _token_for(user_id):
        logger.info("gmail fallback skipped — no token")
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
                logger.warning("gmail fallback %s: %s", name, exc)
    logger.info("Gmail fallback tools registered: %d", n)
    return n
