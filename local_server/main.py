"""Local server (FastAPI). Run:  python -m local_server.main

Browser: http://127.0.0.1:8765  (the port can be changed in the settings or with --port)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from common import protocol as P  # noqa: E402
from common.gitbus import BusError  # noqa: E402
from local_server import config as C  # noqa: E402
from local_server import themes as TH  # noqa: E402
from local_server.bootstrap import code_fingerprint, push_code, run_bootstrap  # noqa: E402
from local_server.bus_sync import Hub, HubError  # noqa: E402
from local_server.github_client import GitHub, GitHubError  # noqa: E402

STATIC = Path(__file__).parent / "static"

C.load_env()
cfg = C.Config()
hub = Hub(cfg)
_server = None  # uvicorn.Server


# ---------------------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------------------
class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=500)


class SetupIn(BaseModel):
    visibility: str = "private"
    repo_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    existing_repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    passphrase: str = Field(default="", max_length=200)
    encrypt: bool = True


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    reply_to: int | None = None


class EditIn(BaseModel):
    message_id: int
    text: str = Field(min_length=1, max_length=4096)


class DeleteIn(BaseModel):
    message_ids: list[int] = Field(min_length=1, max_length=100)


class FetchIn(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset_id: int = 0


class FileReqIn(BaseModel):
    chat_id: int
    message_id: int


class SettingsIn(BaseModel):
    encrypt: bool | None = None
    max_file_mb: int | None = Field(default=None, ge=1, le=95)
    auto_download_documents: bool | None = None
    mark_read_on_open: bool | None = None
    auto_connect: bool | None = None
    auto_start: bool | None = None
    shutdown_on_disconnect: bool | None = None
    auto_purge_hours: float | None = Field(default=None, ge=0, le=24 * 365)
    big_file_mb: int | None = Field(default=None, ge=1, le=95)
    big_file_purge_hours: float | None = Field(default=None, ge=0, le=24 * 365)
    poll_interval: float | None = Field(default=None, ge=1, le=30)
    startup_backfill_chats: int | None = Field(default=None, ge=0, le=100)
    max_dialogs: int | None = Field(default=None, ge=10, le=1000)
    ui_dir: str | None = Field(default=None, pattern=r"^(ltr|rtl)$")


class PurgeIn(BaseModel):
    scope: str = Field(default="all", pattern=r"^(all|big)$")


class ThemeIn(BaseModel):
    """A theme as sent by the editor. Deep validation (colors, fonts, ranges) lives in themes.normalize_theme."""
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    name: str = ""
    base: str = "dark"
    colors: dict[str, Any] = {}
    fonts: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    pattern: bool = True
    css: str = ""
    activate: bool = True


class ThemeImportIn(BaseModel):
    theme: dict[str, Any]


class ThemeActiveIn(BaseModel):
    id: str = Field(max_length=40)


class DeriveIn(BaseModel):
    canvas: str = Field(max_length=20)
    ink: str = Field(max_length=20)
    accent: str = Field(max_length=20)
    base: str = Field(default="dark", pattern=r"^(dark|light)$")
    contrast: float = Field(default=1.0, ge=0.5, le=2.0)


# ---------------------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------------------
def request_exit() -> None:
    if _server:
        _server.should_exit = True


def _auto_connect() -> None:
    try:
        if cfg.configured:
            cur_hash = code_fingerprint()
            if cfg.get("deployed_code_hash") != cur_hash:
                try:
                    pushed = push_code(
                        cfg["github_token"], cfg["repo_owner"], cfg["repo_name"],
                        cfg["instance_id"] or "sync", hub.log,
                    )
                    cfg.update({"deployed_code_hash": cur_hash}, save=True)
                    if pushed:
                        hub.log("Updated worker code on GitHub; restarting Listener…")
                        with contextlib.suppress(Exception):
                            gh = GitHub(cfg["github_token"])
                            for run in gh.active_runs(cfg["repo_owner"], cfg["repo_name"]):
                                gh.cancel_run(cfg["repo_owner"], cfg["repo_name"], run["id"])
                            time.sleep(1.5)
                except Exception as e:  # noqa: BLE001
                    hub.log(f"Code update check skipped: {str(e)[:120]}")
            if cfg["auto_connect"]:
                hub.connect()
    except (GitHubError, HubError, BusError) as e:
        hub.last_error = str(getattr(e, "message", e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.shutdown_cb = request_exit
    hub.start()
    threading.Thread(target=_auto_connect, daemon=True).start()
    yield
    if cfg.configured and not hub.user_stopped:
        with contextlib.suppress(Exception):
            hub.disconnect()
    hub.stop()


app = FastAPI(title="tg-bridge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Guards against DNS rebinding and CSRF: this server has full control of the Telegram account."""
    host = (urlparse("//" + (request.headers.get("host") or "")).hostname or "").lower()
    allowed = {"127.0.0.1", "localhost", "::1", str(cfg["host"]).lower()}
    if host not in allowed:
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).netloc.lower() != (request.headers.get("host") or "").lower()):
            return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
        if request.headers.get("x-requested-with") != "tg-bridge":
            return JSONResponse({"error": "missing header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/chats/") and request.url.path.endswith("/avatar"):
        resp.headers["Cache-Control"] = "private, max-age=2592000, immutable"
    else:
        resp.headers["Cache-Control"] = "no-store"
    if not request.url.path.startswith("/api/files/"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
    return resp


@app.exception_handler(GitHubError)
async def _gh_err(request: Request, exc: GitHubError):
    return JSONResponse({"error": exc.message}, status_code=400)


@app.exception_handler(HubError)
async def _hub_err(request: Request, exc: HubError):
    return JSONResponse({"error": str(exc)}, status_code=409)


@app.exception_handler(BusError)
async def _bus_err(request: Request, exc: BusError):
    return JSONResponse({"error": f"git error: {str(exc)[:200]}"}, status_code=502)


@app.exception_handler(TH.ThemeError)
async def _theme_err(request: Request, exc: TH.ThemeError):
    return JSONResponse({"error": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------------------
# Status and settings
# ---------------------------------------------------------------------------------------
def public_settings() -> dict:
    keys = ("encrypt", "max_file_mb", "auto_download_documents", "mark_read_on_open", "auto_connect", "auto_start",
            "shutdown_on_disconnect", "auto_purge_hours", "big_file_mb", "big_file_purge_hours", "poll_interval",
            "startup_backfill_chats", "max_dialogs", "ui_dir", "theme_active", "theme_rev", "visibility", "port")
    return {k: cfg[k] for k in keys}


@app.get("/api/state")
def state():
    snap = hub.snapshot()
    detail = hub.status_doc.get("detail")
    if detail == "session_decrypt_failed" and hub.setup_state != "running":
        snap["configured"] = False
    snap.update({
        "settings": public_settings(),
        "token_set": bool(cfg["github_token"]),
        "session_file": C.SESSION_ENC_PATH.exists(),
        "api_keys_env": bool(os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH")),
        "setup": {"state": hub.setup_state, "log": hub.setup_log[-60:]},
        "action_status": hub.status_doc.get("state"),
        "action_detail": detail,
    })
    return snap


@app.post("/api/settings")
def save_settings(body: SettingsIn):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    hub.apply_settings(values)
    return {"settings": public_settings()}


# ---------------------------------------------------------------------------------------
# Themes (see local_server/themes.py). Cosmetic only: nothing here touches the bus or the network.
# ---------------------------------------------------------------------------------------
@app.get("/theme.css")
def theme_stylesheet(request: Request):
    """The active theme as CSS, loaded after style.css. ``?safe=1`` serves the built-in theme (recovery from a bad theme)."""
    return Response(TH.css_for(cfg, safe="safe" in request.query_params), media_type="text/css")


@app.get("/api/theme/schema")
def theme_schema():
    return TH.schema()


@app.get("/api/themes")
def themes_list():
    return TH.list_themes(cfg)


@app.post("/api/themes")
def themes_save(body: ThemeIn):
    raw = body.model_dump(exclude={"activate"})
    return {"theme": TH.save_theme(cfg, raw, activate=body.activate), "rev": cfg["theme_rev"]}


@app.post("/api/themes/import")
def themes_import(body: ThemeImportIn):
    return {"theme": TH.import_theme(cfg, body.theme), "rev": cfg["theme_rev"]}


@app.post("/api/themes/preview")
def themes_preview(body: ThemeIn):
    return {"css": TH.preview_css(body.model_dump(exclude={"activate"}))}


@app.post("/api/themes/derive")
def themes_derive(body: DeriveIn):
    return {"colors": TH.derive_palette(body.canvas, body.ink, body.accent, body.base, body.contrast)}


@app.post("/api/themes/active")
def themes_activate(body: ThemeActiveIn):
    return {"theme": TH.set_active(cfg, body.id), "rev": cfg["theme_rev"]}


@app.delete("/api/themes/{theme_id}")
def themes_delete(theme_id: str):
    TH.delete_theme(cfg, theme_id)
    return {"rev": cfg["theme_rev"]}


@app.get("/api/themes/{theme_id}/export")
def themes_export(theme_id: str):
    theme = TH.get_theme(cfg, theme_id)
    if theme is None:
        raise TH.ThemeError("That theme no longer exists.")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", theme["name"]).strip("-") or "theme"
    return JSONResponse(TH.export_theme(theme), headers={"Content-Disposition": f'attachment; filename="{slug}.tgtheme.json"'})


# ---------------------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------------------
@app.post("/api/setup/token")
def setup_token(body: TokenIn):
    token = body.token.strip()
    info = GitHub(token).check_token()
    cfg.update({"github_token": token})
    return info


@app.post("/api/setup/run")
def setup_run(body: SetupIn):
    if hub.setup_state == "running":
        raise HubError("Setup is already running.")
    if not cfg["github_token"]:
        raise HubError("Enter the GitHub token first.")
    if not C.SESSION_ENC_PATH.exists():
        raise HubError("session.enc was not found; run \"python -m tools.login\" first.")
    if not body.passphrase.strip():
        raise HubError("Enter your session passphrase so Secrets can be uploaded to GitHub.")
    hub.setup_state, hub.setup_log = "running", []

    def work() -> None:
        try:
            run_bootstrap(cfg, visibility=body.visibility, repo_name=body.repo_name,
                          existing_repo=body.existing_repo, passphrase=body.passphrase,
                          encrypt=body.encrypt, progress=hub.setup_log.append)
            hub.reset_bus()
            hub.setup_state = "done"
            if cfg["auto_connect"]:
                try:
                    hub.connect()
                except (GitHubError, HubError, BusError) as e:
                    hub.setup_log.append("⚠ " + str(getattr(e, "message", e)))
        except GitHubError as e:
            hub.setup_log.append("❌ " + e.message)
            hub.setup_state = "error"
        except Exception as e:  # noqa: BLE001
            hub.setup_log.append(f"❌ {type(e).__name__}: {str(e)[:200]}")
            hub.setup_state = "error"

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


@app.post("/api/connect")
def connect():
    return hub.connect()


@app.post("/api/disconnect")
def disconnect():
    return hub.disconnect()


# ---------------------------------------------------------------------------------------
# Chats and messages
# ---------------------------------------------------------------------------------------
@app.get("/api/chats")
def chats():
    return hub.chats()


@app.post("/api/chats/refresh")
def refresh_chats():
    return {"command_id": hub.command("list_chats")}


@app.get("/api/chats/{chat_id}/messages")
def messages(chat_id: int, limit: int = 80):
    return {"messages": hub.messages(chat_id, limit)}


@app.get("/api/chats/{chat_id}/avatar")
def avatar(chat_id: int):
    src = hub.avatar_stream(chat_id)
    headers = {"X-Content-Type-Options": "nosniff"}
    if isinstance(src, Path):
        return FileResponse(src, media_type="image/jpeg", headers=headers)
    return StreamingResponse(src, media_type="image/jpeg", headers=headers)


@app.post("/api/chats/{chat_id}/fetch")
def fetch(chat_id: int, body: FetchIn):
    return {"command_id": hub.command("get_messages", chat_id=chat_id, limit=body.limit, offset_id=body.offset_id)}


@app.post("/api/chats/{chat_id}/send")
def send(chat_id: int, body: SendIn):
    return {"command_id": hub.command("send", chat_id=chat_id, text=body.text, reply_to=body.reply_to)}


@app.post("/api/chats/{chat_id}/edit")
def edit(chat_id: int, body: EditIn):
    return {"command_id": hub.command("edit", chat_id=chat_id, message_id=body.message_id, text=body.text)}


@app.post("/api/chats/{chat_id}/delete")
def delete(chat_id: int, body: DeleteIn):
    return {"command_id": hub.command("delete", chat_id=chat_id, message_ids=body.message_ids)}


@app.post("/api/chats/{chat_id}/mark_read")
def mark_read(chat_id: int):
    return {"command_id": hub.command("mark_read", chat_id=chat_id)}


@app.get("/api/commands/{cid}")
async def command_result(cid: str):
    """Long-polls up to 2.4s so the browser receives the command result the exact millisecond it arrives."""
    try:
        P.command_path(cid)
    except ValueError:
        raise HubError("Invalid command id") from None
    return {"result": await hub.wait_result_async(cid, timeout=2.4)}


# ---------------------------------------------------------------------------------------
# Files (documents only)
# ---------------------------------------------------------------------------------------
@app.post("/api/files/request")
def file_request(body: FileReqIn):
    return {"command_id": hub.command("download_file", chat_id=body.chat_id, message_id=body.message_id)}


@app.get("/api/files/{file_id}/download")
def file_download(file_id: str):
    name, size, src = hub.file_stream(file_id)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }
    if isinstance(src, Path):
        return FileResponse(src, media_type="application/octet-stream", headers=headers)
    return StreamingResponse(src, media_type="application/octet-stream", headers=headers)


# ---------------------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------------------
@app.post("/api/purge")
def purge(body: PurgeIn):
    return hub.purge(body.scope)


# ---------------------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    # /?safe=1 ignores the saved theme (and its custom CSS) so a theme that made the UI unusable can be fixed or deleted.
    if "safe" in request.query_params:
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace('href="/theme.css"', 'href="/theme.css?safe=1"')
        return HTMLResponse(html)
    return FileResponse(STATIC / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


_TEXT_BROWSERS = frozenset({"www-browser", "links", "links2", "elinks", "lynx", "w3m", "browsh", "retawq"})


def _gui_browser_available() -> bool:
    """True only when opening the URL would show the real web app.

    Python's webbrowser falls back to terminal browsers (w3m, lynx, links, elinks) when there is no graphical
    session (Termux, SSH, headless Linux). Those run in the foreground of THIS terminal and render the raw HTML
    without CSS or JavaScript, so the app looks broken and the server's console is hijacked.
    """
    if sys.platform not in ("darwin", "win32", "cygwin") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    name = os.path.basename(str(getattr(browser, "name", "") or "")).lower()
    return type(browser).__name__ != "Elinks" and name not in _TEXT_BROWSERS


def open_browser(url: str) -> bool:
    """Opens ``url`` in a graphical browser if there is one. Never starts a terminal browser. Returns success."""
    with contextlib.suppress(Exception):
        if os.environ.get("TERMUX_VERSION") or os.path.isdir("/data/data/com.termux"):
            opener = shutil.which("termux-open-url") or shutil.which("termux-open")
            if opener:
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            return False
        if _gui_browser_available():
            return bool(webbrowser.open(url))
    return False


def main() -> None:
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")

    import uvicorn

    ap = argparse.ArgumentParser(description="Telvia local server")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true", help="Do not open the web browser automatically")
    args = ap.parse_args()
    if args.host or args.port:
        cfg.update({k: v for k, v in (("host", args.host), ("port", args.port)) if v}, save=False)
    host, port = cfg["host"], int(cfg["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠ Warning: the server is listening on a non-local address; this interface has full control of the Telegram account and has no authentication!")
    url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    global _server
    _server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    server = _server

    def announce() -> None:
        for _ in range(100):                      # wait (up to 10 s) until the port is really bound
            if server.started:
                break
            time.sleep(0.1)
        else:
            return
        print(f"→ {url}   (Server is running! Keep this window open. Press Ctrl+C to stop.)", flush=True)
        if not args.no_browser and host in ("127.0.0.1", "localhost") and not open_browser(url):
            print("  Open the address above in your browser.", flush=True)

    threading.Thread(target=announce, daemon=True).start()
    try:
        server.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopped cleanly.")
    except SystemExit:
        if not server.started:
            print(f"✖ Could not start the server on {url}. Is another copy already running? Try --port.", file=sys.stderr)
            sys.exit(1)
        print("\nStopped cleanly.")


if __name__ == "__main__":
    main()
