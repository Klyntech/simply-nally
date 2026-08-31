"""Telegram-specific formatting utilities."""

from __future__ import annotations

import html
import re

TELEGRAM_MAX_LEN = 4096


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split text at 4096 code points, preferring newlines > spaces."""
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n ")
        if not remaining and not chunks[-1].strip():
            break
    return chunks


def telegram_format(text: str) -> str:
    """Convert LLM markdown to Telegram HTML.

    Handles:
      ```lang\\ncode``` -> <pre>code</pre>
      `inline` -> <code>inline</code>
      **bold** / __bold__ -> <b>bold</b>
      *italic* / _italic_ -> <i>italic</i>
      ~~strike~~ -> <s>strike</s>
      [text](url) -> <a href="url">text</a>
      ### heading -> <b>heading</b>
    Code blocks are protected and HTML-escaped.
    """
    if not text:
        return text

    blocks: dict[str, str] = {}
    inlines: dict[str, str] = {}

    def _save_block(m: re.Match[str]) -> str:
        key = f"\x00TG_BLOCK_{len(blocks)}\x00"
        code = m.group(1) or ""
        if code.endswith("\n"):
            code = code[:-1]
        escaped = html.escape(code, quote=False)
        blocks[key] = f"<pre>{escaped}</pre>"
        return key

    def _save_inline(m: re.Match[str]) -> str:
        key = f"\x00TG_INLINE_{len(inlines)}\x00"
        code = m.group(1) or ""
        escaped = html.escape(code, quote=False)
        inlines[key] = f"<code>{escaped}</code>"
        return key

    text = re.sub(r"```(?:\w*\n)?(.*?)```", _save_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+?)`", _save_inline, text)

    text = html.escape(text, quote=False)

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)

    for key, val in inlines.items():
        text = text.replace(key, val)
    for key, val in blocks.items():
        text = text.replace(key, val)

    return text
