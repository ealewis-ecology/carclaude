"""Prebuilt persona catalog.

Each persona is one file in personalities/<id>.md with a small YAML-ish header:

    ---
    name: JARVIS
    voice_id: DYkrAHD8iwork3YSUBbs
    ---
    <the personality / response-guideline prompt body...>

`name` is shown in the picker; `voice_id` is the ElevenLabs voice to speak with (omit
to use the .env default). The body becomes the agent's system prompt (the locked
functional rules are still appended in agent.py). Add a persona = drop in a file.
"""
from __future__ import annotations

import re

from server.config import PROJECT_ROOT

_VOICE_ID_RE = re.compile(r"[A-Za-z0-9]+")

PERSONA_DIR = PROJECT_ROOT / "personalities"
# Optional private personas: drop <id>.md files here to add or override personas without
# publishing them. This directory is git-ignored, so it never ships with the repo. A local
# persona with the same id as a shipped one overrides it.
LOCAL_PERSONA_DIR = PROJECT_ROOT / "personalities.local"


def _parse(text: str) -> tuple[str | None, str | None, str]:
    name = voice_id = None
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        i, hdr = 1, []
        while i < len(lines) and lines[i].strip() != "---":
            hdr.append(lines[i])
            i += 1
        if i < len(lines):                       # found the closing '---'
            body = "\n".join(lines[i + 1:])
            for ln in hdr:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    k, v = k.strip().lower(), v.strip()
                    if k == "name":
                        name = v
                    elif k in ("voice_id", "voice"):
                        # Only accept a clean alphanumeric id; anything else falls back to the
                        # .env default voice rather than traversing into the ElevenLabs API path.
                        voice_id = v if _VOICE_ID_RE.fullmatch(v) else None
    return name, voice_id, body.strip()


def _load_dir(directory) -> dict[str, dict]:
    """Parse every <id>.md in a directory into {id: persona}. Missing dir -> {}."""
    found: dict[str, dict] = {}
    if directory.is_dir():
        for f in sorted(directory.glob("*.md")):
            try:
                name, voice_id, prompt = _parse(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            if prompt:
                found[f.stem] = {"id": f.stem, "name": name or f.stem,
                                 "voice_id": voice_id, "prompt": prompt}
    return found


def catalog() -> list[dict]:
    """All personas, sorted by id. Each: {id, name, voice_id, prompt}.

    Personas come from personalities/ (shipped with the repo) plus an optional, git-ignored
    personalities.local/ for private personas. A local persona with the same id as a shipped
    one overrides it, so you can keep private versions without publishing them.
    """
    merged = _load_dir(PERSONA_DIR)
    merged.update(_load_dir(LOCAL_PERSONA_DIR))   # local overrides shipped, by id
    return [merged[k] for k in sorted(merged)]


def get(pid: str) -> dict | None:
    return next((p for p in catalog() if p["id"] == pid), None)
