"""Persistent conversation history for the in-car agent.

The live SDK session keeps context across voice turns, but it is wiped whenever the
session is rebuilt — a persona switch, a personality.md edit, a preferences change, or a
server restart all start fresh. So each completed turn (your words, the spoken reply, and
any tools the agent ran) is appended here to history/conversation.jsonl. That gives two
things the user asked for:

  * the agent can be handed a short digest of the most recent turns at session start, so
    it picks up where you left off without being asked (see agent._build_system_prompt);
  * the agent has READ access to the full log (granted in agent._path_allowed) so it can
    look further back "if necessary". It can read but never write or delete the log.

One file, append-only, capped at _MAX_TURNS lines. Turns are serialized by the server's
_turn_lock, so there is no concurrent writer to race.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from server.config import PROJECT_ROOT

DIR = PROJECT_ROOT / "history"
FILE = DIR / "conversation.jsonl"

_MAX_TURNS = 1000        # keep the last N turns; older ones roll off
_FIELD_CHARS = 800       # per-utterance cap stored on disk
_WS = re.compile(r"\s+")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _one_line(s: str, limit: int) -> str:
    s = _WS.sub(" ", (s or "").strip())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def record(persona: str, user: str, assistant: str, tools: list[str] | None = None) -> None:
    """Append one completed turn. No-op if both sides are empty."""
    user = (user or "").strip()
    assistant = (assistant or "").strip()
    if not user and not assistant:
        return
    rec = {
        "ts": _now(),
        "persona": (persona or "")[:80],
        "user": user[:_FIELD_CHARS],
        "assistant": assistant[:_FIELD_CHARS],
        "tools": [str(t)[:160] for t in (tools or [])][:12],
    }
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        lines = FILE.read_text(encoding="utf-8").splitlines() if FILE.exists() else []
        lines.append(json.dumps(rec, ensure_ascii=False))
        if len(lines) > _MAX_TURNS:
            lines = lines[-_MAX_TURNS:]
        FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(DIR, 0o700)        # the transcript is owner-private (mirrors .env's 0600)
        os.chmod(FILE, 0o600)
    except OSError:
        pass


def recent(n: int = 8) -> list[dict]:
    """The last n turns as dicts (oldest first)."""
    try:
        lines = FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines[-n:] if n > 0 else lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def digest(n: int = 6, max_chars: int = 1400) -> str:
    """A compact plain-text tail of the last n turns for the system prompt.

    Labels the human as 'User' and the agent (the reader of the prompt) as 'You', so the
    model reads it as its own recent memory.
    """
    recs = recent(n)
    if not recs:
        return ""
    lines = []
    for r in recs:
        when = str(r.get("ts", ""))[:16].replace("T", " ")
        u = _one_line(r.get("user", ""), 180)
        a = _one_line(r.get("assistant", ""), 180)
        seg = f"[{when}] User: {u}"
        if a:
            seg += f" | You replied: {a}"
        lines.append(seg)
    block = "\n".join(lines)
    return block[-max_chars:]
