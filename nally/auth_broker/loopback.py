"""CLI loopback callback server — 127.0.0.1 ephemeral port.

For `python main.py login` we spin a short-lived HTTP server on 127.0.0.1:0
and use that as redirect_uri. The provider redirects here, we capture
code/state and hand to AuthBroker.

Security: binds only to 127.0.0.1, narrow path, exact redirect_uri validation,
short-lived, closes after one callback or timeout.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class LoopbackServer:
    """Ephemeral loopback server for CLI auth."""

    def __init__(self, provider: str, state: str):
        self.provider = provider
        self.state = state
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.result: dict[str, Any] | None = None
        self._event = threading.Event()

    def start(self) -> str:
        """Start server on 127.0.0.1:0 and return redirect_uri."""
        provider = self.provider

        # Use closure to capture result
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                # Only accept /callback or /oauth/callback/{provider}
                if parsed.path not in ("/callback", f"/oauth/callback/{provider}", "/oauth/callback"):
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not found")
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                params = {k: v[0] for k, v in qs.items() if v}
                outer.result = params
                outer._event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if params.get("error"):
                    err = params.get("error_description", params.get("error"))
                    self.wfile.write(
                        f"<html><body><h1>Authorization failed</h1><p>{err}</p><p>You can close this window.</p></body></html>".encode()
                    )
                else:
                    self.wfile.write(
                        b"<html><body><h1>Connected</h1><p>You can close this window and return to the CLI.</p></body></html>"
                    )

            def log_message(self, format, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Loopback server listening on 127.0.0.1:%d for %s", port, provider)
        return f"http://127.0.0.1:{port}/callback"

    def wait(self, timeout: float = 180) -> dict[str, Any] | None:
        """Block until callback received or timeout. Returns query params or None."""
        ok = self._event.wait(timeout)
        self.stop()
        if not ok:
            return None
        return self.result

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
