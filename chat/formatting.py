"""Small, safe subset of Markdown for chat messages."""

import html
import re


FENCE = re.compile(r"(?ms)^```([\w+#.-]*)[ \t]*\n(.*?)^```[ \t]*$")
INLINE = re.compile(
    r"\[([^]\n]+)\]\((https?://[^\s)]+)\)|\*\*([^*\n]+)\*\*|"
    r"\*([^*\n]+)\*|`([^`\n]+)`"
)


def blocks(text):
    """Split fenced code from regular text without interpreting arbitrary HTML."""
    result = []
    position = 0
    for match in FENCE.finditer(text):
        if match.start() > position:
            result.append(("text", "", text[position:match.start()].strip("\n")))
        result.append(("code", match.group(1).lower(), match.group(2).rstrip("\n")))
        position = match.end()
    if position < len(text):
        result.append(("text", "", text[position:].strip("\n")))
    return result or [("text", "", "")]


def inline_markup(text):
    """Produce escaped Pango markup for bold, italic, code and HTTPS/HTTP links."""
    result = []
    position = 0
    for match in INLINE.finditer(text):
        result.append(html.escape(text[position:match.start()]))
        label, url, bold, italic, code = match.groups()
        if url:
            result.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        elif bold:
            result.append(f"<b>{html.escape(bold)}</b>")
        elif italic:
            result.append(f"<i>{html.escape(italic)}</i>")
        else:
            result.append(f'<span font_family="monospace">{html.escape(code)}</span>')
        position = match.end()
    result.append(html.escape(text[position:]))
    return "".join(result)
