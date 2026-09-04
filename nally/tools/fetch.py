"""Fetch tool — GET a URL and return readable text (stdlib only).

Includes basic SSRF protection: resolves the target hostname and blocks
private/reserved IP ranges before making the request.  Redirect following
is handled by urllib; we re-check the final URL's IP after the request.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSRF protection — block private / reserved / loopback ranges
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # loopback IPv4
    ipaddress.ip_network("10.0.0.0/8"),  # private class A
    ipaddress.ip_network("172.16.0.0/12"),  # private class B
    ipaddress.ip_network("192.168.0.0/16"),  # private class C
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),  # loopback IPv6
    ipaddress.ip_network("fc00::/7"),  # IPv6 private
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("0.0.0.0/8"),  # "this" network
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # documentation (TEST-NET-1)
    ipaddress.ip_network("198.51.100.0/24"),  # documentation (TEST-NET-2)
    ipaddress.ip_network("203.0.113.0/24"),  # documentation (TEST-NET-3)
]


def _is_blocked_ip(hostname: str) -> str | None:
    """Resolve hostname and return reason if IP is blocked, else None."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        return f"DNS resolution failed for {hostname}: {exc}"

    for _family, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                return f"IP {ip} is in blocked range {net} (SSRF protection)"
    return None


def _check_url_safety(url: str) -> str | None:
    """Full URL safety check. Returns reason string if blocked, else None."""
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return "could not parse hostname from URL"

    # Block localhost variants
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return f"hostname '{hostname}' is blocked (SSRF protection)"

    return _is_blocked_ip(hostname)


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------
def _strip_html(raw: str) -> str:
    """Very small HTML -> text extractor (no heavy deps)."""
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------
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

        # SSRF check
        ssrf_block = _check_url_safety(url)
        if ssrf_block:
            logger.warning("Fetch SSRF blocked: %s — %s", url[:200], ssrf_block)
            return f"Error: {ssrf_block}"

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
                # Check content type
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype and not any(x in ctype for x in ("text", "html", "xml", "json")):
                    return f"Error: unsupported content type: {ctype}"

                # Check final URL for SSRF (redirect could bypass initial check)
                final_url = resp.url
                if final_url != url:
                    final_ssrf = _check_url_safety(final_url)
                    if final_ssrf:
                        logger.warning(
                            "Fetch SSRF blocked redirect: %s -> %s — %s",
                            url[:200],
                            final_url[:200],
                            final_ssrf,
                        )
                        return f"Error: redirect to blocked address: {final_ssrf}"

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
