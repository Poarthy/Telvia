"""Local settings: ~/.tg-bridge/config.json (mode 600) and .env loading."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("TG_BRIDGE_HOME") or (Path.home() / ".tg-bridge")).expanduser()

SESSION_ENC_PATH = HOME / "session.enc"
CONTENT_KEY_PATH = HOME / "content.key"
BUS_DIR = HOME / "bus"
CONFIG_PATH = HOME / "config.json"

DEFAULTS = {
    "github_token": "",
    "repo_owner": "",
    "repo_name": "",
    "instance_id": "",
    "visibility": "private",
    "default_branch": "main",
    "deployed_code_hash": "",       # local SHA-256 of bot/common/workflows so push_code only runs when files change
    # local server
    "host": "127.0.0.1",
    "port": 8765,
    "poll_interval": 2.0,
    "auto_connect": True,           # connect the Action on startup
    "auto_start": True,             # restart the Action if it drops (and the user did not disconnect it)
    "shutdown_on_disconnect": False,
    "ui_dir": "ltr",                # interface layout direction: "ltr" or "rtl"
    "theme_active": "ember",        # id of a built-in theme or of one of custom_themes (see local_server/themes.py)
    "custom_themes": [],            # user-made themes (validated by themes.normalize_theme when read)
    "theme_rev": 0,                 # bumped on every theme change so open tabs know to reload /theme.css
    # data
    "encrypt": True,
    "max_file_mb": 90,
    "auto_download_documents": False,
    "mark_read_on_open": False,     # opening a chat does not mark it as read on Telegram
    "startup_backfill_chats": 15,
    "max_dialogs": 200,
    # purge
    "auto_purge_hours": 0,          # 0 = off
    "big_file_mb": 20,
    "big_file_purge_hours": 0,
    "last_purge_ts": 0,
    "last_big_purge_ts": 0,
}

# Keys that are also written to the bus branch (config.json) so the Listener can read them
BUS_KEYS = ("encrypt", "max_file_mb", "auto_download_documents", "startup_backfill_chats", "max_dialogs")


def load_env() -> None:
    """Loads .env files into os.environ (without overriding existing variables)."""
    for p in (PROJECT_ROOT / ".env", HOME / ".env"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def secure_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update({k: v for k, v in raw.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
            if not self.data.get("github_token"):
                self.data["github_token"] = os.environ.get("GITHUB_TOKEN", "")

    def save(self) -> None:
        with self._lock:
            secure_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    @property
    def lock(self) -> threading.RLock:
        """Hold this around a read-modify-write of a value (e.g. the list of custom themes)."""
        return self._lock

    def __getitem__(self, k):
        with self._lock:
            return self.data[k]

    def get(self, k, default=None):
        with self._lock:
            return self.data.get(k, default)

    def update(self, values: dict, save: bool = True) -> None:
        with self._lock:
            for k, v in values.items():
                if k in DEFAULTS:
                    self.data[k] = v
            if save:
                self.save()

    @property
    def configured(self) -> bool:
        return bool(self.get("github_token") and self.get("repo_owner") and self.get("repo_name"))

    @property
    def repo_full(self) -> str:
        return f"{self['repo_owner']}/{self['repo_name']}"

    @property
    def remote_url(self) -> str:
        return f"https://github.com/{self.repo_full}.git"

    def bus_config(self) -> dict:
        with self._lock:
            return {k: self.data[k] for k in BUS_KEYS}


def load_content_key() -> bytes | None:
    from common.crypto import key_from_b64
    try:
        return key_from_b64(CONTENT_KEY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_content_key() -> bytes:
    from common.crypto import generate_key, key_to_b64
    key = load_content_key()
    if key is None:
        key = generate_key()
        secure_write(CONTENT_KEY_PATH, key_to_b64(key))
    return key
