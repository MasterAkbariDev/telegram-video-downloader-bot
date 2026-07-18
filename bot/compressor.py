"""Compress videos with ffmpeg — light (~half size) and hard (Telegram limit)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from bot.config import FFMPEG_THREADS, MAX_VIDEO_HEIGHT, get_compress_target_bytes, get_max_file_size

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], None]

# Light compress: aim ~50% size for typical reels (fast encode)
LIGHT_COMPRESS_MIN_BYTES = 3 * 1024 * 1024
# Hard compress: only when over soft Telegram-friendly target
HARD_COMPRESS_MIN_BYTES = 8 * 1024 * 1024


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def needs_compress(path: Path, *, max_bytes: int | None = None) -> bool:
    """True when file is over the soft target (hard/limit path)."""
    target = max_bytes if max_bytes is not None else get_compress_target_bytes()
    size = path.stat().st_size
    return size > target and size >= HARD_COMPRESS_MIN_BYTES


def needs_light_compress(path: Path) -> bool:
    """True for normal videos we should shrink ~50% before upload."""
    return path.stat().st_size >= LIGHT_COMPRESS_MIN_BYTES


def compress_video(
    path: Path,
    *,
    max_bytes: int | None = None,
    max_height: int | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """Re-encode to H.264/AAC MP4 under the size budget. Returns path (may replace original)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed — cannot compress large videos.")

    hard_limit = get_max_file_size()
    target = max_bytes if max_bytes is not None else get_compress_target_bytes()
    target = min(target, int(hard_limit * 0.90))
    height = max_height if max_height is not None else MAX_VIDEO_HEIGHT
    size = path.stat().st_size

    if size <= target:
        return path

    if progress_callback:
        progress_callback(
            f"🗜 <b>Compressing…</b> ({_fmt(size)} → under {_fmt(target)})"
        )

    duration = _probe_duration(path) or 0.0
    if duration > 1:
        video_kbps = max(250, int((target * 8 * 0.85) / duration / 1000))
    else:
        video_kbps = 1200
    video_kbps = min(video_kbps, 2500)

    out = path.with_name(f"{path.stem}.compressed.mp4")

    attempts = (
        (video_kbps, height),
        (max(200, video_kbps // 2), min(height, 480)),
        (max(150, video_kbps // 3), min(height, 360)),
    )

    last_error: Exception | None = None
    for kbps, h in attempts:
        if cancel_check:
            cancel_check()
        scale = f"scale=-2:'min({h},ih)'"
        label = f"Compressing… {h}p @ {kbps} kbps"
        try:
            _run_ffmpeg(
                path,
                out,
                mode="bitrate",
                video_kbps=kbps,
                scale=scale,
                duration=duration,
                label=label,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Compress attempt %sp/%skbps failed: %s", h, kbps, exc)
            out.unlink(missing_ok=True)
            continue

        new_size = out.stat().st_size
        logger.info(
            "Compressed %s → %s (%s → %s @ %sp/%skbps)",
            path.name,
            out.name,
            _fmt(size),
            _fmt(new_size),
            h,
            kbps,
        )
        if new_size <= target or (new_size < size * 0.85 and new_size <= hard_limit):
            return _replace_original(path, out)
        out.unlink(missing_ok=True)

    if last_error:
        raise RuntimeError(f"Compression failed: {last_error}") from last_error
    raise RuntimeError(
        f"Could not compress {_fmt(size)} under {_fmt(target)}. Try a shorter clip."
    )


def light_compress_video(
    path: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    """Fast one-pass re-encode aiming for ~half the original size."""
    if not ffmpeg_available():
        return path

    size = path.stat().st_size
    if size < LIGHT_COMPRESS_MIN_BYTES:
        return path

    target = max(size // 2, 1024 * 1024)
    if progress_callback:
        progress_callback(
            f"🗜 <b>Compressing…</b> ({_fmt(size)} → ~{_fmt(target)})"
        )

    duration = _probe_duration(path) or 0.0
    if duration > 1:
        video_kbps = max(200, int((target * 8 * 0.88) / duration / 1000))
    else:
        video_kbps = 900
    # Cap so short clips don't stay huge; floor keeps watchable quality
    video_kbps = min(max(video_kbps, 200), 1800)

    height = min(MAX_VIDEO_HEIGHT, 720)
    out = path.with_name(f"{path.stem}.light.mp4")
    scale = f"scale=-2:'min({height},ih)'"

    if cancel_check:
        cancel_check()

    try:
        # Prefer CRF for speed/quality; bitrate pass if still too large
        _run_ffmpeg(
            path,
            out,
            mode="crf",
            crf=28,
            scale=scale,
            duration=duration,
            label="Compressing…",
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        new_size = out.stat().st_size
        if new_size > target * 1.15:
            out.unlink(missing_ok=True)
            _run_ffmpeg(
                path,
                out,
                mode="bitrate",
                video_kbps=video_kbps,
                scale=scale,
                duration=duration,
                label=f"Compressing… tighter @ {video_kbps} kbps",
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            new_size = out.stat().st_size
    except Exception as exc:
        logger.warning("Light compress failed for %s: %s", path.name, exc)
        out.unlink(missing_ok=True)
        return path

    # Keep original if we barely saved anything (encode not worth it)
    if new_size >= size * 0.90:
        logger.info(
            "Light compress skipped result for %s (%s → %s, not smaller enough)",
            path.name,
            _fmt(size),
            _fmt(new_size),
        )
        out.unlink(missing_ok=True)
        return path

    logger.info(
        "Light compressed %s (%s → %s, %.0f%%)",
        path.name,
        _fmt(size),
        _fmt(new_size),
        100.0 * new_size / size,
    )
    return _replace_original(path, out)


def _replace_original(path: Path, out: Path) -> Path:
    path.unlink(missing_ok=True)
    final = path.with_suffix(".mp4") if path.suffix.lower() != ".mp4" else path
    if out != final:
        out.replace(final)
        return final
    return out


def _run_ffmpeg(
    src: Path,
    dest: Path,
    *,
    mode: str,
    scale: str,
    cancel_check: CancelCheck | None,
    duration: float = 0.0,
    label: str = "Compressing…",
    progress_callback: ProgressCallback | None = None,
    video_kbps: int | None = None,
    crf: int | None = None,
) -> None:
    threads = str(FFMPEG_THREADS)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(src),
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-threads",
        threads,
        "-filter_threads",
        threads,
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
    ]
    if mode == "crf":
        cmd.extend(["-crf", str(crf if crf is not None else 28)])
    else:
        kbps = video_kbps or 1000
        cmd.extend(
            [
                "-b:v",
                f"{kbps}k",
                "-maxrate",
                f"{int(kbps * 1.2)}k",
                "-bufsize",
                f"{kbps * 2}k",
            ]
        )
    cmd.append(str(dest))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    last_report = 0.0
    try:
        assert proc.stdout is not None
        while True:
            if cancel_check:
                cancel_check()
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.strip()
            if line.startswith("out_time_ms=") and progress_callback and duration > 0:
                try:
                    out_ms = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                now = time.monotonic()
                if now - last_report < 1.0 and out_ms / 1000 < duration:
                    continue
                last_report = now
                from bot.messages import compress_progress

                progress_callback(
                    compress_progress(out_ms / 1_000_000.0, duration, label=label)
                )
            elif line == "progress=end" and progress_callback and duration > 0:
                from bot.messages import compress_progress

                progress_callback(compress_progress(duration, duration, label=label))

        proc.wait(timeout=30)
        if proc.returncode != 0:
            err = ""
            if proc.stderr:
                err = proc.stderr.read()[-500:]
            raise RuntimeError(err or f"ffmpeg exit {proc.returncode}")
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def _probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
            timeout=30,
            stderr=subprocess.DEVNULL,
        ).strip()
        return float(out) if out else None
    except Exception:
        return None


def _fmt(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
