"""Spotify track download via SoundCloud / Invidious / Piped / YouTube (Spotify is DRM)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from bot.config import DOWNLOAD_DIR, YTDLP_PROXY

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], None]

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_INVIDIOUS_INSTANCES = (
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://inv.tux.pizza",
    "https://invidious.fdn.fr",
)

_PIPED_INSTANCES = (
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.nosebs.ru",
    "https://api.piped.private.coffee",
)


def is_spotify_url(url: str) -> bool:
    lower = url.lower()
    return "spotify.com" in lower or "open.spotify.com" in lower


def spotify_track_id(url: str) -> str | None:
    match = re.search(
        r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?(track|episode)/([a-zA-Z0-9]+)",
        url,
        re.IGNORECASE,
    )
    return match.group(2) if match else None


def fetch_spotify_meta(url: str) -> dict[str, str]:
    """Title/artist via Spotify oEmbed (no auth)."""
    oembed = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
    title = ""
    artist = ""
    try:
        with httpx.Client(
            headers={"User-Agent": _DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(oembed)
            if resp.status_code < 400:
                data = resp.json()
                title = (data.get("title") or "").strip()
                if " · " in title:
                    parts = [p.strip() for p in title.split(" · ", 1)]
                    title, artist = parts[0], parts[1]
                author = (data.get("author_name") or "").strip()
                if author and not artist:
                    artist = author
            if not title:
                page = client.get(url, headers={"User-Agent": _DESKTOP_UA})
                if page.status_code < 400:
                    title = _og(page.text, "og:title") or title
                    artist = _og(page.text, "og:description") or artist
                    if artist and "·" in artist:
                        artist = artist.split("·")[0].strip()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("Spotify metadata failed for %s: %s", url, exc)

    return {"title": title or "Spotify track", "artist": artist}


def youtube_search_query(meta: dict[str, str]) -> str:
    title = meta.get("title") or "track"
    artist = meta.get("artist") or ""
    if artist and artist.lower() not in title.lower():
        return f"{artist} - {title}"
    return title


def fetch_youtube_oembed_meta(url: str) -> dict[str, str]:
    """Title/artist via YouTube oEmbed (works even when the video is unplayable)."""
    video_id = _youtube_video_id(url)
    watch = (
        f"https://www.youtube.com/watch?v={video_id}"
        if video_id
        else url.replace("music.youtube.com", "www.youtube.com")
    )
    oembed = f"https://www.youtube.com/oembed?url={quote(watch, safe='')}&format=json"
    title = ""
    artist = ""
    try:
        with httpx.Client(
            headers={"User-Agent": _DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(oembed)
            if resp.status_code < 400:
                data = resp.json()
                title = (data.get("title") or "").strip()
                artist = (data.get("author_name") or "").strip()
                # Auto-generated Topic channels: "Artist - Topic"
                if artist.lower().endswith(" - topic"):
                    artist = artist[: -len(" - topic")].strip()
                elif artist.lower().endswith("-topic"):
                    artist = artist[: -len("-topic")].strip()
    except Exception as exc:
        logger.warning("YouTube oEmbed failed for %s: %s", url, exc)

    return {"title": title or "YouTube track", "artist": artist}


def _youtube_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|"
        r"music\.youtube\.com/watch\?v=)([a-zA-Z0-9_-]{6,})",
        url,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def is_youtube_unavailable_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return (
        "video unavailable" in text
        or "this video is not available" in text
        or "private video" in text
        or "has been removed" in text
    )


def resolve_unavailable_youtube(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    prefer_audio: bool = True,
):
    """When a YouTube / Music id is unplayable, find the same track elsewhere.

    Topic / Music catalog ids often return UNPLAYABLE while oEmbed still has
    title + artist — search SoundCloud, Piped/Invidious, then YouTube.
    """
    from bot.downloader import resolve_media

    if progress_callback:
        progress_callback("🎧 <b>Track unavailable — searching for a match…</b>")
    if cancel_check:
        cancel_check()

    meta = fetch_youtube_oembed_meta(url)
    query = youtube_search_query(meta)
    display = (
        f"{meta['artist']} - {meta['title']}".strip(" -")
        if meta.get("artist")
        else meta["title"]
    )
    label = f"{meta['artist']} — {meta['title']}" if meta.get("artist") else meta["title"]
    original_id = _youtube_video_id(url)
    errors: list[str] = []

    # 1) YouTube search — Topic catalog ids often have a matching channel upload
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback(f"🎧 <b>Searching YouTube…</b>\n<i>{_esc(label)}</i>")
    try:
        logger.info("Unavailable YouTube %s → ytsearch %r", url, query)
        return resolve_media(
            f"ytsearch5:{query}",
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            force_audio=prefer_audio,
            display_title=display,
        )
    except Exception as exc:
        errors.append(f"youtube: {exc}")
        logger.warning("YouTube search path failed: %s", exc)

    # 2) Piped / Invidious (search for an alternate upload — skip the dead id)
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback(f"🎧 <b>Trying YouTube mirrors…</b>\n<i>{_esc(label)}</i>")
    try:
        result = _resolve_via_mirrors(
            query,
            display_title=display,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            prefer_title=meta.get("title") or "",
            prefer_artist=meta.get("artist") or "",
            skip_video_ids={original_id} if original_id else None,
        )
        if result:
            return result
        errors.append("mirrors: no usable stream")
    except Exception as exc:
        errors.append(f"mirrors: {exc}")
        logger.warning("YouTube mirror path failed: %s", exc)

    # 3) SoundCloud
    if progress_callback:
        progress_callback(f"🎧 <b>Finding on SoundCloud…</b>\n<i>{_esc(label)}</i>")
    try:
        logger.info("Unavailable YouTube %s → SoundCloud %r", url, query)
        result = _resolve_via_soundcloud(
            query,
            meta=meta,
            display_title=display,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        if result:
            return result
        errors.append("soundcloud: no good match")
    except Exception as exc:
        errors.append(f"soundcloud: {exc}")
        logger.warning("YouTube SoundCloud path failed: %s", exc)

    raise RuntimeError(
        "This YouTube track isn’t available and no matching upload was found. "
        "Try a Spotify or SoundCloud link instead. "
        f"(details: {'; '.join(errors)[:400]})"
    )


def resolve_spotify_via_youtube(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Resolve Spotify: SoundCloud (best match) → mirrors → YouTube."""
    from bot.downloader import resolve_media

    if progress_callback:
        progress_callback("🎧 <b>Reading Spotify track info…</b>")
    if cancel_check:
        cancel_check()

    meta = fetch_spotify_meta(url)
    query = youtube_search_query(meta)
    display = (
        f"{meta['artist']} - {meta['title']}".strip(" -")
        if meta.get("artist")
        else meta["title"]
    )
    label = f"{meta['artist']} — {meta['title']}" if meta.get("artist") else meta["title"]

    errors: list[str] = []

    # 1) SoundCloud — pick best title/artist match (skip ads / promos)
    if progress_callback:
        progress_callback(f"🎧 <b>Finding on SoundCloud…</b>\n<i>{_esc(label)}</i>")
    try:
        logger.info("Spotify %s → SoundCloud search %r", url, query)
        result = _resolve_via_soundcloud(
            query,
            meta=meta,
            display_title=display,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        if result:
            return result
        errors.append("soundcloud: no good match")
    except Exception as exc:
        errors.append(f"soundcloud: {exc}")
        logger.warning("Spotify SoundCloud path failed: %s", exc)

    # 2) Piped / Invidious mirrors
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback(f"🎧 <b>Trying YouTube mirrors…</b>\n<i>{_esc(label)}</i>")
    try:
        result = _resolve_via_mirrors(
            query,
            display_title=display,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            prefer_title=meta.get("title") or "",
            prefer_artist=meta.get("artist") or "",
        )
        if result:
            return result
        errors.append("mirrors: no usable stream")
    except Exception as exc:
        errors.append(f"mirrors: {exc}")
        logger.warning("Spotify mirror path failed: %s", exc)

    # 3) YouTube via yt-dlp (often blocked on VPS — last resort)
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback(f"🎧 <b>Trying YouTube…</b>\n<i>{_esc(label)}</i>")
    try:
        logger.info("Spotify %s → YouTube search %r", url, query)
        return resolve_media(
            f"ytsearch5:{query}",
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            force_audio=True,
            display_title=display,
        )
    except Exception as exc:
        errors.append(f"youtube: {exc}")
        logger.warning("Spotify YouTube path failed: %s", exc)

    raise RuntimeError(
        "Could not find a matching song for this Spotify track. "
        "Paste a direct SoundCloud link if you have one. "
        f"(details: {'; '.join(errors)[:400]})"
    )


def _resolve_via_soundcloud(
    query: str,
    *,
    meta: dict[str, str],
    display_title: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
):
    """Search SoundCloud and download the best non-ad, non-DRM match."""
    import yt_dlp

    from bot.downloader import resolve_media
    from bot.config import get_cookies_file

    if cancel_check:
        cancel_check()

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": 20,
    }
    cookies = get_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY

    search = f"scsearch10:{query}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search, download=False)

    entries = [e for e in (info.get("entries") or []) if e]
    ranked = _rank_music_entries(
        entries,
        title=meta.get("title") or "",
        artist=meta.get("artist") or "",
    )
    if not ranked:
        return None

    last_exc: Exception | None = None
    for best in ranked[:6]:
        track_url = (
            best.get("webpage_url")
            or best.get("original_url")
            or best.get("url")
        )
        if not track_url or not str(track_url).startswith("http"):
            permalink = best.get("permalink") or best.get("id")
            if not permalink:
                continue
            track_url = str(permalink) if str(permalink).startswith("http") else None
            if not track_url:
                continue

        logger.info(
            "Trying SoundCloud candidate %r → %s",
            best.get("title"),
            track_url[:120],
        )
        try:
            return resolve_media(
                track_url,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                force_audio=True,
                display_title=display_title,
            )
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "drm" in msg:
                logger.warning("SoundCloud DRM, trying next: %s", best.get("title"))
                continue
            # Other hard failures — still try next candidates
            logger.warning("SoundCloud candidate failed (%s): %s", best.get("title"), exc)
            continue

    if last_exc:
        raise last_exc
    return None


_AD_TITLE_RE = re.compile(
    r"\b("
    r"advert(?:isement|ising)?|sponsored|promo(?:tion)?|"
    r"promote\s+your|get\s+discovered|buy\s+follows|"
    r"free\s+download\s+link|click\s+here|link\s+in\s+(bio|description)"
    r")\b",
    re.IGNORECASE,
)


def _is_ad_entry(entry: dict) -> bool:
    title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("creator") or entry.get("channel") or ""
    text = f"{title} {uploader}"
    if _AD_TITLE_RE.search(text):
        return True
    # Very short clips are usually ads / intros
    duration = entry.get("duration")
    try:
        if duration is not None and float(duration) < 45:
            # Allow short songs only if title looks like a real match (checked by score)
            if re.search(r"\b(ad|ads|promo|sponsor)\b", title, re.I):
                return True
    except (TypeError, ValueError):
        pass
    return False


def _token_set(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _rank_music_entries(
    entries: list[dict],
    *,
    title: str,
    artist: str,
) -> list[dict]:
    """Rank search hits against title/artist; skip ads. Best first."""
    want_title = _token_set(title)
    want_artist = _token_set(artist)
    want_all = want_title | want_artist

    scored: list[tuple[float, dict]] = []
    for entry in entries:
        if _is_ad_entry(entry):
            logger.info("Skipping SoundCloud ad-like hit: %s", entry.get("title"))
            continue

        etitle = entry.get("title") or ""
        uploader = entry.get("uploader") or entry.get("creator") or ""
        hay = _token_set(f"{etitle} {uploader}")
        if not hay:
            continue

        if not want_all:
            dur = entry.get("duration")
            try:
                if dur is not None and float(dur) < 45:
                    continue
            except (TypeError, ValueError):
                pass
            scored.append((1.0, entry))
            continue

        overlap_title = len(want_title & hay) / max(len(want_title), 1)
        overlap_artist = len(want_artist & hay) / max(len(want_artist), 1) if want_artist else 0
        overlap_all = len(want_all & hay) / max(len(want_all), 1)

        score = overlap_title * 60 + overlap_artist * 40 + overlap_all * 20

        tl = title.lower().strip()
        el = etitle.lower()
        if tl and tl in el:
            score += 35
        if artist and artist.lower() in el:
            score += 25
        if artist and artist.lower() in uploader.lower():
            score += 20

        duration = entry.get("duration")
        try:
            dur = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            dur = None
        if dur is not None:
            if dur < 45:
                score -= 50
            elif dur < 90:
                score -= 10
            elif 90 <= dur <= 600:
                score += 10

        if want_title and overlap_title < 0.34 and tl not in el:
            continue
        if score < 25:
            continue

        scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        logger.info(
            "SoundCloud ranked %d candidates; best score=%.1f title=%r",
            len(scored),
            scored[0][0],
            scored[0][1].get("title"),
        )
    return [entry for _score, entry in scored]


def _pick_best_music_entry(
    entries: list[dict],
    *,
    title: str,
    artist: str,
) -> dict | None:
    ranked = _rank_music_entries(entries, title=title, artist=artist)
    return ranked[0] if ranked else None


def _resolve_via_mirrors(
    query: str,
    *,
    display_title: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    prefer_title: str = "",
    prefer_artist: str = "",
    skip_video_ids: set[str] | None = None,
):
    video_id, video_title = _mirror_search(
        query,
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )
    if not video_id:
        return None

    audio_url, ext = _mirror_audio_url(video_id)
    if not audio_url:
        return None

    return _download_audio_result(
        audio_url,
        ext=ext,
        display_title=display_title or video_title or "Spotify track",
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )


def resolve_youtube_playlist_audio(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Download audio from a YouTube / YouTube Music playlist without yt-dlp YouTube."""
    list_id = _youtube_playlist_id(url)
    if not list_id:
        raise RuntimeError("Could not read this YouTube playlist id.")

    if progress_callback:
        progress_callback("🎧 <b>Reading playlist…</b>")

    video_id, title = _piped_playlist_first_video(list_id)
    if not video_id:
        video_id, title = _invidious_playlist_first_video(list_id)

    # YouTube Music album playlists (OLAK5…) often fail on mirrors — use page title → SoundCloud
    if not video_id:
        page_title = _fetch_page_title(url)
        if page_title:
            if progress_callback:
                progress_callback(
                    f"🎧 <b>Finding playlist on SoundCloud…</b>\n<i>{_esc(page_title)}</i>"
                )
            meta = {"title": page_title, "artist": ""}
            result = _resolve_via_soundcloud(
                page_title,
                meta=meta,
                display_title=page_title,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            if result:
                return result
        raise RuntimeError(
            "Could not read this YouTube Music playlist from mirrors. "
            "Send a Spotify track or a direct SoundCloud link instead."
        )

    if progress_callback:
        progress_callback(f"🎧 <b>Downloading first track…</b>\n<i>{_esc(title or video_id)}</i>")

    audio_url, ext = _mirror_audio_url(video_id)
    if audio_url:
        return _download_audio_result(
            audio_url,
            ext=ext,
            display_title=title or "YouTube track",
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    # Do NOT call yt-dlp YouTube here — VPS is bot-blocked. Use SoundCloud for the track title.
    if title:
        if progress_callback:
            progress_callback(f"🎧 <b>Finding track on SoundCloud…</b>\n<i>{_esc(title)}</i>")
        result = _resolve_via_soundcloud(
            title,
            meta={"title": title, "artist": ""},
            display_title=title,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        if result:
            return result

    raise RuntimeError(
        "YouTube blocked this server and mirrors had no audio stream. "
        "Send a Spotify track or SoundCloud link instead."
    )


def is_youtube_playlist_url(url: str) -> bool:
    lower = url.lower()
    if "list=" not in lower:
        return False
    return any(
        host in lower
        for host in (
            "youtube.com",
            "youtu.be",
            "music.youtube.com",
            "m.youtube.com",
        )
    )


def _youtube_playlist_id(url: str) -> str | None:
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def _download_audio_result(
    audio_url: str,
    *,
    ext: str,
    display_title: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
):
    from bot.downloader import MediaResult
    from bot.fast_download import download_http, make_client

    if progress_callback:
        progress_callback("⬇️ <b>Downloading audio…</b>")

    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"audio.{ext or 'm4a'}"

    try:
        with make_client() as client:
            download_http(
                client,
                audio_url,
                dest,
                referer="https://www.youtube.com/",
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        mp3 = _ensure_mp3(dest, cancel_check=cancel_check)
        return MediaResult(
            title=display_title,
            is_audio=True,
            file_size=mp3.stat().st_size,
            file_path=mp3,
            used_direct=False,
        )
    except Exception:
        _cleanup_dir(output_dir)
        raise


def _mirror_search(
    query: str,
    *,
    prefer_title: str = "",
    prefer_artist: str = "",
    skip_video_ids: set[str] | None = None,
) -> tuple[str | None, str]:
    vid, title = _piped_search(
        query,
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )
    if vid:
        return vid, title
    return _invidious_search(
        query,
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )


def _mirror_audio_url(video_id: str) -> tuple[str | None, str]:
    url, ext = _piped_audio_url(video_id)
    if url:
        return url, ext
    return _invidious_audio_url(video_id)


def _fetch_page_title(url: str) -> str:
    try:
        with httpx.Client(
            headers={"User-Agent": _DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                return ""
            title = _og(resp.text, "og:title") or ""
            if not title:
                match = re.search(r"<title[^>]*>([^<]+)</title>", resp.text, re.I)
                if match:
                    title = match.group(1).strip()
            title = re.sub(r"\s*[|\-–]\s*YouTube(?:\s*Music)?\s*$", "", title, flags=re.I)
            return title.strip()
    except httpx.HTTPError as exc:
        logger.debug("Page title fetch failed for %s: %s", url, exc)
        return ""


def _rank_video_candidates(
    items: list[dict],
    *,
    prefer_title: str,
    prefer_artist: str,
    skip_video_ids: set[str] | None = None,
) -> tuple[str | None, str]:
    """Pick best video id/title from Piped/Invidious search rows."""
    skip = skip_video_ids or set()
    scored: list[tuple[float, str, str]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        vid = row.get("videoId") or row.get("id") or row.get("url")
        if isinstance(vid, str) and vid.startswith("/watch?v="):
            vid = vid.split("v=", 1)[-1]
        if not isinstance(vid, str) or not re.fullmatch(r"[\w-]{6,}", vid):
            continue
        if vid in skip:
            continue
        title = (row.get("title") or "").strip()
        if _AD_TITLE_RE.search(title):
            continue
        fake = {
            "title": title,
            "uploader": row.get("uploaderName") or row.get("author") or row.get("uploader") or "",
            "duration": row.get("lengthSeconds") or row.get("duration"),
        }
        if prefer_title or prefer_artist:
            picked = _pick_best_music_entry(
                [fake],
                title=prefer_title or title,
                artist=prefer_artist,
            )
            if not picked:
                continue
        hay = _token_set(f"{title} {fake['uploader']}")
        want = _token_set(f"{prefer_title} {prefer_artist}") or hay
        score = len(want & hay) / max(len(want), 1) * 100
        if prefer_title and prefer_title.lower() in title.lower():
            score += 40
        scored.append((score, vid, title))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1], scored[0][2]

    for row in items:
        if not isinstance(row, dict):
            continue
        vid = row.get("videoId") or row.get("id") or row.get("url")
        if isinstance(vid, str) and vid.startswith("/watch?v="):
            vid = vid.split("v=", 1)[-1]
        if isinstance(vid, str) and re.fullmatch(r"[\w-]{6,}", vid) and vid not in skip:
            title = (row.get("title") or "").strip()
            if not _AD_TITLE_RE.search(title):
                return vid, title
    return None, ""


def _piped_search(
    query: str,
    *,
    prefer_title: str = "",
    prefer_artist: str = "",
    skip_video_ids: set[str] | None = None,
) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=15.0,
        follow_redirects=True,
    ) as client:
        for base in _PIPED_INSTANCES:
            try:
                resp = client.get(f"{base}/search", params={"q": query, "filter": "music_songs"})
                if resp.status_code >= 400:
                    resp = client.get(f"{base}/search", params={"q": query, "filter": "videos"})
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    continue
                vid, title = _rank_video_candidates(
                    items,
                    prefer_title=prefer_title,
                    prefer_artist=prefer_artist,
                    skip_video_ids=skip_video_ids,
                )
                if vid:
                    logger.info("Piped search %s → %s (%s)", base, vid, title)
                    return vid, title
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Piped search %s failed: %s", base, exc)
    return None, ""


def _piped_audio_url(video_id: str) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        for base in _PIPED_INSTANCES:
            try:
                resp = client.get(f"{base}/streams/{video_id}")
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                streams = data.get("audioStreams") or []
                best: tuple[int, str, str] | None = None
                for s in streams:
                    url = s.get("url")
                    if not url:
                        continue
                    br = int(s.get("bitrate") or 0)
                    mime = (s.get("mimeType") or s.get("format") or "").lower()
                    ext = "m4a" if "mp4" in mime or "m4a" in mime else "webm"
                    if best is None or br > best[0]:
                        best = (br, url, ext)
                if best:
                    logger.info("Piped streams %s @ %s", video_id, base)
                    return best[1], best[2]
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Piped streams %s @ %s failed: %s", video_id, base, exc)
    return None, ""


def _piped_playlist_first_video(list_id: str) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        for base in _PIPED_INSTANCES:
            try:
                resp = client.get(f"{base}/playlists/{list_id}")
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                related = data.get("relatedStreams") or data.get("videos") or []
                for row in related:
                    if not isinstance(row, dict):
                        continue
                    vid = row.get("url") or row.get("id") or row.get("videoId")
                    if isinstance(vid, str) and vid.startswith("/watch?v="):
                        vid = vid.split("v=", 1)[-1]
                    if isinstance(vid, str) and re.fullmatch(r"[\w-]{6,}", vid):
                        return vid, (row.get("title") or "").strip()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Piped playlist %s @ %s failed: %s", list_id, base, exc)
    return None, ""


def _invidious_search(
    query: str,
    *,
    prefer_title: str = "",
    prefer_artist: str = "",
    skip_video_ids: set[str] | None = None,
) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=15.0,
        follow_redirects=True,
    ) as client:
        for base in _INVIDIOUS_INSTANCES:
            try:
                resp = client.get(
                    f"{base}/api/v1/search",
                    params={"q": query, "type": "video"},
                )
                if resp.status_code >= 400:
                    continue
                rows = resp.json()
                if not isinstance(rows, list):
                    continue
                vid, title = _rank_video_candidates(
                    rows,
                    prefer_title=prefer_title,
                    prefer_artist=prefer_artist,
                    skip_video_ids=skip_video_ids,
                )
                if vid:
                    logger.info("Invidious search %s → %s (%s)", base, vid, title)
                    return vid, title
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                logger.debug("Invidious search %s failed: %s", base, exc)
    return None, ""


def _invidious_audio_url(video_id: str) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        for base in _INVIDIOUS_INSTANCES:
            try:
                resp = client.get(f"{base}/api/v1/videos/{video_id}")
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                candidates: list[tuple[int, str, str]] = []
                for fmt in data.get("adaptiveFormats") or []:
                    url = fmt.get("url")
                    if not url:
                        continue
                    mime = (fmt.get("type") or fmt.get("mimeType") or "").lower()
                    itag = str(fmt.get("itag") or "")
                    is_audio = "audio" in mime or itag in {
                        "139",
                        "140",
                        "141",
                        "249",
                        "250",
                        "251",
                        "599",
                        "600",
                    }
                    if not is_audio:
                        continue
                    bitrate = int(fmt.get("bitrate") or fmt.get("audioBitrate") or 0)
                    ext = "m4a" if "mp4" in mime or "m4a" in mime else "webm"
                    candidates.append((bitrate, url, ext))
                if candidates:
                    candidates.sort(key=lambda c: c[0], reverse=True)
                    return candidates[0][1], candidates[0][2]
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Invidious video %s @ %s failed: %s", video_id, base, exc)
    return None, ""


def _invidious_playlist_first_video(list_id: str) -> tuple[str | None, str]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        for base in _INVIDIOUS_INSTANCES:
            try:
                resp = client.get(f"{base}/api/v1/playlists/{list_id}")
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                for row in data.get("videos") or []:
                    vid = row.get("videoId")
                    if vid:
                        return str(vid), (row.get("title") or "").strip()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Invidious playlist %s @ %s failed: %s", list_id, base, exc)
    return None, ""


def _ensure_mp3(path: Path, *, cancel_check: CancelCheck | None) -> Path:
    if path.suffix.lower() == ".mp3":
        return path
    if not shutil.which("ffmpeg"):
        return path
    out = path.with_suffix(".mp3")
    if cancel_check:
        cancel_check()
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        logger.warning("ffmpeg mp3 convert failed: %s", (proc.stderr or "")[-300:])
        return path
    path.unlink(missing_ok=True)
    return out


def _cleanup_dir(directory: Path) -> None:
    try:
        if not directory.exists() or directory == DOWNLOAD_DIR:
            return
        for item in directory.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
        directory.rmdir()
    except OSError:
        pass


def _og(html: str, prop: str) -> str:
    match = re.search(
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
            html,
            re.IGNORECASE,
        )
    return match.group(1).strip() if match else ""


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
