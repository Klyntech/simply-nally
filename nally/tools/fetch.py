"""Fetch tool — GET a URL and return readable text (stdlib only)."""

from __future__ import annotations

import html as html_lib
import re
import urllib.error
import urllib.request
from typing import Any

from .base import Tool


def _strip_html(raw: str) -> str:
    """Very small HTML → text extractor (no heavy deps)."""
    # Remove script/style blocks
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # Remove HTML comments
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # Replace tags with space (keep words separated)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode entities (&amp; etc.)
    text = html_lib.unescape(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n", "\n\n", text)
    # Trim lines
    lines = [line.strip() for line in text.splitlines()]
    # Drop empty lines at start/end and collapse multiples
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


class Fetch(Tool):
    def __init__(self, timeout: int = 15, max_bytes: int = 500_000) -> None:
        super().__init__(
            name="fetch",
            description="Fetch a web page by URL and return its text content. Use to read articles, docs, or any URL.",
            parameters={
                "url": {
                    "type": "string",
                    "description": "URL to fetch (must start with http:// or https://)",
                    "required": True,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max chars to return (default 15000, max 50000)",
                    "required": False,
                },
            },
        )
        self.timeout = timeout
        self.max_bytes = max_bytes

    def execute(self, url: str = "", max_chars: int = 15000, **kwargs: Any) -> str:  # type: ignore[override]
        if not url or not url.strip():
            return "Error: url must not be empty"
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "Error: url must start with http:// or https://"
        if len(url) > 2000:
            return "Error: url too long (max 2000 chars)"
        # Validate max_chars
        try:
            mc = int(max_chars) if max_chars is not None else 15000
        except (ValueError, TypeError):
            mc = 15000
        mc = max(100, min(50_000, mc))

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Simply NALLY fetch)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # Check content type — we only handle text/html and text/*
                ctype = resp.headers.get("Content-Type", "").lower()
                # Still try to read even if missing, but warn on binary
                if ctype and not any(x in ctype for x in ("text", "html", "xml", "json")):
                    return f"Error: unsupported content type: {ctype}"

                raw_bytes = resp.read(self.max_bytes + 1)
                if len(raw_bytes) > self.max_bytes:
                    return f"Error: response too large (>{self.max_bytes} bytes)"

        except urllib.error.HTTPError as exc:
            return f"Error: HTTP {exc.code} {exc.reason} for {url}"
        except urllib.error.URLError as exc:
            return f"Error: failed to fetch {url}: {exc.reason}"
        except TimeoutError:
            return f"Error: fetch timed out after {self.timeout}s for {url}"
        except Exception as exc:
            return f"Error: fetch failed for {url}: {type(exc).__name__}: {exc}"

        # Decode
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = raw_bytes.decode("latin-1", errors="ignore")

        # If JSON, return as-is (truncated)
        text = raw_text.strip() if "json" in ctype else _strip_html(raw_text)

        if not text.strip():
            return "Error: no readable text found at URL"

        if len(text) > mc:
            text = text[:mc] + f"\n... [truncated, {len(text)} chars total]"

        return text


def register_fetch_tools(registry) -> None:
    registry.register(Fetch())
