"""Environment-driven configuration for carclaude.

Importing this module also *applies* two side effects the agent depends on:
  * prepends carclaude/bin to PATH  -> the `rm`/`git` shim scripts win name lookup
  * exports CARCLAUDE_TRASH         -> the `rm` shim knows where to move files
so that the Bash tool the Agent SDK later spawns inherits a safe environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
WEB_DIR = PROJECT_ROOT / "web"

load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int, lo: int = 1, hi: int = 65535) -> int:
    """Parse an int env var with a clear, actionable error instead of an import-time traceback."""
    raw = _env(key, str(default)) or str(default)
    try:
        val = int(raw)
    except ValueError:
        raise SystemExit(f"Invalid {key}={raw!r}: must be an integer.")
    if not (lo <= val <= hi):
        raise SystemExit(f"Invalid {key}={val}: must be {lo}..{hi}.")
    return val


@dataclass(frozen=True)
class Settings:
    app_token: str = _env("APP_TOKEN")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")
    agent_model: str = _env("AGENT_MODEL", "claude-opus-4-8")

    working_dir: str = _env("WORKING_DIR", str(Path.home() / "project"))
    trash_dir: str = _env("TRASH_DIR") or str(Path(_env("WORKING_DIR", str(Path.home() / "project"))) / ".trash")
    agent_effort: str = _env("AGENT_EFFORT", "medium").lower()  # low|medium|high|xhigh|max

    stt_provider: str = _env("STT_PROVIDER", "openai").lower()
    stt_model: str = _env("STT_MODEL", "whisper-1")
    tts_provider: str = _env("TTS_PROVIDER", "openai").lower()
    tts_model: str = _env("TTS_MODEL", "tts-1")
    tts_voice: str = _env("TTS_VOICE", "alloy")

    openai_api_key: str = _env("OPENAI_API_KEY")
    deepgram_api_key: str = _env("DEEPGRAM_API_KEY")
    deepgram_model: str = _env("DEEPGRAM_MODEL", "nova-2")
    elevenlabs_api_key: str = _env("ELEVENLABS_API_KEY")
    elevenlabs_voice_id: str = _env("ELEVENLABS_VOICE_ID")
    elevenlabs_model: str = _env("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

    host: str = _env("HOST", "127.0.0.1")
    port: int = _env_int("PORT", 8787)

    def apply_environment(self) -> None:
        """Make the rm/git shims active and the trash dir known for any child shell."""
        Path(self.trash_dir).mkdir(parents=True, exist_ok=True)
        os.environ["CARCLAUDE_TRASH"] = self.trash_dir
        path = os.environ.get("PATH", "")
        if str(BIN_DIR) not in path.split(os.pathsep):
            os.environ["PATH"] = os.pathsep.join([str(BIN_DIR), path])
        # The SDK reads ANTHROPIC_API_KEY from the environment.
        if self.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = self.anthropic_api_key


settings = Settings()
settings.apply_environment()
