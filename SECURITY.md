# Security policy

carclaude is, by design, a **remote agent that runs arbitrary code on the host it runs on.**
Read this before you expose it to anything.

## Threat model

- **One trusted owner.** Authentication is a single shared bearer token (`APP_TOKEN`) plus,
  strongly recommended, Cloudflare Access in front of the public hostname.
- **`APP_TOKEN` is root-equivalent.** Anyone who has it can make the agent read, write, and
  run code on your machine as the user the server runs as. Treat it like a root password.
- The server is meant to bind to `127.0.0.1` and be reached only through a cloudflared
  tunnel — never expose the port directly to the internet.

## What the safety rails do — and do NOT do

carclaude has guard rails so a *mis-heard voice command* doesn't wreck your machine:

- a plain `rm`/`rmdir`/`unlink` is routed to a trash folder instead of deleting;
- `git` is disabled;
- the app's own `.env`, `server/`, `bin/`, and `personality.md` are off-limits to the agent;
- a `read_only` mode blocks edits and command execution.

**These are best-effort, not a sandbox.** They are a denylist over shell command strings plus
a PATH shim. A determined — or prompt-injected — agent can defeat them, for example by:

- deleting via an interpreter (`python3 -c "import os; os.remove(...)"`, `perl -e 'unlink ...'`);
- destroying file contents without `rm` (`> file`, `truncate`, an editor save);
- reading secrets through shell expansion the denylist can't enumerate (globbing, computed
  filenames, base64, etc.).

So: **do not rely on the rails for confidentiality or for true delete-safety.** They reduce
footguns; they are not a security boundary.

## The actual boundary: run it sandboxed and unprivileged

The only durable protection is the OS. Before exposing carclaude:

1. **Run as a dedicated, non-root user** whose home contains **no** SSH keys, cloud
   credentials (`~/.aws`, `~/.config/gh`, `~/.netrc`), or other repositories. The agent's
   Bash tool runs as this user and is *not* confined to your project directory.
2. **Keep the cloudflared tunnel credential out of that user's reach.**
3. For real isolation, run the server inside a **container, VM, or `bwrap`/`firejail`
   sandbox** that only mounts the one project directory it should touch.
4. **Keep `HOST=127.0.0.1`** and put **Cloudflare Access** (or equivalent SSO/OTP) in front
   of the public hostname, as a second lock on top of `APP_TOKEN`.
5. Point `WORKING_DIR` at a project you trust. Files the agent reads (including a repo's
   own README or a fetched page) can contain **prompt-injection** that steers the agent;
   carclaude treats saved notes and history as untrusted data, but injection + arbitrary
   code is still a real risk on an untrusted repo.

## Secrets

- All provider API keys and `APP_TOKEN` live only on the host (in `.env`) and are **never**
  sent to the browser/phone. The phone authenticates to the host; the host authenticates to
  the providers.
- `.env` is git-ignored and should be `chmod 600`. Even better, store it outside the project
  tree (e.g. a systemd `EnvironmentFile` or a secrets manager) so the agent can't reach it.
- **If a key is ever committed, logged, or shared, rotate it immediately** at the provider.
  Git history preserves secrets even after deletion.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue. Email the
maintainer (see the GitHub profile) with steps to reproduce. We'll acknowledge and work on a
fix before any public disclosure.
