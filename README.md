# Telvia

**A local, Telegram-style web app that never talks to Telegram directly. Everything is relayed through a GitHub repository.**

```
browser ⇄ local server (FastAPI) ⇄ git push/pull ⇄ "bus" branch ⇄ GitHub Actions (Telethon) ⇄ Telegram
```

![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Termux%20(proot--distro)-informational)

---

## Table of contents

- [What it is](#what-it-is)
- [Features](#features)
- [How it works](#how-it-works)
- [⚠️ Cost in Actions minutes](#️-cost-in-actions-minutes)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Linux / macOS / Windows](#linux--macos--windows)
  - [Android (Termux with proot-distro)](#android-termux-with-proot-distro)
- [Setup (one time)](#setup-one-time)
- [Daily use](#daily-use)
- [GitHub token (PAT)](#github-token-pat)
- [Themes](#themes)
- [Text direction (LTR / RTL)](#text-direction-ltr--rtl)
- [Security](#security)
- [Behavior and limits](#behavior-and-limits)
- [Purge](#purge)
- [Configuration reference](#configuration-reference)
- [Project structure](#project-structure)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Legal and ethical notes](#legal-and-ethical-notes)

---

## What it is

Telvia gives you a private, local web interface for your own Telegram account in situations where your device cannot (or should not) reach Telegram directly. Your browser only ever talks to a small server on `127.0.0.1`, and that server only ever talks to `github.com` through plain `git clone / fetch / push`. A GitHub Actions job running Telethon is the only component that touches Telegram.

## Features

- **No direct Telegram connection from your device.** Messages, commands, heartbeat and document files travel only through `git` against `github.com` (not `api.github.com`). REST calls are used only for setup and control (creating the repo, setting Secrets, cancelling a run).
- **Chat list and conversations:** tabs for All / Groups / Channels / Private, search, send, reply, edit, delete, optional "mark as read".
- **Documents are fully supported** (downloaded in the Action, optionally encrypted, committed to git, served from your local disk).
- **Photos, GIFs, videos and stickers are never downloaded or displayed**, not even thumbnails. You only see a "🖼 Photo" card with the caption. The only image exception is the current chat/profile picture, fetched as a tiny 48 px JPEG for the chat list.
- **End-to-end content encryption** (AES-256-GCM) for everything stored on the bus branch; the Telegram session itself is stored encrypted.
- **Automatic setup wizard:** finds or creates the repo, uploads the worker code and workflows, creates the orphan `bus` branch, sets Secrets, enables Actions and starts the Listener.
- **Seamless 6-hour handoff** between Action runs, plus a Coordinator safety net that restarts the chain if it drops.
- **Purge tools:** wipe messages, files and avatars on demand or automatically, including a force-pushed orphan `bus` branch.
- **Themes:** five built-in themes, a full theme editor, import/export, and a CSS escape hatch.
- **Bidirectional UI:** layout can be switched between LTR and RTL, and all user content follows its own text direction.
- **No build step:** pure-Python backend and plain HTML/CSS/JS frontend.

## How it works

1. The **local server** (FastAPI, bound to `127.0.0.1`) serves the web UI and keeps a local clone of the `bus` branch in `~/.tg-bridge/bus`.
2. When you send a message or request a file, it is written as a **command** file and pushed to the `bus` branch.
3. The **Listener** (`bot/listener.py`, workflow `sync-worker`) runs in GitHub Actions, polls the branch, executes the commands through Telethon, and writes incoming messages, chat lists, avatars and files back to the branch (compressed and, by default, encrypted).
4. The local server pulls the branch every ~2.5 seconds and updates the UI.
5. The **Coordinator** (`bot/coordinator.py`, workflow `state-check`, every 15 minutes) re-dispatches the Listener if none is running and your local heartbeat is fresh.

Typical round-trip latency is 2 to 8 seconds.

---

## ⚠️ Cost in Actions minutes

The Listener is one continuous job (up to 5 h 45 min, then it hands off to the next job), so it runs roughly **24 hours a day, about 43,000 minutes a month**.

- A **private** repo on GitHub's free plan has a limited monthly quota (about 2,000 minutes at the time of writing), which is only a few dozen hours of connection. **Check your current plan and quota on GitHub's Billing page before long-term use.**
- A **public** repo normally has unlimited minutes on standard runners, but everything in it is public. In that case content encryption is mandatory (and chat names / file sizes can still be guessed from git metadata).
- The app stops the Action by itself when the local server is closed or you press **Disconnect**, so no minutes are wasted.
- In a public repo, GitHub disables `schedule` workflows (the Coordinator) after 60 days of inactivity. Connecting manually still works.

---

## Requirements

- Python **3.10 or newer** (the Action runner uses Python 3.14)
- `git`
- A GitHub account and a personal access token (see [GitHub token](#github-token-pat))
- A Telegram account and an `api_id` / `api_hash` from <https://my.telegram.org> (*API development tools*)
- A place that can reach Telegram **once**, for the initial login (a proxy via `--proxy` also works)

Python dependencies (`requirements.txt`) are all pure Python or ship prebuilt wheels: `fastapi`, `uvicorn` (deliberately **without** the `[standard]` extra), `pydantic`, `requests`, `telethon`, `cryptography`, `pynacl`.

---

## Installation

Clone the repository first:

```bash
git clone https://github.com/Poarthy/telvia.git
cd telvia
```

### Linux / macOS / Windows

```bash
python3 -m venv .venv
. .venv/bin/activate              # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env              # then fill in TG_API_ID and TG_API_HASH
```

### Android (Termux with proot-distro)

On Termux the supported method is **proot-distro + a Python virtual environment (`.venv`)**. Nothing is installed with `pip` directly in Termux itself.

**1. Install proot-distro and a Linux distribution (in Termux):**

```bash
pkg update && pkg upgrade
pkg install proot-distro
proot-distro install ubuntu
```

**2. Log in to the distribution:**

```bash
proot-distro login ubuntu
```

**3. Inside Ubuntu, install Python, venv and git:**

```bash
apt update
apt install -y python3 python3-venv git
```

**4. Get the project and create the `.venv` (still inside Ubuntu):**

```bash
git clone https://github.com/Poarthy/telvia.git
cd telvia
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env                         # set TG_API_ID and TG_API_HASH  (apt install nano if missing)
```

**5. Run it** (the commands in [Setup](#setup-one-time) and [Daily use](#daily-use), always with the `.venv` activated):

```bash
python -m local_server.main --no-browser
```

Then open `http://127.0.0.1:8765` in your **Android browser**. proot shares the phone's network, so the address works as is.

Next time you open Termux:

```bash
proot-distro login ubuntu
cd telvia && . .venv/bin/activate
python -m local_server.main --no-browser
```

Notes for proot:

- All app data (`~/.tg-bridge`: encrypted session, content key, config, local bus clone) lives **inside the Ubuntu root filesystem**. Removing the distro (`proot-distro remove ubuntu`) deletes it, so back it up first if you need it.
- Do **not** install `cryptg` locally; Telethon works with its pure-Python `pyaes` fallback. If `pip install telethon` ever gets stuck on `cryptg`, use: `pip install telethon --no-deps pyaes rsa pyasn1`.
- Keep Termux alive while the server runs (acquire the wakelock from the Termux notification) so Android does not kill the process.

---

## Setup (one time)

Run all commands from the project root with the virtual environment activated.

### 1. Telegram login

From a place that can reach Telegram (add `--proxy socks5://…` if needed; `pysocks` must be installed for proxies):

```bash
python -m tools.login
```

It asks for your phone number, the login code and (if you use one) the two-step password, then asks for a **passphrase**. The session is encrypted with AES-256-GCM and saved to `~/.tg-bridge/session.enc`. The plain-text session is never left anywhere.

### 2. Create a GitHub token

See [GitHub token (PAT)](#github-token-pat).

### 3. Start the local server

```bash
python -m local_server.main       # then open http://127.0.0.1:8765
```

The browser wizard asks for: **token → repo type (private/public) → passphrase → "Start setup"**. The app then:

1. finds or creates the repo (neutral name such as `workspace-notes-7f3a21`),
2. uploads the stripped worker code and workflows to `main`,
3. creates the orphan `bus` branch,
4. sets the Secrets,
5. enables Actions and starts the Listener.

Options: `--port 9000`, `--host 127.0.0.1`, `--no-browser`. The port can also be set in `~/.tg-bridge/config.json`.

## Daily use

```bash
python -m local_server.main
```

It reconnects automatically. When the worker code in your checkout has changed, the app pushes the update to GitHub and restarts the Listener by itself. Stop it with **Ctrl+C** or the **Disconnect** button.

---

## GitHub token (PAT)

The token type is detected from its prefix.

| Type | Required access |
|---|---|
| **Classic** (`ghp_…`) | scopes `repo` (create/read/write repos + Secrets) and `workflow` |
| **Fine-grained** (`github_pat_…`) | on the repo: `Contents: R&W`, `Actions: R&W`, `Secrets: R&W`, `Workflows: R&W`, `Metadata: Read`. (`Workflows` is on top of the basic list: without it GitHub rejects any push that changes `.github/workflows`.) If the app should **create a new repo**, the token also needs account-level `Administration: R&W` at creation time; otherwise create an empty repo yourself and enter its name in the wizard. |

If access is insufficient, the app shows a precise, plain-language message instead of the raw API error.

---

## Themes

Open **Settings → Theme**.

- **Built-in presets:** *Ember* (default, dark), *Paper* (light), *Midnight*, *Forest* and *High contrast*.
- **Editor:** every color token (surfaces, text, borders, accent, status, messages, composer), the three font stacks, sizes and radii, and the background grid.
- **Derive a palette** from just three colors (canvas, ink, accent) with a contrast slider.
- **Import / export** themes as JSON (`tg-bridge-theme` format), up to 30 custom themes.
- **Custom CSS** for complete freedom. Everything except this field is validated against strict allow-lists; custom CSS is contained by the server's Content-Security-Policy and cannot load remote resources.
- If a theme ever breaks the page, open `http://127.0.0.1:8765/?safe=1` to get the built-in theme back.

## Text direction (LTR / RTL)

The interface language is English, but it is fully bidirectional.

- **Layout direction.** Use the **⇄** button in the sidebar header (or *Settings → Layout direction*) to mirror the whole interface: sidebar, bubbles, composer, toasts, dialogs, wizard and the mobile slide-in view. The choice is saved as `ui_dir` (`"ltr"` or `"rtl"`, default `"ltr"`) in `~/.tg-bridge/config.json`.
- **Content direction.** Independently of the layout, every piece of user content (chat titles, previews, sender names, message text, reply quotes, file names, the search box and the composer) uses `dir="auto"`, so Persian, Arabic or Hebrew text renders correctly even in an LTR layout and mixed-direction text is ordered correctly. Fields that must always read left-to-right (token, passphrase, repo name) are forced to `ltr`.
- **Implementation.** The stylesheet uses only logical properties (`margin-inline-*`, `inset-inline-*`, `text-align: start/end`), so no per-direction CSS is duplicated.

---

## Security

- The Telegram session equals **full login to your account**. It is encrypted with AES-256-GCM (key derived from the passphrase with PBKDF2, 600k iterations). The encrypted blob is stored in the `TG_SESSION_ENC` Secret and the passphrase in a separate `TG_SESSION_KEY` Secret (defense in depth; the main protection is GitHub Secrets themselves, which cannot be read back and are masked in logs).
- **Anyone who can change this repo's workflows or the code on `main` can reach the Secrets, and therefore your account.** Enable 2FA on GitHub, give the token only the access it needs, add no collaborators, and rotate the token after long-term use.
- The encrypted session backup lives only on your device (`~/.tg-bridge/session.enc`), never in the repo. If the Secret is deleted: `python -m tools.upload_session_secret` (no new login needed).
- The content-encryption key (`~/.tg-bridge/content.key`) is local only, plus a Secret named `TG_CONTENT_KEY` for the Listener. **If you lose it, encrypted data on the bus cannot be read.**
- If you *Terminate* the session in Telegram, the Listener reports `session_invalid` and you must run `python -m tools.login` again.
- The local server listens only on `127.0.0.1`, validates `Host` / `Origin` and requires a custom header on state-changing requests (defense against CSRF and DNS rebinding). **It has no authentication. Never expose it on `0.0.0.0`.**
- Action logs never print message text, chat names or Secrets.
- Files containing secrets (`.env`, `session.enc`, `content.key`, `config.json`) are written with mode `600` and are listed in `.gitignore`.

---

## Behavior and limits

- **Traffic and commits:** every heartbeat and message is a commit. Every 250 commits (`squash_every_commits`) the Listener compacts the `bus` history into one commit.
- **Files:** a document is downloaded in the Action, encrypted (if encryption is on) and committed to git. The default limit is 90 MB (GitHub rejects anything above 100 MB). Above the limit only metadata and a "file exceeds the allowed size limit" message are kept. Downloads from the UI are served from the **local disk** after a `git pull`.
- **Profile pictures:** fetched only in Telegram's small rendition, recompressed to a 48×48 JPEG at quality 28, rejected if larger than 12 KiB, cached on the bus (encrypted when encryption is on) and refreshed when Telegram's photo id changes (15-minute background check). They are served only from the local server with a versioned private browser cache.
- **Read receipts:** opening a chat does **not** mark it as read on Telegram unless you press *Mark as read* or enable it in the settings.
- **Latency:** about 2 to 8 seconds per round trip (the git cycle runs about every 2.5 seconds on each side).
- **Device clock:** the Coordinator and the Listener compare the local side's liveness, so keep your device clock on "automatic".
- **Disconnecting:** closing the local server (Ctrl+C) sends a `stop` command and cancels the run. If it is killed without a clean exit, the Listener shuts itself down after 2 minutes without hearing from the local side.
- **6-hour handoff:** the Listener stops at 5 h 45 min and triggers the next job. Two simultaneous connections to one session never happen (`concurrency` with `cancel-in-progress: false`).

## Purge

**Settings → Purge data** deletes messages, the chat list, cached avatars, `files_index` and all files, and **force-pushes** an orphan commit to the `bus` branch. Options: purge automatically every N hours, and a separate option for "big files only".

GitHub may keep old objects until garbage collection (a few days). For guaranteed removal, delete the repository and create it again.

---

## Configuration reference

**`.env`** (see `.env.example`):

| Variable | Description |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | Telegram API credentials from <https://my.telegram.org> |
| `TG_PROXY` | Optional, used only by `tools.login` (e.g. `socks5://127.0.0.1:1080`) |
| `TG_BRIDGE_HOME` | Optional data directory (default `~/.tg-bridge`) |

**Local data directory** (`~/.tg-bridge`):

| Path | Content |
|---|---|
| `session.enc` | Encrypted Telegram session |
| `content.key` | Content-encryption key |
| `config.json` | Local settings, token, repo info, themes |
| `bus/` | Local clone of the `bus` branch |

**Useful settings** (Settings dialog or `config.json`): `encrypt`, `max_file_mb`, `auto_download_documents`, `mark_read_on_open`, `auto_connect`, `auto_start`, `shutdown_on_disconnect`, `poll_interval`, `startup_backfill_chats`, `max_dialogs`, `auto_purge_hours`, `big_file_mb`, `big_file_purge_hours`, `ui_dir`, `theme_active`, `port`.

---

## Project structure

```
common/             protocol, AES-GCM crypto, git wrapper for the bus, tiny GitHub API client
bot/                Listener (Telethon), commands, Coordinator, pushed to the repo's main branch
local_server/       FastAPI server + UI (HTML/CSS/JS, no build step), theme engine, bootstrap
tools/              login.py, upload_session_secret.py
.github/workflows/  listener.yml (sync-worker), coordinator.yml (state-check)
tests/              network-independent tests with a fake Telethon
```

## Tests

Run from the project root with the virtual environment activated:

```bash
python -m tests.test_common        # encryption, GitBus against a local remote
python -m tests.test_hub           # hub and coordinator (fake Telegram)
python -m tests.test_listener      # listener (fake Telegram)
python -m tests.test_themes        # theme engine
python -m tests.test_theme_api     # theme HTTP API (needs fastapi)
python -m tests.test_launch        # server launch (needs fastapi)
```

Sample data in the tests includes right-to-left (Hebrew) text.

## Troubleshooting

| Problem | What to do |
|---|---|
| `python3 -m venv` fails on Ubuntu | `apt install python3-venv` |
| Browser does not open automatically | Use `--no-browser` and open `http://127.0.0.1:8765` manually |
| "Could not start the server" | Another copy is running; stop it or use `--port` |
| Action stops with `session_invalid` | Run `python -m tools.login` again |
| `TG_SESSION_ENC` Secret was deleted | `python -m tools.upload_session_secret` |
| Push rejected for `.github/workflows` | Token lacks `workflow` scope / `Workflows: R&W` |
| UI unreadable after a custom theme | Open `/?safe=1` and fix or delete the theme |

---

## Legal and ethical notes

- Use it **only for your own personal account**. No spam or bulk sending, and no access to other people's accounts.
- Other people's private chats are private data; keeping them (even encrypted) carries responsibility, which is why purging is a core part of the design.
- Continuous use of GitHub Actions to relay personal traffic may conflict with GitHub's Terms of Service / Actions usage limits and with Telegram's terms. Checking their latest versions is your responsibility.
- This project is not affiliated with Telegram or GitHub.
