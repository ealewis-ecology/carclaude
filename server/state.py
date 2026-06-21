"""Mutable runtime selection for the (single-user) session.

The persona picker writes here; the agent reads `persona_prompt`/`token` to decide
when to rebuild its session, and voice.py reads `voice_id` for TTS. This module imports
nothing from the rest of the app, so it's safe to import anywhere (no cycles).
"""
from __future__ import annotations

persona_id: str | None = None
persona_name: str | None = None
persona_prompt: str | None = None
voice_id: str | None = None        # active ElevenLabs voice; None -> .env default
token: int = 0                     # bumps on each selection -> triggers agent rebuild


def select(pid: str, name: str, prompt: str, vid: str | None) -> None:
    global persona_id, persona_name, persona_prompt, voice_id, token
    persona_id, persona_name, persona_prompt, voice_id = pid, name, prompt, vid
    token += 1
