"""User-facing copy and message formatting."""

from __future__ import annotations

import html

# (host fragment, display name, emoji)
_PLATFORMS: tuple[tuple[str, str, str], ...] = (
    ("youtube.com", "YouTube", "▶️"),
    ("youtu.be", "YouTube", "▶️"),
    ("instagram.com", "Instagram", "📸"),
    ("instagr.am", "Instagram", "📸"),
    ("tiktok.com", "TikTok", "🎵"),
    ("spotify.com", "Spotify", "🎧"),
    ("soundcloud.com", "SoundCloud", "🎧"),
    ("twitter.com", "X", "🐦"),
    ("x.com", "X", "🐦"),
    ("facebook.com", "Facebook", "📘"),
    ("fb.watch", "Facebook", "📘"),
    ("reddit.com", "Reddit", "🔴"),
    ("vimeo.com", "Vimeo", "🎬"),
    ("twitch.tv", "Twitch", "🟣"),
    ("music.apple.com", "Apple Music", "🎵"),
    ("deezer.com", "Deezer", "🎵"),
    ("bandcamp.com", "Bandcamp", "🎵"),
)

START_TEXT = (
    "👋 <b>Welcome!</b>\n\n"
    "Send a video or music link and I’ll download it for you.\n\n"
    "<b>Supported</b>\n"
    "▶️ YouTube · 📸 Instagram · 🎵 TikTok · 🐦 X · 🎧 SoundCloud\n\n"
    "<b>Groups</b>\n"
    "Add me to a group and paste links — no commands needed.\n\n"
    "<b>Commands</b>\n"
    "/help — how to use\n"
    "/about — bot info\n"
    "/cancel — stop the current download"
)


def start_text() -> str:
    from bot.version import format_version_label

    return f"{START_TEXT}\n\n<i>{format_version_label()}</i>"

HELP_TEXT = (
    "💡 <b>How to use</b>\n\n"
    "1. Paste a plain link in the chat\n"
    "2. Wait a few seconds\n"
    "3. Get your video, photo, or audio\n\n"
    "<b>Tips</b>\n"
    "• You can send several links in one message\n"
    "• Use /cancel to stop a download in progress\n"
    "• Linked text (not a plain URL) is ignored\n\n"
    "<b>Groups</b>\n"
    "1. In @BotFather: <code>/setprivacy</code> → Disable\n"
    "2. Remove and re-add the bot to the group\n"
    "3. Allow the bot to send messages\n\n"
    "<b>Supported</b>\n"
    "YouTube, Instagram, TikTok, X, and SoundCloud."
)

ABOUT_TEXT = (
    "ℹ️ <b>About</b>\n\n"
    "Downloads videos, photos, and music from "
    "YouTube, Instagram, TikTok, X, and SoundCloud.\n\n"
    "{version_block}\n\n"
    "<b>Limits</b>\n"
    "• Files up to 50 MB by default\n"
    "• Admins can enable larger uploads in /admin\n\n"
    "<b>Source</b>\n"
    '<a href="https://github.com/MasterAkbariDev/telegram-video-downloader-bot">'
    "GitHub</a>"
)


def about_text() -> str:
    from bot.version import format_version_block

    return ABOUT_TEXT.format(version_block=format_version_block(include_ytdlp=True))


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def detect_platform(url: str) -> tuple[str, str]:
    """Return (name, emoji) for a URL."""
    lower = url.lower()
    for fragment, name, emoji in _PLATFORMS:
        if fragment in lower:
            return name, emoji
    return "Link", "🔗"


def truncate_url(url: str, max_len: int = 52) -> str:
    url = url.strip()
    if len(url) <= max_len:
        return url
    return url[: max_len - 1] + "…"


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def link_header(url: str, index: int | None = None, total: int | None = None) -> str:
    name, emoji = detect_platform(url)
    if index is not None and total is not None and total > 1:
        return f"{emoji} <b>{name}</b>  ·  link {index}/{total}"
    return f"{emoji} <b>{name}</b>"


def status_message(
    url: str,
    step: str,
    *,
    index: int | None = None,
    total: int | None = None,
    title: str | None = None,
) -> str:
    header = link_header(url, index, total)
    lines = [header, "", step]
    if title:
        lines.extend(["", f"📝 {esc(title)}"])
    lines.extend(["", f"<code>{esc(truncate_url(url))}</code>"])
    return "\n".join(lines)


def connecting_status(
    url: str,
    *,
    name: str,
    emoji: str,
    elapsed_sec: int,
    phase: int = 0,
    index: int | None = None,
    total: int | None = None,
) -> str:
    """Rotating copy while yt-dlp / extract is still running."""
    phases = (
        f"{emoji} <b>Extracting…</b>",
        f"{emoji} <b>Extracting…</b> <i>({elapsed_sec}s)</i>",
        f"⬇️ <b>Preparing download…</b> <i>({elapsed_sec}s)</i>",
        f"⏳ <b>Still working…</b> <i>({elapsed_sec}s)</i>",
    )
    step = phases[min(phase, len(phases) - 1)]
    return status_message(url, step, index=index, total=total)


def batch_notice(count: int) -> str:
    return f"📎 Found <b>{count}</b> links — processing one by one…"


def caption(
    title: str,
    url: str,
    *,
    is_audio: bool = False,
    is_image: bool = False,
    file_size: int | None = None,
    album_count: int | None = None,
    uploader: str | None = None,
) -> str:
    name, emoji = detect_platform(url)
    if is_audio:
        kind = "Audio"
    elif is_image or (album_count and album_count > 0):
        kind = "Photos" if album_count and album_count > 1 else "Photo"
    else:
        kind = "Video"

    display_title = _caption_title(title, url, name)
    lines = [f"{emoji} <b>{esc(display_title)}</b>"]
    if uploader:
        lines.append(f"👤 {esc(_format_uploader(uploader))}")
    lines.append(f"{kind} from {name}")
    if album_count and album_count > 1:
        lines.append(f"🖼 {album_count} items")
    if file_size:
        lines.append(f"📦 {format_size(file_size)}")
    lines.append(f'<a href="{html.escape(url, quote=True)}">Open original</a>')
    return "\n".join(lines)


def _caption_title(title: str, url: str, platform_name: str) -> str:
    """Short clean title — avoid Instagram hashtag dumps in Telegram captions."""
    text = (title or "").strip()
    lower_url = url.lower()
    if "instagram.com" in lower_url or "instagr.am" in lower_url:
        if not text or text.count("#") >= 3 or len(text) > 80:
            return "Instagram post"
    if not text:
        return platform_name
    if len(text) > 100:
        return text[:97].rstrip() + "…"
    return text


def _format_uploader(uploader: str) -> str:
    name = uploader.strip()
    if name.startswith("@"):
        return name
    # TikTok / IG style handles without spaces look better with @
    if " " not in name and "/" not in name and len(name) <= 32:
        return f"@{name}"
    return name


def download_progress(
    downloaded: int,
    total: int | None,
    *,
    max_bytes: int | None = None,
) -> str:
    if total and total > 0:
        pct = min(100, downloaded / total * 100)
        bar = _progress_bar(pct)
        size_line = f"{format_size(downloaded)} / {format_size(total)}"
        if max_bytes and total > max_bytes:
            size_line += f" · max {format_size(max_bytes)}"
        return f"⬇️ <b>Downloading…</b> {pct:.0f}%\n{bar}\n{size_line}"
    return f"⬇️ <b>Downloading…</b> {format_size(downloaded)}"


def upload_progress(uploaded: int, total: int | None) -> str:
    if total and total > 0:
        pct = min(100, uploaded / total * 100)
        bar = _progress_bar(pct)
        return (
            f"📤 <b>Uploading…</b> {pct:.0f}%\n"
            f"{bar}\n"
            f"{format_size(uploaded)} / {format_size(total)}"
        )
    return f"📤 <b>Uploading…</b> {format_size(uploaded)}"


def error_message(url: str, reason: str, *, index: int | None = None, total: int | None = None) -> str:
    header = link_header(url, index, total)
    return (
        f"{header}\n\n"
        f"❌ <b>Could not download</b>\n"
        f"{esc(reason)}\n\n"
        f"💡 Try again or use a different link.\n\n"
        f"<code>{esc(truncate_url(url))}</code>"
    )


def cancelled_message(url: str, *, index: int | None = None, total: int | None = None) -> str:
    header = link_header(url, index, total)
    return (
        f"{header}\n\n"
        f"🛑 <b>Cancelled</b>\n\n"
        f"<code>{esc(truncate_url(url))}</code>"
    )


def unsupported_link_message(url: str | None = None) -> str:
    lines = [
        "❌ <b>Unsupported link</b>",
        "",
        "I only download from:",
        "▶️ YouTube · 📸 Instagram · 🎵 TikTok · 🐦 X · 🎧 SoundCloud",
    ]
    if url:
        lines.extend(["", f"<code>{esc(truncate_url(url))}</code>"])
    return "\n".join(lines)


def friendly_error(raw: str) -> str:
    lower = raw.lower()
    if (
        "sign in to confirm" in lower
        or "confirm you're not a bot" in lower
        or "confirm you are not a bot" in lower
        or ("not a bot" in lower and ("youtube" in lower or "cookie" in lower))
        or "youtube blocked this server" in lower
        or "spotify downloads need youtube" in lower
    ):
        if "cookies" in lower or "cookie" in lower:
            # Already a crafted hint from spotify/downloader
            return raw
        return (
            "YouTube blocked this server (bot check). "
            "For music, send a Spotify track or SoundCloud link instead. "
            "Optional: add YouTube cookies to data/cookies.txt for direct YouTube links."
        )
    if (
        "drm" in lower
        or "spotify" in lower and ("unsupported" in lower or "not supported" in lower)
    ):
        return (
            "Spotify tracks are DRM-protected. "
            "The bot searches YouTube for the same song — try again, or paste a YouTube link."
        )
    if "requested format is not available" in lower or "format is not available" in lower:
        return (
            "That video quality/format isn’t available from YouTube right now. "
            "Try again in a moment, or send a different link."
        )
    if "video unavailable" in lower or "this video is not available" in lower:
        return (
            "This YouTube video isn’t available "
            "(removed, private, or blocked in this region)."
        )
    if "too large" in lower or "exceeds" in lower or "50 mb" in lower:
        if "try a shorter" in lower or "telegram" in lower or "2 gb" in lower:
            return raw
        return (
            "This file is too large (max 50 MB). "
            "Admins can enable 2 GB uploads via /admin → 🔑 2 GB upload API."
        )
    if "private" in lower or "login" in lower or "sign in" in lower:
        return "This content is private or requires a login."
    if "410" in lower or " gone" in lower:
        return "This video was removed or is no longer available on the site."
    if "404" in lower or "not found" in lower:
        return "This video could not be found — the link may be broken or expired."
    if "403" in lower or "forbidden" in lower:
        return "Access to this content was denied by the site."
    if (
        "bot/age verification" in lower
        or "age or bot check" in lower
        or "bot verification" in lower
        or "cookies.txt" in lower and "automated access" in lower
    ):
        return (
            "This site blocked automated access (age or bot check). "
            "Open the link in your browser, pass the verification, then add "
            "cookies to <code>data/cookies.txt</code> (set COOKIES_FILE in .env)."
        )
    if "429" in lower or "too many requests" in lower or "rate limit" in lower:
        if "instagram" in lower:
            return (
                "Instagram is rate-limiting this server (too many requests). "
                "Wait a few minutes and try again. Admins: add a cookies.txt file "
                "(COOKIES_FILE in .env) to improve reliability."
            )
        return "The site is rate-limiting requests. Wait a few minutes and try again."
    if "500" in lower or "internal server error" in lower:
        return "The site's media API failed temporarily. Try again in a few minutes."
    if "geo" in lower or "not available in your country" in lower:
        return "This content is not available in your region."
    if "copyright" in lower:
        return "Blocked due to copyright restrictions."
    if "unsupported" in lower or "no video" in lower:
        return "This link type is not supported."
    if len(raw) > 180:
        return "Something went wrong. Please try again."
    return raw


def _progress_bar(pct: float, width: int = 12) -> str:
    filled = round(width * pct / 100)
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)
