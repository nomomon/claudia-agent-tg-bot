"""Convert the Markdown a model writes into the HTML subset Telegram accepts.

Supported: bold, italic, inline and fenced code, links, headings (shown bold), and
bullet lists (shown with a bullet character). Anything else is shown as plain text.
"""

from __future__ import annotations

import html
import re

_FENCED = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC = re.compile(
    r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<!\w)_(?![\s_])(.+?)(?<![\s_])_(?!\w)"
)
_SLOT = re.compile("\x00(\\d+)\x00")


def _link(match: re.Match[str]) -> str:
    # The text is already HTML-escaped, so the URL only needs its quotes handled.
    url = match.group(2).replace('"', "&quot;")
    return f'<a href="{url}">{match.group(1)}</a>'


def to_telegram_html(markdown: str) -> str:
    slots: list[str] = []

    def hold(fragment: str) -> str:
        # Code is held aside so its contents are never formatted again.
        slots.append(fragment)
        return f"\x00{len(slots) - 1}\x00"

    text = _FENCED.sub(lambda m: hold(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), markdown)
    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(_link, text)
    text = _HEADING.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return _SLOT.sub(lambda m: slots[int(m.group(1))], text)
