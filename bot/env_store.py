"""Read and update .env values from the admin panel."""

from __future__ import annotations

import re
from pathlib import Path

from bot.config import ROOT_DIR

ENV_PATH = ROOT_DIR / ".env"


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    lines = ["# Managed by the bot — do not commit this file"]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_env_value(key: str, value: str) -> None:
    if not ENV_PATH.exists():
        write_env({key: value})
        return

    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def remove_env_key(key: str) -> None:
    if not ENV_PATH.exists():
        return

    pattern = re.compile(rf"^{re.escape(key)}=.*\n?", re.MULTILINE)
    text = pattern.sub("", ENV_PATH.read_text(encoding="utf-8"))
    ENV_PATH.write_text(text, encoding="utf-8")


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}…{value[-visible:]}"
