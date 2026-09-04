"""Centralized HTTPS OAuth callback handler.

This module implements the single callback endpoint for all providers:

    https://nally.domain.com/oauth/callback/{provider}
        ?code=...&state=...

For local development:
    http://localhost:PORT/oauth/callback/{provider}

Flow:
    GET /oauth/callback/{provider}?code=...&state=...&error=...
        ↓
    lookup state in OAuthFlowStore (keyed by user_id + state)
        ↓
    But state alone doesn't tell us user_id.
    So we store state → flow lookup that can be searched without user_id,
    OR we embed user_id in state, OR we search all flows for matching state.

    For security, we search across all user flows for the matching state.
    Once found, we know user_id and can complete the exchange.

    state → find flow (any user) → get user_id → OAuthManager.callback()

For backwards compatibility, also handle:
    /notion/callback  (legacy Notion path)
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)


async def handle_oauth_callback(
    provider: str,
    query_params: dict[str, str] | Any,
    oauth_manager: Any,
) -> tuple[bool, str]:
    """Handle an OAuth callback.

    Args:
        provider: Provider name (github, gmail, notion)
        query_params: Dict with code, state, error
        oauth_manager: OAuthManager instance

    Returns:
        (success, message) tuple for HTTP response
    """
    # Normalize query_params
    if hasattr(query_params, "get"):
        code = query_params.get("code")
        state = query_params.get("state")
        error = query_params.get("error")
        error_description = query_params.get("error_description")
    else:
        code = None
        state = None
        error = None
        error_description = None

    if isinstance(code, list):
        code = code[0] if code else None
    if isinstance(state, list):
        state = state[0] if state else None
    if isinstance(error, list):
        error = error[0] if error else None

    if error:
        msg = error_description or error
        logger.warning("OAuth callback error for %s: %s (state=%s)", provider, msg, state)
        return False, f"Authorization failed: {msg}"

    if not code or not state:
        logger.warning("OAuth callback missing code/state for %s", provider)
        return False, "Missing code or state in callback"

    # Find the flow by searching all active flows for matching state
    # This is O(n) but n is small (active flows). For production, consider
    # indexing by state alone in addition to (user_id, state).
    target_flow = None
    target_user_id = None

    # Search through the flow store
    # We need to access internal _flows — use the manager's flow_store
    flow_store = oauth_manager._flow_store
    for (user_id, flow_state), flow in list(flow_store._flows.items()):
        if flow_state == state and flow.provider == provider:
            target_flow = flow
            target_user_id = user_id
            break
        # Also allow cross-provider match if provider doesn't matter (some providers don't include it in state)
        # But we already check provider

    # Fallback: if not found with provider match, try without provider check
    # (legacy flows may not have provider set correctly)
    if target_flow is None:
        for (user_id, flow_state), flow in list(flow_store._flows.items()):
            if flow_state == state:
                target_flow = flow
                target_user_id = user_id
                logger.warning(
                    "OAuth flow found without provider match: expected %s got %s for state %s",
                    provider,
                    flow.provider,
                    state[:8],
                )
                break

    if target_flow is None or target_user_id is None:
        logger.warning(
            "No OAuth flow found for state %s (provider %s)",
            state[:8] if state else "none",
            provider,
        )
        return False, "Invalid or expired OAuth state. Please try connecting again."

    # Complete the flow via OAuthManager
    try:
        result = await oauth_manager.callback(
            user_id=target_user_id,
            state=state,
            code=code,
            provider_name=provider,
        )
        logger.info("OAuth success for user %s provider %s", target_user_id, provider)
        return (
            True,
            f"Successfully connected {provider.capitalize()} as {result.token.account or 'user'}",
        )
    except Exception as exc:
        logger.warning("OAuth callback exchange failed for %s: %s", provider, exc)
        return False, f"Failed to complete authorization: {exc}"


def build_callback_url(provider: str, base_url: str | None = None) -> str:
    """Build the callback URL for a provider.

    Args:
        provider: Provider name
        base_url: Base URL (e.g., https://nally.example.com). If None, uses env.

    Returns:
        Full callback URL
    """
    import os

    if base_url is None:
        base_url = (
            os.getenv("OAUTH_BASE_URL", "").strip() or os.getenv("WEBHOOK_BASE_URL", "").strip()
        )

    if base_url:
        return f"{base_url.rstrip('/')}/oauth/callback/{provider}"

    # Local dev fallback
    port = os.getenv("OAUTH_CALLBACK_PORT", os.getenv("NOTION_CALLBACK_PORT", "8080"))
    return f"http://localhost:{port}/oauth/callback/{provider}"


# Local development callback server (optional, for CLI testing)
# This is NOT used in SaaS — SaaS uses the Starlette route above.
# But it's useful for local testing without deploying.


class LocalCallbackServer:
    """Simple HTTP server for local OAuth testing.

    Usage:
        server = LocalCallbackServer(oauth_manager)
        url = server.get_callback_url("github")
        # User visits url in browser, GitHub redirects here
        # Server captures code/state and completes flow
    """

    def __init__(self, oauth_manager: Any, port: int = 8080):
        self.oauth_manager = oauth_manager
        self.port = port
        self._server = None

    def get_callback_url(self, provider: str) -> str:
        return f"http://localhost:{self.port}/oauth/callback/{provider}"

    async def start(self):
        """Start the callback server."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        manager = self.oauth_manager

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                # Match /oauth/callback/{provider}
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "oauth" and parts[1] == "callback":
                    provider = parts[2]
                    qs = urllib.parse.parse_qs(parsed.query)
                    # Convert to simple dict
                    params = {k: v[0] if v else None for k, v in qs.items()}
                    import asyncio

                    # Run handle_oauth_callback in new loop
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        success, msg = loop.run_until_complete(
                            handle_oauth_callback(provider, params, manager)
                        )
                    except Exception as exc:
                        success, msg = False, str(exc)

                    self.send_response(200 if success else 400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    if success:
                        self.wfile.write(
                            f"<html><body><h1>Success!</h1><p>{msg}</p>"
                            "<p>You can close this tab.</p></body></html>".encode()
                        )
                    else:
                        self.wfile.write(
                            f"<html><body><h1>Error</h1><p>{msg}</p></body></html>".encode()
                        )
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        self._server = HTTPServer(("0.0.0.0", self.port), Handler)

        def serve():
            self._server.serve_forever()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        logger.info("Local OAuth callback server started on port %d", self.port)
        return thread

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
