"""Release version and build metadata."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from bot.config import ROOT_DIR

VERSION_FILE = ROOT_DIR / "VERSION"


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        line = VERSION_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if line:
            return line
    except OSError:
        pass
    return "0.0.0"


def get_git_commit(*, short: bool = True) -> str | None:
    args = ["git", "rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    try:
        return subprocess.check_output(
            args,
            cwd=ROOT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except Exception:
        return None


def get_git_branch() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except Exception:
        return None


def get_ytdlp_version() -> str:
    try:
        import yt_dlp

        return getattr(yt_dlp, "version", {}).get("__version__", "unknown")
    except Exception:
        return "unknown"


def format_version_label() -> str:
    return f"v{get_version()}"


def format_build_line() -> str:
    commit = get_git_commit()
    branch = get_git_branch()
    if branch and commit:
        return f"{branch} @ <code>{commit}</code>"
    if commit:
        return f"<code>{commit}</code>"
    return "unknown"


def format_version_block(*, include_ytdlp: bool = False) -> str:
    lines = [f"Version: <b>{format_version_label()}</b>", f"Build: {format_build_line()}"]
    if include_ytdlp:
        lines.append(f"yt-dlp: <code>{get_ytdlp_version()}</code>")
    return "\n".join(lines)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
