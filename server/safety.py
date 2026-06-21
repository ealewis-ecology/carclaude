"""Command safety for the in-car agent.

Two requirements:
  * git is disabled entirely.
  * `rm` must not actually delete — it is routed to a trash folder.

How that's enforced (defense in depth):
  1. PATH shims (carclaude/bin/rm, /rmdir, /unlink, /git) — a bare `rm foo` resolves
     to a script that MOVES `foo` into the trash; bare `git` resolves to a refusal.
     This makes the common case transparent ("rm went to trash").
  2. `evaluate_bash()` below — a PreToolUse guard that DENIES the ways an agent could
     sneak past the shims: PATH overrides (`PATH=/usr/bin rm`), `env -i`, absolute-path
     deletes (/bin/rm), `find -delete`, `shred`, `sudo`, writing to block devices, fork
     bombs, any `git`, and reads/writes of off-limits files. Bare `rm`/`rmdir`/`unlink`
     are intentionally ALLOWED here, because the shim makes them safe.

THIS IS A BEST-EFFORT DENYLIST, NOT A SANDBOX. It stops the obvious and the enumerated
footguns from a mis-heard voice command. It CANNOT stop a determined agent: an interpreter
(`python3 -c "import os; os.remove(...)"`), output redirection (`> file`), or a filename
built from shell expansion can still destroy data, and `.env`/secrets can be read with
enough shell trickery. Confidentiality of secrets and true delete-safety depend on running
the server as a sandboxed, non-root user that cannot reach those files — see SECURITY.md.
The regexes here are a guard rail, not a wall.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from server.config import PROJECT_ROOT

# The install directory name (e.g. "carclaude"). Deriving it from PROJECT_ROOT keeps the
# off-limits guards working regardless of what folder the repo is cloned into.
_APP = re.escape(PROJECT_ROOT.name)

# (compiled pattern, human reason). First match wins. Patterns are matched against both the
# raw command AND a de-quoted form (see _dequote), so quoting like g"i"t / .e''nv can't hide
# a denied token.
_DENY = [
    (re.compile(r"\bgit\b"),
     "git is disabled in the in-car agent."),
    (re.compile(r"\bsudo\b"),
     "sudo / privilege escalation is disabled."),
    (re.compile(r"(?:^|[\s;&|()`$])PATH="),
     "overriding PATH bypasses the rm->trash and git shims; use plain `rm`."),
    (re.compile(r"\benv\s+-i\b"),
     "`env -i` clears the safe PATH and bypasses the shims."),
    (re.compile(r"(?:^|[\s;&|()`$])/(?:usr/)?bin/(?:rm|rmdir|unlink)\b"),
     "absolute-path deletes bypass the trash; use plain `rm` (it is routed to trash)."),
    (re.compile(r"\b(?:command|env|busybox)\s+rm\b"),
     "that bypasses the trash; use plain `rm` (it is routed to trash)."),
    (re.compile(r"\\rm\b"),
     "that bypasses the trash; use plain `rm` (it is routed to trash)."),
    (re.compile(r"\bfind\b[^|;&\n]*\s-delete\b"),
     "`find -delete` deletes in place; move matches to trash with `rm` instead."),
    (re.compile(r"\bshred\b"),
     "`shred` permanently destroys data and is disabled."),
    (re.compile(r"\bmkfs|\bdd\b[^\n]*\bof=/dev/|>\s*/dev/[sh]d"),
     "writing to raw devices is disabled."),
    (re.compile(r":\s*\(\s*\)\s*\{"),
     "fork-bomb pattern is disabled."),
    (re.compile(r"\.env\b"),
     "the .env secrets file is off-limits."),
    (re.compile(_APP + r"/(?:server|bin|deploy|web|\.venv|\.git)\b|" + _APP + r"/run\.sh\b"),
     "app internals are off-limits; only personalities/ and preferences.json are editable."),
    (re.compile(r"(?:>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\btruncate\b|\bsed\b[^\n]*\s-i)\s*"
                r"[^\n;|&]*" + _APP + r"/personality(?:-[\w-]+)?\.md\b"),
     "personality.md is loaded as the base prompt; edit personas under personalities/ instead."),
    (re.compile(r"(?:>>?|\btee\b|\brm\b|\bmv\b|\btruncate\b)\s*[^\n;|&]*" + _APP + r"/history\b"),
     "the conversation history is read-only; read it with cat/grep but never modify or delete it."),
    (re.compile(r"(?:>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\btruncate\b|\bshred\b|\brmtree\b)\s*"
                r"[^\n;|&]*\.trash\b"),
     "the trash is recoverable storage; it can't be moved, emptied, or overwritten."),
]

# Characters the shell strips during word-splitting. Removing them gives a rough "what the
# shell will actually run" form, so a denied token hidden behind quoting/backslashes (g"i"t,
# .e''nv, \rm) is still caught.
_STRIP = str.maketrans("", "", "\"'\\")


class Decision(NamedTuple):
    allowed: bool
    reason: str


def _dequote(s: str) -> str:
    return s.translate(_STRIP)


def evaluate_bash(command: str) -> Decision:
    """Allow or deny a Bash command string (best-effort; see module docstring)."""
    if not command or not command.strip():
        return Decision(True, "")
    forms = (command, _dequote(command))
    for pat, reason in _DENY:
        if any(pat.search(f) for f in forms):
            return Decision(False, reason)
    return Decision(True, "")


if __name__ == "__main__":  # quick self-check: python -m server.safety
    app = PROJECT_ROOT.name
    cases = {
        "rm -rf build/": True,             # allowed -> shim sends it to trash
        "rm a.txt b.txt": True,
        "rmdir old/": True,
        "ls -la && cat README.md": True,
        "python analysis/run.py": True,
        "git status": False,
        "git commit -am x": False,
        'g"i"t status': False,             # quoting bypass closed
        "sudo rm -rf /": False,
        "/bin/rm -rf important/": False,
        "command rm secret": False,
        r"\rm forced": False,
        "PATH=/usr/bin rm a.txt": False,   # PATH override closed
        "env PATH=/usr/bin rm a.txt": False,
        "env -i rm a.txt": False,
        'cat .e""nv': False,               # quoted .env read closed
        "find . -name '*.tmp' -delete": False,
        "find . -name '*.tmp' -exec rm {} +": True,   # exec rm -> shimmed -> trash
        "shred -u secret.key": False,
        "dd if=/dev/zero of=/dev/sda": False,
        f"cat {app}/history/conversation.jsonl": True,        # reading history is fine
        f"tail -100 {app}/history/conversation.jsonl": True,
        f"grep foo {app}/history/conversation.jsonl": True,
        f"rm {app}/history/conversation.jsonl": False,        # but not deleting/overwriting it
        f"echo x >> {app}/history/conversation.jsonl": False,
        f"echo bad > {app}/personality.md": False,            # base-prompt write closed
        "mv .trash /tmp/gone": False,                          # trash can't be relocated
    }
    ok = True
    for cmd, want in cases.items():
        got = evaluate_bash(cmd).allowed
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{flag}] allowed={got!s:5} want={want!s:5}  {cmd}")
    print("ALL OK" if ok else "SOME FAILED")
