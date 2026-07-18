"""Local changelog helpers."""

from __future__ import annotations

import re
from pathlib import Path

from bot.config import ROOT_DIR
from bot.messages import esc

CHANGELOG_FILE = ROOT_DIR / "CHANGELOG.md"

_VERSION_HEADER = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*", re.MULTILINE)


def read_changelog_text() -> str:
    try:
        return CHANGELOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def parse_sections(text: str | None = None) -> list[tuple[str, str]]:
    """Return [(version, body), ...] newest first."""
    raw = text if text is not None else read_changelog_text()
    if not raw.strip():
        return []

    matches = list(_VERSION_HEADER.finditer(raw))
    if not matches:
        return []

    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        version = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        sections.append((version, body))
    return sections


def format_changelog_for_telegram(
    *,
    max_versions: int = 5,
    max_chars: int = 3500,
    source: str | None = None,
) -> str:
    sections = parse_sections(source)
    if not sections:
        return "📜 <b>Changelog</b>\n\n<i>No changelog found.</i>"

    lines = ["📜 <b>Changelog</b>", ""]
    for version, body in sections[:max_versions]:
        lines.append(f"<b>v{esc(version)}</b>")
        for raw_line in body.splitlines():
            line = raw_line.rstrip()
            if not line:
                lines.append("")
                continue
            if line.startswith("### "):
                lines.append(f"<b>{esc(line[4:])}</b>")
            elif line.startswith("- "):
                lines.append(f"• {esc(line[2:])}")
            else:
                lines.append(esc(line))
        lines.append("")

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "\n\n<i>…truncated</i>"
    return text


def changelog_since(local_version: str, remote_text: str | None = None) -> str:
    """Changelog entries newer than local_version (for update alerts)."""
    sections = parse_sections(remote_text if remote_text is not None else None)
    newer: list[tuple[str, str]] = []
    for version, body in sections:
        if _version_tuple(version) > _version_tuple(local_version):
            newer.append((version, body))
        else:
            break

    if not newer:
        # Still show the top remote section if versions equal but commit differs
        if sections:
            version, body = sections[0]
            return _format_section(version, body)
        return "<i>No details.</i>"

    parts = [_format_section(v, b) for v, b in newer[:4]]
    text = "\n\n".join(parts)
    if len(text) > 2500:
        text = text[:2480].rstrip() + "\n…"
    return text


def _format_section(version: str, body: str) -> str:
    lines = [f"<b>v{esc(version)}</b>"]
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("### "):
            lines.append(f"<b>{esc(line[4:])}</b>")
        elif line.startswith("- "):
            lines.append(f"• {esc(line[2:])}")
        else:
            lines.append(esc(line))
    return "\n".join(lines)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.strip().lstrip("vV").split("."):
        digits = re.match(r"(\d+)", piece)
        parts.append(int(digits.group(1)) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
