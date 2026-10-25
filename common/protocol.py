"""Constants and helpers shared by the local server and the bot (standard library only).

All the paths below are *inside the bus branch*, not on main.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid

BRANCH = "bus"
WORKFLOW_FILE = "listener.yml"
COORDINATOR_FILE = "coordinator.yml"
META_FILE = ".bridge-meta.json"
CODE_VERSION = "1.5.0"

# --- Paths inside the bus branch ---------------------------------------------
CONFIG = "config.json"                       # settings (plain text, nothing sensitive)
CHATS_INDEX = "data/chats_index.json"        # encrypted
AVATARS_DIR = "data/avatars"                 # 48px cached profile photos, encrypted when enabled
FILES_INDEX = "data/files_index.json"        # encrypted
MESSAGES_DIR = "data/messages"               # every line is compressed + encrypted
FILES_DIR = "data/files"                     # blobs (when encrypted: chunked AES-GCM)
RESULTS = "data/results.json"                # encrypted
SESSION_STATE = "data/session_state.enc"     # encrypted runtime MTProto session state for seamless handoff
STATUS = "data/status.json"                  # plain text (no chat content)
ACTION_HB = "data/action_heartbeat.json"     # plain text
PURGE_MARKER = "data/purge_marker.json"      # plain text

COMMANDS_DIR = "commands"
LOCAL_HB = "commands/local_heartbeat.json"   # plain text
STOP_CMD = "commands/stop.json"              # plain text
SESSION_REFRESHED = "commands/session_refreshed.json"
_RESERVED_COMMAND_FILES = frozenset({"local_heartbeat.json", "stop.json", "session_refreshed.json"})

# --- Timing (optimized for minimum GitHub quota & anti-ban) -------------------
ACTION_HB_INTERVAL = 60      # seconds (piggybacked on data flushes)
LOCAL_HB_INTERVAL = 60       # seconds (piggybacked on command flushes)
STALE_AFTER = 180            # seconds; unchanged for longer than this = disconnected
HANDOFF_AFTER = 5 * 3600 + 45 * 60   # 5 hours 45 minutes

DEFAULT_BUS_CONFIG = {
    "encrypt": True,                 # encrypt bus/data content (mandatory for public repos)
    "max_file_mb": 90,               # max document size to commit to git
    "auto_download_documents": False,
    "startup_backfill_chats": 10,    # number of chats whose recent messages are checked at startup
    "backfill_messages": 20,
    "max_dialogs": 200,
    "local_timeout": STALE_AFTER,
    "squash_every_commits": 250,     # compact history more frequently to keep repo size minimal
}

# --- Clock -------------------------------------------------------------------
_clock_offset = 0.0
_clock_synced = False


def now() -> float:
    """Current time, corrected for the device clock offset."""
    return time.time() + _clock_offset


def set_clock_offset(offset: float) -> None:
    global _clock_offset, _clock_synced
    _clock_offset = float(offset)
    _clock_synced = True


def clock_synced() -> bool:
    return _clock_synced


# --- JSON --------------------------------------------------------------------
def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(data: bytes | str | None, default=None):
    if not data:
        return default
    try:
        return json.loads(data)
    except (ValueError, TypeError):
        return default


# --- Compact message serialization (saves ~40% JSON overhead) ----------------
def pack_record(rec: dict) -> dict:
    """Compacts a message/edit/del record by shortening keys and omitting default/empty values."""
    op = rec.get("op")
    if op == "msg":
        out = {"o": "m", "i": rec["id"], "c": rec["chat_id"], "d": rec.get("date", 0)}
        if rec.get("out"):
            out["u"] = 1
        if rec.get("sender_id") is not None:
            out["s"] = rec["sender_id"]
        if rec.get("sender"):
            out["n"] = rec["sender"]
        if rec.get("text"):
            out["t"] = rec["text"]
        if rec.get("reply_to") is not None:
            out["r"] = rec["reply_to"]
        if rec.get("media_type"):
            out["m"] = rec["media_type"]
        if rec.get("fwd"):
            out["w"] = 1
        if rec.get("edit_date"):
            out["e"] = rec["edit_date"]
        if rec.get("service"):
            out["v"] = 1
        if rec.get("file"):
            out["f"] = rec["file"]
        return out
    if op == "edit":
        return {"o": "e", "i": rec["id"], "c": rec["chat_id"], "t": rec.get("text", ""), "e": rec.get("edit_date", 0)}
    if op == "del":
        return {"o": "d", "i": rec["id"], "c": rec["chat_id"]}
    return rec


def unpack_record(rec: dict) -> dict:
    """Restores a compact record back to its full dictionary format (backward-compatible)."""
    if not isinstance(rec, dict) or "op" in rec:
        return rec
    o = rec.get("o")
    if o == "m":
        out = {
            "op": "msg",
            "id": rec["i"],
            "chat_id": rec["c"],
            "date": rec.get("d", 0),
            "out": bool(rec.get("u")),
            "sender_id": rec.get("s"),
            "sender": rec.get("n"),
            "text": rec.get("t", ""),
            "reply_to": rec.get("r"),
            "media_type": rec.get("m"),
            "fwd": bool(rec.get("w")),
        }
        if "e" in rec:
            out["edit_date"] = rec["e"]
        if "v" in rec:
            out["service"] = True
        if "f" in rec:
            out["file"] = rec["f"]
        return out
    if o == "e":
        return {"op": "edit", "id": rec["i"], "chat_id": rec["c"], "text": rec.get("t", ""), "edit_date": rec.get("e", 0)}
    if o == "d":
        return {"op": "del", "id": rec["i"], "chat_id": rec["c"]}
    return rec


# --- Path obfuscation & IDs --------------------------------------------------
_path_salt: bytes | None = None
_CMD_ID_RE = re.compile(r"^[0-9a-f]{13}-[0-9a-f]{8}$")
_FILE_ID_RE = re.compile(r"^(?:-?\d+_\d+|[0-9a-f]{16,32})$")
_UNSAFE_FN_RE = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")


def set_path_key(key: bytes | None) -> None:
    """Sets the HMAC key used to obfuscate chat_id/file_id in Git tree paths."""
    global _path_salt
    _path_salt = hashlib.blake2s(key, person=b"tgb-path").digest() if key else None


def _obf(tag: str, val: str | int) -> str:
    if not _path_salt:
        return str(val)
    return hmac.new(_path_salt, f"{tag}:{val}".encode("utf-8"), hashlib.blake2s).hexdigest()[:20]


def new_command_id() -> str:
    t_hex = f"{int(time.time() * 1000) & 0xFFFFFFFFFFFF:012x}"
    return f"{t_hex}0-{uuid.uuid4().hex[:8]}"


def command_path(cid: str) -> str:
    if not _CMD_ID_RE.match(cid):
        raise ValueError("bad command id")
    return f"{COMMANDS_DIR}/{cid}.json"


def is_command_file(name: str) -> bool:
    return name.endswith(".json") and name not in _RESERVED_COMMAND_FILES and not name.startswith(".")


def messages_path(chat_id) -> str:
    return f"{MESSAGES_DIR}/{_obf('m', int(chat_id))}.jsonl"


def avatar_path(chat_id, encrypted: bool = False) -> str:
    """Small profile photo cache path for a numeric Telegram dialog id."""
    path = f"{AVATARS_DIR}/{_obf('a', int(chat_id))}.jpg"
    return path + ".enc" if encrypted else path


def valid_file_id(file_id: str) -> bool:
    return bool(_FILE_ID_RE.match(file_id or ""))


def file_id_for(chat_id, message_id) -> str:
    raw = f"{int(chat_id)}_{int(message_id)}"
    return _obf("f", raw) if _path_salt else raw


def safe_filename(name: str | None, fallback: str = "file") -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _UNSAFE_FN_RE.sub("_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:100] + dot + ext[:10]) if dot and len(ext) <= 10 else name[:120]
    return name


def merge_bus_config(raw: dict | None) -> dict:
    cfg = dict(DEFAULT_BUS_CONFIG)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k not in cfg:
                continue
            default = cfg[k]
            if isinstance(default, bool):
                ok = isinstance(v, bool)
            elif isinstance(default, (int, float)):
                ok = isinstance(v, (int, float)) and not isinstance(v, bool)
            else:
                ok = isinstance(v, type(default))
            if ok:
                cfg[k] = v
    return cfg
