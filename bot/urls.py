"""Extract plain media URLs from Telegram messages."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from telegram import Message, MessageEntity

# Platforms we intentionally trigger on in chat messages
_SUPPORTED_HOSTS = (
    "instagram.com",
    "instagr.am",
    "youtube.com",
    "youtu.be",
    "m.youtube.com",
    "music.youtube.com",
    "soundcloud.com",
    "on.soundcloud.com",
    "m.soundcloud.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    # Adult (yt-dlp + fallback extractors)
    "pornhub.com",
    "xvideos.com",
    "xhamster.com",
    "redtube.com",
    "xnxx.com",
    "spankbang.com",
    "eporner.com",
    "youporn.com",
    "tube8.com",
    "beeg.com",
    "beeg.site",
    "beeg.team",
)

# Plain http(s) URLs for supported hosts only
_PLAIN_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:"
    r"instagram\.com|instagr\.am|"
    r"youtube\.com|youtu\.be|m\.youtube\.com|music\.youtube\.com|"
    r"soundcloud\.com|on\.soundcloud\.com|m\.soundcloud\.com|"
    r"tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com|m\.tiktok\.com|"
    r"x\.com|twitter\.com|"
    r"pornhub\.com|xvideos\.com|xhamster\.com|redtube\.com|xnxx\.com|"
    r"spankbang\.com|eporner\.com|youporn\.com|tube8\.com|"
    r"beeg\.com|beeg\.site|beeg\.team"
    r")"
    r"[^\s<>\"']*",
    re.IGNORECASE,
)

# Bare domains (no scheme) sometimes pasted from mobile
_BARE_URL_RE = re.compile(
    r"(?:www\.)?"
    r"(?:"
    r"instagram\.com|instagr\.am|"
    r"youtube\.com|youtu\.be|"
    r"soundcloud\.com|"
    r"tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com|"
    r"x\.com|twitter\.com|"
    r"pornhub\.com|xvideos\.com|xhamster\.com|redtube\.com|xnxx\.com|"
    r"spankbang\.com|eporner\.com|youporn\.com|tube8\.com|"
    r"beeg\.com|beeg\.site|beeg\.team"
    r")"
    r"/[^\s<>\"']+",
    re.IGNORECASE,
)

# Any http(s) URL — used to detect unsupported links in DMs
_ANY_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def is_supported_url(url: str) -> bool:
    """True if URL is a supported media platform."""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in _SUPPORTED_HOSTS)


def extract_urls(text: str) -> list[str]:
    """Extract supported plain URLs from text (no Telegram entities)."""
    if not text:
        return []

    found: list[str] = []
    for match in _PLAIN_URL_RE.findall(text):
        found.append(normalize_url(match))
    for match in _BARE_URL_RE.findall(text):
        found.append(normalize_url(match))
    return _unique([u for u in found if is_supported_url(u)])


def extract_urls_from_message(message: Message) -> list[str]:
    """
    Extract only plain pasted links for supported platforms.

    Skips text-bound hyperlinks (MessageEntity.TEXT_LINK), where visible
    text is bound to a URL — those are not "plain links".
    """
    found: list[str] = []

    if message.text:
        found.extend(
            _plain_urls_from_text(message.text, message.entities)
        )
    if message.caption:
        found.extend(
            _plain_urls_from_text(message.caption, message.caption_entities)
        )

    return _unique([u for u in found if is_supported_url(u)])


def normalize_url(url: str) -> str:
    """Ensure URL has a scheme and trim trailing punctuation."""
    url = url.strip().rstrip(".,;:!?)>]}\"'")
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    if _BARE_URL_RE.match(url) or url.startswith("www."):
        return f"https://{url}"
    return url


def _plain_urls_from_text(
    text: str,
    entities: tuple[MessageEntity, ...] | None,
) -> list[str]:
    """
    Prefer Telegram URL entities (plain links).

    Ignore TEXT_LINK. Also ignore any regex match that overlaps a TEXT_LINK
    span, so bound hyperlink text never triggers a download.
    """
    urls: list[str] = []
    text_link_spans = _text_link_spans(entities)

    if entities:
        for entity in entities:
            if entity.type != MessageEntity.URL:
                continue
            if _overlaps_any(entity.offset, entity.length, text_link_spans):
                continue
            fragment = _entity_text(text, entity)
            if fragment:
                urls.append(normalize_url(fragment))

    # Fallback: regex for plain URLs not already covered, still skip text_link spans
    for match in _PLAIN_URL_RE.finditer(text):
        start, end = match.start(), match.end()
        utf16_start = _utf16_len(text[:start])
        utf16_len = _utf16_len(match.group(0))
        if _overlaps_any(utf16_start, utf16_len, text_link_spans):
            continue
        urls.append(normalize_url(match.group(0)))

    for match in _BARE_URL_RE.finditer(text):
        start, end = match.start(), match.end()
        utf16_start = _utf16_len(text[:start])
        utf16_len = _utf16_len(match.group(0))
        if _overlaps_any(utf16_start, utf16_len, text_link_spans):
            continue
        urls.append(normalize_url(match.group(0)))

    return urls


def _entity_text(text: str, entity: MessageEntity) -> str:
    """Slice text using Telegram UTF-16 offsets."""
    encoded = text.encode("utf-16-le")
    start = entity.offset * 2
    end = (entity.offset + entity.length) * 2
    try:
        return encoded[start:end].decode("utf-16-le")
    except Exception:
        return text[entity.offset : entity.offset + entity.length]


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _text_link_spans(
    entities: tuple[MessageEntity, ...] | None,
) -> list[tuple[int, int]]:
    if not entities:
        return []
    spans: list[tuple[int, int]] = []
    for entity in entities:
        if entity.type == MessageEntity.TEXT_LINK:
            spans.append((entity.offset, entity.offset + entity.length))
    return spans


def _overlaps_any(offset: int, length: int, spans: list[tuple[int, int]]) -> bool:
    end = offset + length
    for start, stop in spans:
        if offset < stop and end > start:
            return True
    return False


def _unique(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def extract_any_urls_from_message(message: Message) -> list[str]:
    """Extract any plain http(s) URLs (including unsupported hosts). Ignores TEXT_LINK."""
    found: list[str] = []
    texts = []
    if message.text:
        texts.append((message.text, message.entities))
    if message.caption:
        texts.append((message.caption, message.caption_entities))

    for text, entities in texts:
        text_link_spans = _text_link_spans(entities)
        if entities:
            for entity in entities:
                if entity.type != MessageEntity.URL:
                    continue
                if _overlaps_any(entity.offset, entity.length, text_link_spans):
                    continue
                fragment = _entity_text(text, entity)
                if fragment:
                    found.append(normalize_url(fragment))
        for match in _ANY_URL_RE.finditer(text):
            utf16_start = _utf16_len(text[: match.start()])
            utf16_len = _utf16_len(match.group(0))
            if _overlaps_any(utf16_start, utf16_len, text_link_spans):
                continue
            found.append(normalize_url(match.group(0)))
    return _unique(found)
