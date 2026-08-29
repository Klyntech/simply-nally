"""Web search tool — DuckDuckGo (free, no key) with raw scrape fallback."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any

from .base import Tool


def _fallback_scrape(query: str, num_results: int) -> str:
    """Last resort: scrape lite.duckduckgo.com/lite/ with stdlib only."""
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request(
            "https://lite.duckduckgo.com/lite/",
            data=data,
            headers={"User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract result links/titles from lite DDG HTML
        # lite DDG uses <a href="...">title</a> with snippets in nearby <td>
        pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
        matches = pattern.findall(html)
        # Filter to actual result URLs (skip DDG internals)
        results: list[str] = []
        for href, title in matches:
            if "duckduckgo.com" in href or not href.startswith("http"):
                continue
            title = title.strip()
            if not title or len(title) < 3:
                continue
            # Unescape basic entities
            title = (
                title.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
            )
            href = href.replace("&amp;", "&")
            results.append(f"{title}\n{href}")
            if len(results) >= num_results:
                break

        if not results:
            return "No results found (fallback scrape returned empty)."

        return "\n\n".join(f"{i + 1}. {r}" for i, r in enumerate(results))

    except Exception as exc:
        return f"Error: fallback search failed: {type(exc).__name__}: {exc}"


class WebSearch(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description="Search the web and return titles, URLs and snippets. Use for current information, news, docs, or anything not in training data.",
            parameters={
                "query": {
                    "type": "string",
                    "description": "Search query",
                    "required": True,
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-5, default 3)",
                    "required": False,
                },
            },
        )

    def execute(self, query: str = "", num_results: int = 3, **kwargs: Any) -> str:  # type: ignore[override]
        if not query or not query.strip():
            return "Error: query must not be empty"
        if len(query) > 500:
            return "Error: query too long (max 500 chars)"

        # Clamp num_results
        try:
            n = int(num_results) if num_results is not None else 3
        except (ValueError, TypeError):
            n = 3
        n = max(1, min(5, n))
        q = query.strip()

        # Try DDGS library first (new name `ddgs`, fallback to legacy `duckduckgo_search`)
        def _ddg_search(q: str, n: int):
            try:
                from ddgs import DDGS as _DDGS  # type: ignore

                with _DDGS() as ddgs:
                    return list(ddgs.text(q, max_results=n))
            except ImportError:
                from duckduckgo_search import DDGS as _DDGS2  # type: ignore

                with _DDGS2() as ddgs:
                    return list(ddgs.text(q, max_results=n))

        try:
            results = _ddg_search(q, n)

            if not results:
                return "No results found."

            lines: list[str] = []
            for i, r in enumerate(results, 1):
                title = (r.get("title") or "").strip()
                href = (r.get("href") or r.get("link") or "").strip()
                body = (r.get("body") or r.get("snippet") or "").strip()
                # Clean
                body = body.replace("\n", " ").strip()
                if len(body) > 300:
                    body = body[:300] + "..."
                lines.append(f"{i}. {title}\n{href}\n{body}")

            return "\n\n".join(lines)

        except ImportError:
            # Library not installed — use fallback scrape
            return _fallback_scrape(q, n)
        except Exception as exc:
            # Library failed (network, rate limit, etc.) — try fallback
            fallback = _fallback_scrape(q, n)
            if "Error: fallback" not in fallback and "No results" not in fallback:
                return fallback
            return f"Error: web search failed: {type(exc).__name__}: {exc}\nFallback: {fallback}"


def register_web_tools(registry) -> None:
    registry.register(WebSearch())
