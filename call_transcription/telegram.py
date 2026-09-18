"""Pure HTML rendering: no network, Telegram tokens or database access."""
import html
import re


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def append_transcript_quote(header, transcript, *, limit=1024, full_url=None):
    lines = []
    speakers = transcript.get("speakers", {})
    for segment in transcript.get("segments", []):
        label = {"manager": "Менеджер", "client": "Клиент"}.get(segment.get("role"))
        if not label:
            label = speakers.get(segment.get("speaker_id"), {}).get("label", "speaker_unknown")
        lines.append(f"{label}: {segment.get('text', '')}")
    if not lines:
        return header
    heading = "\n\n📝 Расшифровка\n"
    text = "\n".join(lines)
    visible_header = html.unescape(re.sub(r"<[^>]+>", "", header))
    available = limit - utf16_length(visible_header + heading)
    footer_text = "\n… Фрагмент. Полный диалог"
    if utf16_length(text) > available:
        room = available - utf16_length(footer_text)
        if room < 20:
            return header
        text = text.encode("utf-16-le")[:room * 2].decode("utf-16-le", errors="ignore").rstrip()
        footer = html.escape(footer_text)
        if full_url:
            footer = "\n… Фрагмент. " + f'<a href="{html.escape(full_url, quote=True)}">Полный диалог</a>'
        quote = html.escape(text) + footer
    else:
        quote = html.escape(text)
    return header + heading + "<blockquote expandable>" + quote + "</blockquote>"
