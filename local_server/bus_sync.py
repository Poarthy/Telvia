"""Hub: syncs the local server with the bus branch (plain git only) + controls the Action over REST.

Ultra-low-latency features:
  - O(1) Incremental tail-reader for `.jsonl` message files (works even for <64-byte initial files)
  - Optimistic Local Overlay with automatic rollback if a command fails on Telegram
  - UI-activity-aware adaptive polling (0.3s burst, 0.9s when browser tab is open, 3.5s when tab is closed)
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from pathlib import Path

from common import protocol as P
from common.crypto import Codec, NeedKeyError, is_stream_encrypted, iter_decrypt_file
from common.gitbus import BusError, GitBus, Op
from local_server import config as C
from local_server.github_client import GitHub, GitHubError

MAX_CACHE_ENTRIES = 32
PURGE_RETRY_SECONDS = 600     # a failed automatic purge is retried after this long (not on every loop iteration)


class HubError(Exception):
    pass


class Hub:
    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.bus: GitBus | None = None
        self.codec = Codec(None, False)
        self.net = threading.RLock()           # permission for git network operations (sync/flush/purge)
        self.wake = threading.Event()
        self.stop_evt = threading.Event()
        self.res_cond = threading.Condition()  # notifies waiting HTTP requests the millisecond a result arrives
        self.threads: list[threading.Thread] = []
        self.shutdown_cb: Callable[[], None] | None = None

        self.user_stopped = not cfg["auto_connect"]
        self.started_wall = time.time()
        self.hb_seq = 0
        self.last_hb_push = 0.0
        self.act_key = None
        self.act_changed = -1e9                # monotonic; last time the Action heartbeat changed
        self.act_info: dict = {}
        self.was_connected = False
        self.disconnected_since = 0.0
        self.last_dispatch = -1e9              # monotonic; initialized to -1e9 so fresh OS boot never looks like recent dispatch
        self.last_sync_ok = 0.0
        self.last_error = ""
        self.api_msg = ""
        self.actions_state = "unknown"         # enabled | disabled | unknown
        self.status_doc: dict = {}
        self.setup_state = "idle"              # idle | running | done | error
        self.setup_log: list[str] = []
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._msg_cache: OrderedDict[str, tuple[int, int, bytes, dict[int, dict], list[dict]]] = OrderedDict()
        self._flush_count = 0
        self.state_rev = 0
        self._burst_until = 0.0
        self.last_ui_ping = time.monotonic()

        # Optimistic local state overlays for 0ms UI feedback (with automatic rollback on failure)
        self._opt_lock = threading.Lock()
        self._opt_edits: dict[tuple[int, int], tuple[str, int]] = {}
        self._opt_deletes: dict[int, set[int]] = {}
        self._opt_read: set[int] = set()
        self._opt_cmd_map: dict[str, tuple[str, int, object]] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for fn in (self._main_loop, self._api_loop):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_evt.set()
        self.wake.set()
        with self.res_cond:
            self.res_cond.notify_all()

    def log(self, msg: str) -> None:
        print(f"[hub] {msg}", flush=True)

    # ------------------------------------------------------------------ preparing the bus
    def reset_bus(self) -> None:
        with self.net:
            self.bus = None
            self.act_key, self.act_changed = None, -1e9
            self.status_doc = {}
            self._cache.clear()
            self._msg_cache.clear()
            with self._opt_lock:
                self._opt_edits.clear()
                self._opt_deletes.clear()
                self._opt_read.clear()
                self._opt_cmd_map.clear()
            self.state_rev += 1

    def _rebuild_codec(self) -> None:
        key = C.load_content_key()
        encrypt = bool(self.cfg["encrypt"])
        P.set_path_key(key if encrypt else None)
        self.codec = Codec(key, encrypt)

    def _ensure_bus(self) -> GitBus:
        if self.bus is None:
            bus = GitBus(C.BUS_DIR, self.cfg.remote_url, self.cfg["github_token"], log=self.log)
            if not bus.init():
                raise HubError("The bus branch does not exist on the repo yet (finish the setup wizard).")
            self.bus = bus
            self._rebuild_codec()
            self._stage_bus_config()
            self.state_rev += 1
        return self.bus

    def _stage_bus_config(self) -> None:
        if self.bus:
            self.bus.stage("w", P.CONFIG, P.dumps({**P.DEFAULT_BUS_CONFIG, **self.cfg.bus_config()}))

    def require_bus(self) -> GitBus:
        if self.bus is None:
            raise HubError("Not connected to the repo yet; check the connection status.")
        return self.bus

    # ------------------------------------------------------------------ heartbeat
    def _stage_hb(self, active: bool) -> None:
        self.hb_seq += 1
        self.bus.stage("w", P.LOCAL_HB, P.dumps({"seq": self.hb_seq, "ts": P.now(), "active": active,
                                                 "boot": int(self.started_wall)}))
        self.last_hb_push = time.monotonic()

    def _observe_action(self) -> None:
        hb = P.loads(self.bus.read(P.ACTION_HB), None)
        self.status_doc = P.loads(self.bus.read(P.STATUS), {}) or {}
        if not isinstance(hb, dict):
            return
        key = (hb.get("seq"), hb.get("run_id"), hb.get("ts"))
        self.act_info = hb
        if self.act_key is None:
            self.act_key = key
            age = max(0.0, P.now() - float(hb.get("ts", 0)))
            if age < P.STALE_AFTER and self.status_doc.get("state") in ("running", "starting", "handoff"):
                self.act_changed = time.monotonic() - age
        elif key != self.act_key:
            self.act_key = key
            self.act_changed = time.monotonic()

    # ------------------------------------------------------------------ main loop (git)
    def _main_loop(self) -> None:
        while not self.stop_evt.is_set():
            try:
                self._tick()
            except HubError as e:
                self.last_error = str(e)
            except BusError as e:
                self.last_error = f"git: {str(e)[:200]}"
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            now_m = time.monotonic()
            try:
                base_poll = float(self.cfg["poll_interval"])
            except (TypeError, ValueError):     # hand-edited config.json: must not kill the sync thread
                base_poll = 2.0
            if self.user_stopped:
                interval = max(base_poll, 8.0)
            elif now_m < self._burst_until:
                interval = 0.30
            elif (now_m - self.last_ui_ping) < 15.0:
                interval = min(base_poll, 0.90)
            else:
                interval = max(base_poll, 3.5)
            self.wake.wait(interval)
            self.wake.clear()

    def _tick(self) -> None:
        if not self.cfg.configured:
            return
        if self.bus is not None and self.bus.pending_count() > 0:
            time.sleep(0.015)

        with self.net:
            self._ensure_bus()
            changed = False

            # 1) Flush-First: push outgoing commands immediately before polling remote
            if self.bus.pending_count() > 0:
                if not self.user_stopped and time.monotonic() - self.last_hb_push >= 15.0:
                    self._stage_hb(True)
                if self.bus.flush("sync"):
                    self._flush_count += 1
                    changed = True

            # 2) Sync remote state (uses fast 30ms TCP_NODELAY HTTP ref-peek when idle)
            if self.bus.sync():
                changed = True
            self._observe_action()

            # 3) Periodic heartbeat & maintenance
            if not self.user_stopped and time.monotonic() - self.last_hb_push >= P.LOCAL_HB_INTERVAL:
                self._stage_hb(True)
            self._scheduled_purge()
            if self.bus.flush("sync"):
                self._flush_count += 1
                changed = True

            if changed:
                self.state_rev += 1
                with self.res_cond:
                    self.res_cond.notify_all()
            self.last_sync_ok = time.time()
            self.last_error = ""
            self._track_connection()

    def _track_connection(self) -> None:
        st = self.status()["state"]
        if st == "connected":
            self.was_connected, self.disconnected_since = True, 0.0
        elif st in ("disconnected", "disabled") and self.was_connected:
            self.disconnected_since = self.disconnected_since or time.monotonic()
            if (self.cfg["shutdown_on_disconnect"] and not self.user_stopped
                    and time.monotonic() - self.disconnected_since > P.STALE_AFTER and self.shutdown_cb):
                self.log("The Action disconnected; shutting down per the local server settings.")
                self.shutdown_cb()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        if not self.cfg.configured:
            return {"state": "not_configured", "label": "Not set up"}
        sd = self.status_doc or {}
        if sd.get("state") == "error" and sd.get("detail") == "session_decrypt_failed":
            return {"state": "disconnected", "label": "Secrets missing — run upload_session_secret"}
        age = time.monotonic() - self.act_changed
        fresh = age < P.STALE_AFTER
        if sd.get("state") == "session_invalid" and not fresh:
            return {"state": "session_invalid", "label": "Session invalidated; log in again"}
        if self.actions_state == "disabled":
            return {"state": "disabled", "label": "Actions is disabled on the repo"}
        if self.user_stopped:
            return {"state": "stopped", "label": "Disconnected (by you)"}
        if fresh and sd.get("state") not in ("error", "stopped_by_command", "stopped_by_local_timeout"):
            if self.act_info.get("handoff"):
                return {"state": "connecting", "label": "Handing off to the next run…"}
            return {"state": "connected", "label": "Connected"}
        if time.monotonic() - self.last_dispatch < 240 or (self.last_dispatch < 0 and self.bus is None):
            return {"state": "connecting", "label": "Connecting…"}
        return {"state": "disconnected", "label": "Disconnected"}

    def snapshot(self) -> dict:
        self.last_ui_ping = time.monotonic()
        st = self.status()
        return {
            "configured": self.cfg.configured, "status": st, "repo": self.cfg.repo_full if self.cfg.configured else "",
            "visibility": self.cfg["visibility"], "last_sync": self.last_sync_ok, "error": self.last_error,
            "api_msg": self.api_msg, "pending": self.bus.pending_count() if self.bus else 0,
            "action_run": self.act_info.get("run_id"), "encrypt": self.codec.encrypt,
            "clock_synced": P.clock_synced(), "user_stopped": self.user_stopped,
        }

    # ------------------------------------------------------------------ REST thread (optional)
    def _gh(self) -> GitHub:
        return GitHub(self.cfg["github_token"])

    def _api_loop(self) -> None:
        last_perm = 0.0
        while not self.stop_evt.wait(5):
            if not self.cfg.configured:
                continue
            try:
                now = time.monotonic()
                if now - last_perm >= 60:
                    last_perm = now
                    gh = self._gh()
                    ok = gh.actions_enabled(self.cfg["repo_owner"], self.cfg["repo_name"])
                    self.actions_state = "enabled" if ok else "disabled"
                    self.api_msg = ""
                st = self.status()["state"]
                if (st == "disconnected" and self.cfg["auto_start"] and not self.user_stopped
                        and self.status_doc.get("detail") != "session_decrypt_failed"
                        and now - self.last_dispatch > 300):
                    self._dispatch_if_needed()
            except GitHubError as e:
                self.api_msg = e.message
            except Exception as e:  # noqa: BLE001
                self.api_msg = f"{type(e).__name__}"

    def _dispatch_if_needed(self) -> None:
        gh = self._gh()
        owner, name = self.cfg["repo_owner"], self.cfg["repo_name"]
        self.last_dispatch = time.monotonic()
        if gh.active_runs(owner, name):
            return
        gh.dispatch(owner, name, ref=self.cfg["default_branch"] or "main")

    # ------------------------------------------------------------------ connect / disconnect
    def connect(self) -> dict:
        """Enables the local heartbeat and starts the Listener (if needed)."""
        self.user_stopped = False
        self._burst_until = time.monotonic() + 15.0
        with self.net:
            bus = self._ensure_bus()
            bus.stage("d", P.STOP_CMD)
            self._stage_hb(True)
            bus.flush("connect")
            self.state_rev += 1
        self.wake.set()
        try:
            self._dispatch_if_needed()
            return {"dispatched": True, "note": ""}
        except GitHubError as e:
            self.api_msg = e.message
            return {"dispatched": False,
                    "note": "Could not reach the GitHub API; the heartbeat is active and the Coordinator "
                            "will start the Listener. (" + e.message + ")"}

    def disconnect(self) -> dict:
        """Two layers: a stop command on the bus + cancel through the API."""
        self.user_stopped = True
        notes = []
        try:
            with self.net:
                bus = self._ensure_bus()
                self._stage_hb(False)
                bus.stage("w", P.STOP_CMD, P.dumps({"ts": P.now()}))
                bus.flush("disconnect", max_attempts=3)
                self.state_rev += 1
        except (HubError, BusError) as e:
            notes.append(f"The stop command could not be written to the bus: {e}")
        try:
            gh = self._gh()
            for run in gh.active_runs(self.cfg["repo_owner"], self.cfg["repo_name"]):
                gh.cancel_run(self.cfg["repo_owner"], self.cfg["repo_name"], run["id"])
        except GitHubError as e:
            notes.append(f"Could not cancel the run through the API (the stop command is enough): {e.message}")
        self.wake.set()
        return {"notes": notes}

    # ------------------------------------------------------------------ commands & optimistic overlay
    def command(self, typ: str, **kw) -> str:
        self.last_ui_ping = time.monotonic()
        bus = self.require_bus()
        cid = P.new_command_id()
        bus.stage("w", P.command_path(cid), self.codec.enc_json({"type": typ, "ts": P.now(), **kw}))

        with self._opt_lock:
            if typ == "edit" and "chat_id" in kw and "message_id" in kw:
                c_id, m_id = int(kw["chat_id"]), int(kw["message_id"])
                self._opt_edits[(c_id, m_id)] = (str(kw.get("text", "")), int(P.now()))
                self._opt_cmd_map[cid] = ("edit", c_id, m_id)
                self.state_rev += 1
            elif typ == "delete" and "chat_id" in kw and "message_ids" in kw:
                c_id = int(kw["chat_id"])
                m_ids = {int(x) for x in (kw["message_ids"] or ())}
                self._opt_deletes.setdefault(c_id, set()).update(m_ids)
                self._opt_cmd_map[cid] = ("delete", c_id, m_ids)
                self.state_rev += 1
            elif typ == "mark_read" and "chat_id" in kw:
                c_id = int(kw["chat_id"])
                self._opt_read.add(c_id)
                self._opt_cmd_map[cid] = ("mark_read", c_id, None)
                self.state_rev += 1

        self._burst_until = time.monotonic() + 12.0
        self.wake.set()
        return cid

    def _reconcile_optimistic(self, cid: str, ok: bool) -> None:
        with self._opt_lock:
            info = self._opt_cmd_map.pop(cid, None)
            if not info or ok:
                return
            # Command failed on Telegram: roll back the optimistic UI overlay
            typ, c_id, target = info
            if typ == "edit" and isinstance(target, int):
                self._opt_edits.pop((c_id, target), None)
            elif typ == "delete" and isinstance(target, set):
                s = self._opt_deletes.get(c_id)
                if s:
                    s.difference_update(target)
            elif typ == "mark_read":
                self._opt_read.discard(c_id)
            self.state_rev += 1

    def result(self, cid: str) -> dict | None:
        if self.bus is None:
            return None
        try:
            doc = self._cached(P.RESULTS, lambda raw: self.codec.dec_json(raw, {}), {}) or {}
        except NeedKeyError as e:
            raise HubError(str(e)) from e
        res = (doc.get("results") or {}).get(cid)
        if res is not None:
            self._reconcile_optimistic(cid, bool(res.get("ok")))
            if self.bus and self.bus.pending_count() == 0:
                self._burst_until = min(self._burst_until, time.monotonic() + 1.0)
        return res

    def _wait_result_sync(self, cid: str, timeout: float = 2.4) -> dict | None:
        res = self.result(cid)
        if res is not None:
            return res
        self._burst_until = max(self._burst_until, time.monotonic() + 8.0)
        self.wake.set()
        end = time.monotonic() + timeout
        with self.res_cond:
            while time.monotonic() < end and not self.stop_evt.is_set():
                res = self.result(cid)
                if res is not None:
                    return res
                rem = end - time.monotonic()
                if rem <= 0:
                    break
                self.res_cond.wait(timeout=min(rem, 0.35))
        return self.result(cid)

    async def wait_result_async(self, cid: str, timeout: float = 2.4) -> dict | None:
        return await asyncio.to_thread(self._wait_result_sync, cid, timeout)

    # ------------------------------------------------------------------ reading data (bounded LRU + O(1) tail reader)
    def _cached(self, rel: str, parse: Callable[[bytes], object], default):
        if self.bus is None:
            return default
        bus = self.bus
        try:
            st = bus.abs(rel).stat()
        except FileNotFoundError:
            return default
        key = (st.st_mtime_ns, st.st_size)
        hit = self._cache.get(rel)
        if hit and hit[0] == key:
            self._cache.move_to_end(rel)
            return hit[1]
        raw = bus.read(rel)
        if raw is None:
            return default
        try:
            val = parse(raw)
        except NeedKeyError as e:
            raise HubError("The data is encrypted but the content key is not on this device (~/.tg-bridge/content.key).") from e
        self._cache[rel] = (key, val)
        self._cache.move_to_end(rel)
        while len(self._cache) > MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return val

    def chats(self) -> dict:
        self.last_ui_ping = time.monotonic()
        doc = self._cached(P.CHATS_INDEX, lambda raw: self.codec.dec_json(raw, {}), {}) or {}
        raw_chats = doc.get("chats", [])
        with self._opt_lock:
            if not self._opt_read:
                return {"updated": doc.get("updated", 0), "chats": raw_chats}
            out = []
            for c in raw_chats:
                cid = int(c.get("id", 0))
                if cid in self._opt_read:
                    if int(c.get("unread", 0)) == 0:
                        self._opt_read.discard(cid)
                    else:
                        c = {**c, "unread": 0}
                out.append(c)
            return {"updated": doc.get("updated", 0), "chats": out}

    def files_index(self) -> dict:
        doc = self._cached(P.FILES_INDEX, lambda raw: self.codec.dec_json(raw, {}), {}) or {}
        return doc.get("files", {})

    @staticmethod
    def _apply_msg_records(msgs: dict[int, dict], records: list) -> None:
        for raw_rec in records:
            rec = P.unpack_record(raw_rec)
            op, mid = rec.get("op"), rec.get("id")
            if op == "msg":
                msgs[mid] = rec
            elif op == "edit" and mid in msgs:
                msgs[mid] = {**msgs[mid], "text": rec.get("text", ""), "edit_date": rec.get("edit_date")}
            elif op == "del":
                msgs.pop(mid, None)

    def _read_chat_messages(self, chat_id: int) -> list[dict]:
        """O(1) incremental tail-reader: only reads and decrypts newly appended bytes on `.jsonl` growth."""
        if self.bus is None:
            return []
        rel = P.messages_path(chat_id)
        try:
            p = self.bus.abs(rel)
            st = p.stat()
        except FileNotFoundError:
            self._msg_cache.pop(rel, None)
            return []

        mtime_ns, size = st.st_mtime_ns, st.st_size
        hit = self._msg_cache.get(rel)
        if hit and hit[0] == mtime_ns and hit[1] == size:
            with contextlib.suppress(KeyError):     # another request thread may have just evicted it
                self._msg_cache.move_to_end(rel)
            return hit[4]

        try:
            with self.bus.lock, open(p, "rb") as f:
                head_sig = f.read(64)
                prev_sig = hit[2] if hit else b""
                if hit and size > hit[1] and prev_sig and head_sig[:len(prev_sig)] == prev_sig:
                    f.seek(hit[1])
                    delta_bytes = f.read()
                    msgs = dict(hit[3])
                    self._apply_msg_records(msgs, self.codec.dec_lines(delta_bytes))
                else:
                    f.seek(0)
                    full_bytes = f.read()
                    msgs = {}
                    self._apply_msg_records(msgs, self.codec.dec_lines(full_bytes))
        except NeedKeyError as e:
            raise HubError("The data is encrypted but the content key is not on this device (~/.tg-bridge/content.key).") from e
        except OSError:
            return []

        sorted_list = sorted(msgs.values(), key=lambda m: (m.get("date", 0), m["id"]))
        self._msg_cache[rel] = (mtime_ns, size, head_sig, msgs, sorted_list)
        with contextlib.suppress(KeyError):
            self._msg_cache.move_to_end(rel)
        while len(self._msg_cache) > MAX_CACHE_ENTRIES:
            with contextlib.suppress(KeyError):
                self._msg_cache.popitem(last=False)
        return sorted_list

    def messages(self, chat_id: int, limit: int = 80) -> list[dict]:
        self.last_ui_ping = time.monotonic()
        cid = int(chat_id)
        allm = self._read_chat_messages(cid)
        out = allm[-max(1, min(limit, 500)):]
        files = self.files_index()
        max_mb = self.cfg["max_file_mb"]

        with self._opt_lock:
            del_set = set(self._opt_deletes.get(cid) or ())
            has_edits = bool(self._opt_edits)

        res = []
        for m in out:
            mid = m["id"]
            if del_set and mid in del_set:
                continue
            if has_edits and (cid, mid) in self._opt_edits:
                with self._opt_lock:
                    opt_val = self._opt_edits.get((cid, mid))
                if opt_val:
                    new_text, new_ed = opt_val
                    if m.get("text") == new_text:
                        with self._opt_lock:
                            self._opt_edits.pop((cid, mid), None)
                    else:
                        m = {**m, "text": new_text, "edit_date": new_ed}
            f = m.get("file")
            if f:
                m = dict(m)
                rec = files.get(f["id"], {})
                f = dict(f)
                f["status"] = rec.get("status", "remote")
                f["limit_mb"] = max_mb
                m["file"] = f
            res.append(m)
        return res

    # ------------------------------------------------------------------ files
    def file_stream(self, file_id: str) -> tuple[str, int, Iterator[bytes] | Path]:
        """(file name, size, path or iterator) for downloading from the local disk."""
        if not P.valid_file_id(file_id):
            raise HubError("Invalid file id")
        rec = self.files_index().get(file_id)
        if not rec or rec.get("status") != "ready":
            raise HubError("This file has not been fetched yet")
        bus = self.require_bus()
        names = bus.listdir(f"{P.FILES_DIR}/{file_id}")
        if not names:
            raise HubError("The file content was not found on the bus (it may have been purged)")
        blob = "blob.enc" if "blob.enc" in names else names[0]
        path = bus.abs(f"{P.FILES_DIR}/{file_id}/{blob}")
        name = P.safe_filename(rec.get("name"), "file")
        if is_stream_encrypted(str(path), self.codec.key):
            if not self.codec.key:
                raise HubError("The content key is not on this device")
            return name, int(rec.get("size", 0)), iter_decrypt_file(self.codec.key, str(path), f"file:{file_id}".encode())
        return name, path.stat().st_size, path

    def avatar_stream(self, chat_id: int) -> Iterator[bytes] | Path:
        """Returns the cached 48px dialog avatar, decrypting it only for localhost delivery."""
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            raise HubError("Invalid chat id") from None
        bus = self.require_bus()
        paths = (P.avatar_path(chat_id, True), P.avatar_path(chat_id, False))
        path = next((bus.abs(rel) for rel in paths if bus.abs(rel).is_file()), None)
        if path is None:
            raise HubError("This profile photo is not cached yet")
        if is_stream_encrypted(str(path), self.codec.key):
            if not self.codec.key:
                raise HubError("The content key is not on this device")
            return iter_decrypt_file(self.codec.key, str(path), f"av:{chat_id}".encode())
        return path

    # ------------------------------------------------------------------ settings
    def apply_settings(self, values: dict) -> None:
        public = self.cfg["visibility"] == "public"
        if public:
            values = {**values, "encrypt": True}
        self.cfg.update(values)
        self._rebuild_codec()
        with self.net:
            if self.bus:
                self._stage_bus_config()
            self.state_rev += 1
        self.wake.set()

    # ------------------------------------------------------------------ purge
    def purge(self, scope: str = "all") -> dict:
        """scope=all: messages + files + chats; scope=big: only files larger than the threshold."""
        with self.net:
            bus = self._ensure_bus()
            bus.sync(force_fetch=True)
            ops: list[Op] = []
            removed = 0
            if scope == "all":
                ops += [Op("d", P.MESSAGES_DIR), Op("d", P.FILES_DIR), Op("d", P.AVATARS_DIR), Op("d", P.FILES_INDEX),
                        Op("d", P.CHATS_INDEX), Op("d", P.RESULTS)]
                ops.append(Op("w", P.PURGE_MARKER, P.dumps({"ts": P.now()})))
            else:
                limit = int(self.cfg["big_file_mb"]) * 1024 * 1024
                files = dict(self.files_index())
                for fid, rec in files.items():
                    if rec.get("status") == "ready" and int(rec.get("size", 0)) > limit:
                        ops.append(Op("d", f"{P.FILES_DIR}/{fid}"))
                        rec["status"] = "purged"
                        removed += 1
                if not removed:
                    return {"removed": 0}
                ops.append(Op("w", P.FILES_INDEX, self.codec.enc_json({"files": files})))
            bus.squash(ops, "purge")
            self._cache.clear()
            self._msg_cache.clear()
            with self._opt_lock:
                self._opt_edits.clear()
                self._opt_deletes.clear()
                self._opt_read.clear()
                self._opt_cmd_map.clear()
            self.state_rev += 1
            now = time.time()
            self.cfg.update({"last_purge_ts": now} if scope == "all" else {"last_big_purge_ts": now})
        if scope == "all":
            with contextlib.suppress(HubError):
                self.command("list_chats")
        return {"removed": removed, "scope": scope}

    def _scheduled_purge(self) -> None:
        now = time.time()
        hrs = float(self.cfg["auto_purge_hours"] or 0)
        if hrs > 0 and now - float(self.cfg["last_purge_ts"] or 0) >= hrs * 3600:
            self.log("Automatic purge (everything)…")
            try:
                self.purge("all")
            except (BusError, HubError) as e:
                self.last_error = f"Automatic purge failed: {str(e)[:150]}"
                # Without this the failed purge was retried on EVERY loop iteration (every 0.3-3.5 s): a fetch + force-push storm.
                self.cfg.update({"last_purge_ts": now - hrs * 3600 + min(hrs * 3600, PURGE_RETRY_SECONDS)})
            return
        bh = float(self.cfg["big_file_purge_hours"] or 0)
        if bh > 0 and now - float(self.cfg["last_big_purge_ts"] or 0) >= bh * 3600:
            self.log("Automatic purge of big files…")
            try:
                self.purge("big")
            except (BusError, HubError) as e:
                self.last_error = f"Big-file purge failed: {str(e)[:150]}"
                self.cfg.update({"last_big_purge_ts": now - bh * 3600 + min(bh * 3600, PURGE_RETRY_SECONDS)})
            else:
                self.cfg.update({"last_big_purge_ts": now})
