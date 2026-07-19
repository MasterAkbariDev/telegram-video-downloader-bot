"""Spotify / unavailable-YouTube music via parallel multi-source search (DRM bypass)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

_INVIDIOUS_INSTANCES: tuple[str, ...] = ()
# Public Invidious APIs currently return 401/403 or stream 500 with no audio.

_PIPED_INSTANCES: tuple[str, ...] = ()
# Public Piped APIs currently return 403/DNS failures, or streams with no audio.

_AUDIUS_HOSTS = (
    "https://discoveryprovider.audius.co",
    "https://api.audius.co",
)

# Prefer sources that can actually deliver audio right now
_SOURCE_BONUS = {
    "audius": 10.0,
    "soundcloud": 8.0,
}

_SEARCH_WORKERS = 3
_SEARCH_BUDGET_SEC = 10.0
_MAX_DOWNLOAD_TRIES = 8


@dataclass
class _MusicCandidate:
    source: str
    title: str
    uploader: str = ""
    duration: float | None = None
    url: str | None = None
    video_id: str | None = None
    score: float = 0.0
    stream_url: str | None = None
    stream_ext: str = "mp3"


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
    """Title/artist via Spotify oEmbed + embed page (no auth)."""
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

            # oEmbed often omits artist — embed page has full entity JSON
            if not title or not artist:
                kind, track_id = _spotify_kind_and_id(url)
                if track_id:
                    embed = client.get(
                        f"https://open.spotify.com/embed/{kind}/{track_id}",
                        headers={"User-Agent": _DESKTOP_UA},
                    )
                    if embed.status_code < 400:
                        etitle, eartist = _meta_from_spotify_embed(embed.text)
                        title = title or etitle
                        artist = artist or eartist

            if not title:
                page = client.get(url, headers={"User-Agent": _DESKTOP_UA})
                if page.status_code < 400:
                    title = _og(page.text, "og:title") or title
                    if not artist:
                        artist = _og(page.text, "og:description") or artist
                        if artist and "·" in artist:
                            artist = artist.split("·")[0].strip()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("Spotify metadata failed for %s: %s", url, exc)

    return {"title": title or "Spotify track", "artist": artist}


def _spotify_kind_and_id(url: str) -> tuple[str, str | None]:
    match = re.search(
        r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?(track|episode)/([a-zA-Z0-9]+)",
        url,
        re.IGNORECASE,
    )
    if not match:
        return "track", spotify_track_id(url)
    return match.group(1).lower(), match.group(2)


def _meta_from_spotify_embed(html: str) -> tuple[str, str]:
    """Parse title + artists from Spotify embed HTML / __NEXT_DATA__."""
    title = ""
    artists: list[str] = []
    next_data = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if next_data:
        try:
            payload = json.loads(next_data.group(1))
            entity = (
                payload.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity")
                or {}
            )
            title = (entity.get("name") or entity.get("title") or "").strip()
            for item in entity.get("artists") or []:
                name = (item.get("name") or "").strip()
                if name:
                    artists.append(name)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    if not artists:
        block = re.search(r'"artists"\s*:\s*\[(.*?)\]', html, re.DOTALL)
        if block:
            artists = [
                name
                for name in re.findall(r'"name"\s*:\s*"((?:\\.|[^"\\])*)"', block.group(1))
                if name
            ]
    if not title:
        m = re.search(r'"name"\s*:\s*"((?:\\.|[^"\\])*)"', html)
        if m:
            title = m.group(1).replace('\\"', '"').strip()

    artist = ", ".join(artists)
    return title, artist


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
    """When a YouTube / Music id is unplayable, find the same track elsewhere."""
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

    if progress_callback:
        progress_callback(
            f"🎧 <b>Finding track…</b>\n<i>{_esc(label)}</i>"
            if label
            else "🎧 <b>Finding track…</b>"
        )

    return _resolve_music_by_query(
        query,
        meta=meta,
        display_title=display,
        label=label,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        skip_video_ids={original_id} if original_id else None,
        allow_yt_search=prefer_audio,
        force_audio=prefer_audio,
        not_found_message=(
            "This YouTube track isn’t available and no matching upload was found. "
            "Try a Spotify or SoundCloud link instead."
        ),
    )


def resolve_spotify_via_youtube(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Resolve Spotify via parallel multi-source search (not SoundCloud-first)."""
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

    if progress_callback:
        progress_callback(
            f"🎧 <b>Finding track…</b>\n<i>{_esc(label)}</i>"
            if label
            else "🎧 <b>Finding track…</b>"
        )

    return _resolve_music_by_query(
        query,
        meta=meta,
        display_title=display,
        label=label,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        allow_yt_search=True,
        force_audio=True,
        not_found_message=(
            "Could not find a matching song for this Spotify track. "
            "Paste a direct SoundCloud link if you have one."
        ),
    )


def _resolve_music_by_query(
    query: str,
    *,
    meta: dict[str, str],
    display_title: str,
    label: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    skip_video_ids: set[str] | None = None,
    allow_yt_search: bool = True,
    force_audio: bool = True,
    not_found_message: str = "Could not find a matching song.",
):
    """Search sources in parallel, then download the best matches in score order."""
    from bot.downloader import resolve_media

    if cancel_check:
        cancel_check()

    prefer_title = meta.get("title") or ""
    prefer_artist = meta.get("artist") or ""
    candidates = _parallel_music_search(
        query,
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )
    errors: list[str] = []

    if candidates:
        logger.info(
            "Music search %r → %d candidates; best=%s score=%.1f %r",
            query,
            len(candidates),
            candidates[0].source,
            candidates[0].score,
            candidates[0].title,
        )
        candidates = _hydrate_mirror_streams(candidates[:_MAX_DOWNLOAD_TRIES])

    for cand in candidates[:_MAX_DOWNLOAD_TRIES]:
        if cancel_check:
            cancel_check()
        try:
            result = _download_music_candidate(
                cand,
                display_title=display_title,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                force_audio=force_audio,
            )
            if result:
                logger.info("Music resolved via %s: %r", cand.source, cand.title)
                return result
        except Exception as exc:
            errors.append(f"{cand.source}: {exc}")
            logger.warning(
                "Music candidate failed (%s %r): %s",
                cand.source,
                cand.title,
                exc,
            )

    if allow_yt_search:
        if cancel_check:
            cancel_check()
        try:
            logger.info("Music search fallback ytsearch %r", query)
            return resolve_media(
                f"ytsearch5:{query}",
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                force_audio=force_audio,
                display_title=display_title,
            )
        except Exception as exc:
            errors.append(f"youtube: {exc}")
            logger.warning("Music ytsearch fallback failed: %s", exc)

    detail = "; ".join(errors)[:400] if errors else "no candidates"
    raise RuntimeError(f"{not_found_message} (details: {detail})")


def _parallel_music_search(
    query: str,
    *,
    prefer_title: str,
    prefer_artist: str,
    skip_video_ids: set[str] | None = None,
) -> list[_MusicCandidate]:
    """Fan out search across sources; return merged ranked candidates."""
    import time
    from concurrent.futures import wait, FIRST_COMPLETED

    searchers = (
        ("soundcloud", lambda: _search_soundcloud_candidates(query, prefer_title, prefer_artist)),
        ("audius", lambda: _search_audius_candidates(query, prefer_title, prefer_artist)),
    )

    merged: list[_MusicCandidate] = []
    started = time.monotonic()
    deadline = started + _SEARCH_BUDGET_SEC
    with ThreadPoolExecutor(max_workers=_SEARCH_WORKERS) as pool:
        futures = {pool.submit(fn): name for name, fn in searchers}
        pending = set(futures)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                name = futures[fut]
                try:
                    found = fut.result() or []
                    logger.info("Music source %s returned %d hit(s)", name, len(found))
                    merged.extend(found)
                except Exception as exc:
                    logger.warning("Music source %s search failed: %s", name, exc)
            # Early exit after a short settle window once we have a strong match
            strong = _finalize_candidates(
                merged,
                prefer_title=prefer_title,
                prefer_artist=prefer_artist,
            )
            settled = (time.monotonic() - started) >= 2.5
            sources = {c.source for c in strong[:6]}
            if strong and strong[0].score >= 120 and (settled or len(sources) >= 2):
                for fut in pending:
                    fut.cancel()
                return strong
        for fut in pending:
            fut.cancel()

    return _finalize_candidates(merged, prefer_title=prefer_title, prefer_artist=prefer_artist)


def _finalize_candidates(
    candidates: list[_MusicCandidate],
    *,
    prefer_title: str,
    prefer_artist: str,
) -> list[_MusicCandidate]:
    """Re-score, dedupe, and sort candidates."""
    scored: list[_MusicCandidate] = []
    seen: set[str] = set()
    for cand in candidates:
        key = (cand.video_id or cand.url or cand.stream_url or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entry = {
            "title": cand.title,
            "uploader": cand.uploader,
            "duration": cand.duration,
        }
        ranked = _rank_music_entries(
            [entry],
            title=prefer_title or cand.title,
            artist=prefer_artist,
        )
        if not ranked and (prefer_title or prefer_artist):
            continue
        base = 40.0 if ranked else 20.0
        if ranked:
            # Reconstruct approximate score via token overlap again for sorting
            base = _score_music_entry(
                entry,
                title=prefer_title or cand.title,
                artist=prefer_artist,
            )
        cand.score = base + _SOURCE_BONUS.get(cand.source, 0.0)
        if cand.score < 20:
            continue
        scored.append(cand)

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def _score_music_entry(entry: dict, *, title: str, artist: str) -> float:
    ranked = _rank_music_entries([entry], title=title, artist=artist)
    if not ranked:
        return 0.0
    # _rank_music_entries doesn't return scores — recompute lightly
    want_title = _token_set(title)
    want_artist = _token_set(artist)
    want_all = want_title | want_artist
    etitle = entry.get("title") or ""
    uploader = entry.get("uploader") or ""
    hay = _token_set(f"{etitle} {uploader}")
    if not want_all:
        return 50.0
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
        elif dur > 900:
            score -= 40
    return score


def _hydrate_mirror_streams(
    candidates: list[_MusicCandidate],
) -> list[_MusicCandidate]:
    """Resolve Piped/Invidious audio URLs in parallel; drop dead mirror hits."""
    from concurrent.futures import wait as wait_futures

    # Only hydrate the best few mirrors — enough to pick a winner quickly
    mirror_budget = 3
    need: list[tuple[int, _MusicCandidate]] = []
    for i, c in enumerate(candidates):
        if c.video_id and not c.stream_url:
            need.append((i, c))
            if len(need) >= mirror_budget:
                break
    if not need:
        return candidates

    ready: dict[int, tuple[str, str]] = {}

    def _one(idx: int, video_id: str) -> None:
        url, ext = _mirror_audio_url(video_id)
        if url:
            ready[idx] = (url, ext or "m4a")

    with ThreadPoolExecutor(max_workers=min(3, len(need))) as pool:
        futs = [
            pool.submit(_one, idx, cand.video_id or "")
            for idx, cand in need
        ]
        wait_futures(futs, timeout=10)

    hydrated_idxs = {idx for idx, _ in need}
    out: list[_MusicCandidate] = []
    for i, cand in enumerate(candidates):
        if i in ready:
            cand.stream_url, cand.stream_ext = ready[i]
            out.append(cand)
        elif i in hydrated_idxs:
            logger.info("Dropping mirror candidate without stream: %r", cand.title)
            continue
        else:
            out.append(cand)
    return out


def _download_music_candidate(
    cand: _MusicCandidate,
    *,
    display_title: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    force_audio: bool,
):
    from bot.downloader import resolve_media

    if cand.stream_url:
        return _download_audio_result(
            cand.stream_url,
            ext=cand.stream_ext or "mp3",
            display_title=display_title,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    if cand.video_id:
        audio_url, ext = _mirror_audio_url(cand.video_id)
        if not audio_url:
            return None
        return _download_audio_result(
            audio_url,
            ext=ext,
            display_title=display_title,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    if cand.url:
        return resolve_media(
            cand.url,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            force_audio=force_audio,
            display_title=display_title,
        )
    return None


def _search_soundcloud_candidates(
    query: str,
    prefer_title: str,
    prefer_artist: str,
) -> list[_MusicCandidate]:
    import yt_dlp

    from bot.config import get_cookies_file

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        # Flat search is much faster — we only need urls/titles for ranking
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 12,
    }
    cookies = get_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch8:{query}", download=False)

    entries = [e for e in (info.get("entries") or []) if e]
    # Flat entries may only have url/title — normalize for ranking + download
    normalized: list[dict] = []
    for entry in entries:
        row = dict(entry)
        url = (
            row.get("webpage_url")
            or row.get("original_url")
            or row.get("url")
        )
        if url and not str(url).startswith("http"):
            # SoundCloud flat ids look like "artist/slug"
            url = f"https://soundcloud.com/{url}"
            row["webpage_url"] = url
        elif url:
            row["webpage_url"] = str(url)
        normalized.append(row)

    ranked = _rank_music_entries(
        normalized,
        title=prefer_title,
        artist=prefer_artist,
    )
    out: list[_MusicCandidate] = []
    for entry in ranked[:6]:
        track_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
        )
        if not track_url:
            continue
        track_url = str(track_url)
        if not track_url.startswith("http"):
            track_url = f"https://soundcloud.com/{track_url}"
        duration = entry.get("duration")
        try:
            dur = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            dur = None
        out.append(
            _MusicCandidate(
                source="soundcloud",
                title=(entry.get("title") or "").strip(),
                uploader=(
                    entry.get("uploader")
                    or entry.get("creator")
                    or entry.get("channel")
                    or ""
                ),
                duration=dur,
                url=track_url,
            )
        )
    return out


def _search_piped_candidates(
    query: str,
    prefer_title: str,
    prefer_artist: str,
    skip_video_ids: set[str] | None,
) -> list[_MusicCandidate]:
    rows = _piped_search_rows(query)
    return _candidates_from_mirror_rows(
        rows,
        source="piped",
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )


def _search_invidious_candidates(
    query: str,
    prefer_title: str,
    prefer_artist: str,
    skip_video_ids: set[str] | None,
) -> list[_MusicCandidate]:
    rows = _invidious_search_rows(query)
    return _candidates_from_mirror_rows(
        rows,
        source="invidious",
        prefer_title=prefer_title,
        prefer_artist=prefer_artist,
        skip_video_ids=skip_video_ids,
    )


def _candidates_from_mirror_rows(
    rows: list[dict],
    *,
    source: str,
    prefer_title: str,
    prefer_artist: str,
    skip_video_ids: set[str] | None,
) -> list[_MusicCandidate]:
    skip = skip_video_ids or set()
    out: list[_MusicCandidate] = []
    for row in rows:
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
        if not title or _AD_TITLE_RE.search(title):
            continue
        uploader = (
            row.get("uploaderName")
            or row.get("author")
            or row.get("uploader")
            or ""
        )
        duration = row.get("lengthSeconds") or row.get("duration")
        try:
            dur = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            dur = None
        entry = {"title": title, "uploader": uploader, "duration": dur}
        if prefer_title or prefer_artist:
            if not _rank_music_entries(
                [entry],
                title=prefer_title or title,
                artist=prefer_artist,
            ):
                continue
        out.append(
            _MusicCandidate(
                source=source,
                title=title,
                uploader=str(uploader),
                duration=dur,
                video_id=vid,
            )
        )
        if len(out) >= 5:
            break
    return out


def _piped_search_rows(query: str) -> list[dict]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=12.0,
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
                if isinstance(items, list) and items:
                    return [r for r in items if isinstance(r, dict)]
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Piped search %s failed: %s", base, exc)
    return []


def _invidious_search_rows(query: str) -> list[dict]:
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=12.0,
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
                if isinstance(rows, list) and rows:
                    return [r for r in rows if isinstance(r, dict)]
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                logger.debug("Invidious search %s failed: %s", base, exc)
    return []


def _search_audius_candidates(
    query: str,
    prefer_title: str,
    prefer_artist: str,
) -> list[_MusicCandidate]:
    out: list[_MusicCandidate] = []
    with httpx.Client(
        headers={"User-Agent": _DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=12.0,
        follow_redirects=True,
    ) as client:
        for host in _AUDIUS_HOSTS:
            try:
                resp = client.get(
                    f"{host}/v1/tracks/search",
                    params={"query": query, "app_name": "TelegramVideoBot"},
                )
                if resp.status_code >= 400:
                    continue
                tracks = (resp.json() or {}).get("data") or []
                if not isinstance(tracks, list):
                    continue
                for track in tracks[:8]:
                    if not isinstance(track, dict):
                        continue
                    title = (track.get("title") or "").strip()
                    user = track.get("user") or {}
                    artist = (
                        (user.get("name") if isinstance(user, dict) else "")
                        or track.get("artist")
                        or ""
                    )
                    duration = track.get("duration")
                    try:
                        dur = float(duration) if duration is not None else None
                    except (TypeError, ValueError):
                        dur = None
                    if dur is not None and dur > 900:
                        continue
                    entry = {"title": title, "uploader": artist, "duration": dur}
                    if prefer_title or prefer_artist:
                        if not _rank_music_entries(
                            [entry],
                            title=prefer_title or title,
                            artist=prefer_artist,
                        ):
                            continue
                    track_id = track.get("id") or track.get("track_id")
                    if not track_id:
                        continue
                    permalink = track.get("permalink") or ""
                    url = (
                        f"https://audius.co{permalink}"
                        if permalink.startswith("/")
                        else (permalink if str(permalink).startswith("http") else None)
                    )
                    stream = (
                        f"{host}/v1/tracks/{track_id}/stream"
                        f"?app_name=TelegramVideoBot"
                    )
                    out.append(
                        _MusicCandidate(
                            source="audius",
                            title=title,
                            uploader=str(artist),
                            duration=dur,
                            url=url,
                            stream_url=stream,
                            stream_ext="mp3",
                        )
                    )
                if out:
                    return out[:5]
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.debug("Audius search %s failed: %s", host, exc)
    return out


def _resolve_via_soundcloud(
    query: str,
    *,
    meta: dict[str, str],
    display_title: str,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
):
    """Search SoundCloud and download the best non-ad, non-DRM match."""
    return _resolve_music_by_query(
        query,
        meta=meta,
        display_title=display_title,
        label=display_title,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        allow_yt_search=False,
        force_audio=True,
        not_found_message="No matching SoundCloud upload found.",
    )


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
            "Music ranked %d candidates; best score=%.1f title=%r",
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
        progress_callback("🎧 <b>Finding track…</b>")

    video_id, title = _piped_playlist_first_video(list_id)
    if not video_id:
        video_id, title = _invidious_playlist_first_video(list_id)

    # YouTube Music album playlists (OLAK5…) often fail on mirrors — search other sources
    if not video_id:
        page_title = _fetch_page_title(url)
        if page_title:
            if progress_callback:
                progress_callback(f"🎧 <b>Finding track…</b>\n<i>{_esc(page_title)}</i>")
            meta = {"title": page_title, "artist": ""}
            result = _resolve_music_by_query(
                page_title,
                meta=meta,
                display_title=page_title,
                label=page_title,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                allow_yt_search=False,
                force_audio=True,
                not_found_message=(
                    "Could not read this YouTube Music playlist from mirrors. "
                    "Send a Spotify track or a direct SoundCloud link instead."
                ),
            )
            return result
        raise RuntimeError(
            "Could not read this YouTube Music playlist from mirrors. "
            "Send a Spotify track or a direct SoundCloud link instead."
        )

    if progress_callback:
        progress_callback(f"⬇️ <b>Downloading…</b>\n<i>{_esc(title or video_id)}</i>")

    audio_url, ext = _mirror_audio_url(video_id)
    if audio_url:
        return _download_audio_result(
            audio_url,
            ext=ext,
            display_title=title or "YouTube track",
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    # Do NOT call yt-dlp YouTube here — VPS is bot-blocked.
    if title:
        if progress_callback:
            progress_callback(f"🎧 <b>Finding track…</b>\n<i>{_esc(title)}</i>")
        result = _resolve_music_by_query(
            title,
            meta={"title": title, "artist": ""},
            display_title=title,
            label=title,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            allow_yt_search=False,
            force_audio=True,
            not_found_message=(
                "YouTube blocked this server and mirrors had no audio stream. "
                "Send a Spotify track or SoundCloud link instead."
            ),
        )
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
        progress_callback("⬇️ <b>Downloading…</b>")

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
    """Race Piped + Invidious stream lookups; return first usable audio URL."""
    from concurrent.futures import wait as wait_futures, FIRST_COMPLETED

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_piped_audio_url, video_id),
            pool.submit(_invidious_audio_url, video_id),
        ]
        pending = set(futures)
        while pending:
            done, pending = wait_futures(pending, timeout=10, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                try:
                    url, ext = fut.result()
                    if url:
                        for other in pending:
                            other.cancel()
                        return url, ext
                except Exception:
                    continue
    return None, ""


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
    from concurrent.futures import wait as wait_futures, FIRST_COMPLETED

    def _try(base: str) -> tuple[str | None, str]:
        with httpx.Client(
            headers={"User-Agent": _DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=8.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(f"{base}/streams/{video_id}")
            if resp.status_code >= 400:
                return None, ""
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
        return None, ""

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_try, base): base for base in _PIPED_INSTANCES[:3]}
        pending = set(futures)
        while pending:
            done, pending = wait_futures(pending, timeout=8, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                try:
                    url, ext = fut.result()
                    if url:
                        for other in pending:
                            other.cancel()
                        return url, ext
                except Exception as exc:
                    logger.debug("Piped streams %s failed: %s", futures[fut], exc)
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
    from concurrent.futures import wait as wait_futures, FIRST_COMPLETED

    def _try(base: str) -> tuple[str | None, str]:
        with httpx.Client(
            headers={"User-Agent": _DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=8.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(f"{base}/api/v1/videos/{video_id}")
            if resp.status_code >= 400:
                return None, ""
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
        return None, ""

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_try, base): base for base in _INVIDIOUS_INSTANCES[:3]}
        pending = set(futures)
        while pending:
            done, pending = wait_futures(pending, timeout=8, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                try:
                    url, ext = fut.result()
                    if url:
                        for other in pending:
                            other.cancel()
                        return url, ext
                except Exception as exc:
                    logger.debug("Invidious video %s @ %s failed: %s", video_id, futures[fut], exc)
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
