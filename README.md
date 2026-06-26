# carclaude

A hands-free, voice-only way to drive a Claude agent over one of your code projects from
your phone — built for the car. The PWA captures your voice, the server transcribes it
(Whisper/Deepgram), runs the **Claude Agent SDK** against your project, and speaks the reply
back (OpenAI/ElevenLabs TTS). It can read, edit, and run code — with **git disabled** and
**`rm` routed to a trash folder** instead of really deleting.

> ⚠️ **This endpoint runs code on your machine.** Treat `APP_TOKEN` like a root password,
> keep the server on `127.0.0.1` (a cloudflared tunnel reaches it locally), and put
> **Cloudflare Access** in front of the public hostname as a second lock. The safety rails
> (git off, `rm`→trash, deny-list) stop *mis-heard* commands — **they are not a sandbox.**
> Read [SECURITY.md](SECURITY.md) and run the server as a dedicated, non-root user before
> exposing it.

> 💸 **It costs real money.** Every turn calls Claude plus a speech-to-text and a
> text-to-speech provider. Set `daily_budget_usd` in `preferences.json` and watch the
> on-screen usage meter.

## Architecture

```
Phone PWA  ──HTTPS (your Cloudflare URL)──▶  cloudflared ──▶  127.0.0.1:8787 (FastAPI)
  mic→STT, play TTS, voice loop                                  ├─ /api/stt  Whisper/Deepgram
                                                                 ├─ /api/tts  OpenAI/ElevenLabs
                                                                 └─ /api/message → Claude Agent SDK
                                                                      cwd = your project
                                                                      git denied · rm → .trash
```

The Anthropic API key lives only on the host. The phone authenticates to the host with
`APP_TOKEN`; the host authenticates to Anthropic with its own key. Keys never reach the phone.

## Requirements

- Python 3.11+ and the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/)
  (installed via `requirements.txt`).
- API keys for Anthropic, plus a speech-to-text provider (OpenAI Whisper or Deepgram) and a
  text-to-speech provider (ElevenLabs or OpenAI).
- A [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  tunnel (or any HTTPS reverse proxy) to reach the phone — a PWA needs HTTPS for mic access.

### Windows

Run it under **WSL2** (Ubuntu): the bash safety shims and the agent's shell tooling expect a
Unix shell. In a WSL2 terminal, follow the Setup steps below exactly as on Linux. (The PWA
itself runs in mobile Safari/Chrome and doesn't care what the server runs on.)

## Setup

```bash
git clone <your-fork-url> carclaude && cd carclaude
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env       # then edit .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> APP_TOKEN
```

Fill in `.env`:
- `APP_TOKEN` — the phone's password (the random string above).
- `ANTHROPIC_API_KEY` — for the agent.
- `OPENAI_API_KEY` — Whisper STT and/or OpenAI TTS. (Or use `STT_PROVIDER=deepgram` /
  `TTS_PROVIDER=elevenlabs` with their keys.)
- `WORKING_DIR` — **the project you want to drive.** The agent can read/edit/run anything
  under it, so choose deliberately. `TRASH_DIR` is where `rm` moves things.

## Run

```bash
./run.sh                                  # serves on 127.0.0.1:8787
cloudflared tunnel run <your-tunnel>      # point ingress at http://127.0.0.1:8787
```

To run both on boot via systemd, see [`deploy/install.sh`](deploy/install.sh) (it installs the
units for your user — never as root).

On the phone: open your Cloudflare URL in Safari → **Share → Add to Home Screen**. Open it,
paste `APP_TOKEN` once. **Hold to talk** for push-to-talk, or tap **Auto** for the hands-free
loop (it re-listens after each reply). **Stop** barges in. **Clear** starts a fresh
conversation (or just say "clear") — it drops the live context and stops auto-recalling
earlier turns; the on-disk transcript is kept.

When a turn takes real work, the assistant speaks a short cue in its own voice that names what
it's doing — "Reading files.", "Editing a file.", "Running a command.", "Searching the web." — so
while driving you hear that it's working before the full reply arrives, and roughly what's taking
the time. While it's only thinking (no tool yet) past a couple of seconds, the cue is a plain
"Thinking." (set by `ack_phrase`). A quick one-line answer skips cues entirely, so nothing is
repeated after every prompt. Each phrase is synthesized once and cached, so it's instant and free
after the first use. Say "turn off acknowledgements" to silence them, change how long it waits
before the first cue (`ack_delay_ms`, 0 = every turn), or ask it to change the thinking phrase
(`status_ack` / `ack_phrase` in `preferences.json`).

## Personalities

The assistant's voice is set by `personality.md` (the default) or a persona picked in the UI.
Personas live in `personalities/<id>.md` — frontmatter with `name` and an ElevenLabs
`voice_id`, then the prompt. Ships with a handful (JARVIS, FRIDAY, Gandalf, Attenborough, a
review "Committee", a patient tutor, and a couple of fun ones). Add a persona by dropping in a
file; the `voice_id`s are examples — swap them for voices from your own ElevenLabs library.

**Private personas:** drop `<id>.md` files in `personalities.local/` (git-ignored) to add or
override personas without publishing them.

## Safety behavior (best-effort — see [SECURITY.md](SECURITY.md))

- **git** — any `git` command is denied (and a `bin/git` shim refuses it).
- **delete → trash** — a plain `rm`/`rmdir`/`unlink` moves targets into `TRASH_DIR` instead
  of deleting. Common bypasses (`/bin/rm`, `PATH=… rm`, `env -i`, `find -delete`, `shred`,
  `sudo`, raw-device writes, fork bombs) are denied by `server/safety.py`.
- **app internals** — the agent can edit only `personalities/` and `preferences.json`, can
  read its own `history/`, and is blocked from `.env`/`server/`/`bin/`. Structured file tools
  are confined to `WORKING_DIR`.

These rails stop mis-heard commands. They are **not** a jail: a determined agent can still
delete via an interpreter, overwrite files, or read secrets with enough shell trickery.
**Confidentiality and true delete-safety depend on running sandboxed and non-root.**

## Files

```
server/  config.py safety.py voice.py agent.py main.py personalities.py prefs.py state.py history.py usage.py
web/     index.html app.js style.css manifest.webmanifest icon.svg
bin/     rm rmdir unlink git           # PATH shims (rm→trash, git→refuse)
deploy/  carclaude.service cloudflared-carclaude.service install.sh
run.sh   cloudflared.config.example.yml  .env.example  requirements.txt  SECURITY.md
```

## License

MIT — see [LICENSE](LICENSE).
