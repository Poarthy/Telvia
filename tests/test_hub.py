"""Tests for the Hub (local side) and the Coordinator against a local git remote; no network and no FastAPI.

Run:  python tests/test_hub.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME = Path(tempfile.mkdtemp())
os.environ["TG_BRIDGE_HOME"] = str(HOME)

from bot import coordinator as CO  # noqa: E402
from common import ghapi  # noqa: E402
from common import protocol as P  # noqa: E402
from common.crypto import LINE_PREFIX, Codec, encrypt_file  # noqa: E402
from common.gitbus import BusError, GitBus  # noqa: E402
from local_server import config as C  # noqa: E402
from local_server.bus_sync import Hub, HubError  # noqa: E402
from local_server.github_client import GitHubError  # noqa: E402


def setup():
    tmp = Path(tempfile.mkdtemp())
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    C.Config.remote_url = property(lambda self: str(remote))
    cfg = C.Config(tmp / "cfg.json")
    cfg.update({"github_token": "x", "repo_owner": "o", "repo_name": "r"}, save=False)
    key = C.ensure_content_key()
    P.set_path_key(key)
    listener = GitBus(tmp / "listener", str(remote))
    listener.init()
    listener.stage("w", P.STATUS, P.dumps({"state": "running", "ts": time.time()}))
    listener.flush("init")
    hub = Hub(cfg)

    def no_api():
        raise GitHubError(0, "offline")
    hub._gh = no_api
    return tmp, remote, cfg, key, listener, hub


def test_hub_end_to_end():
    tmp, remote, cfg, key, listener, hub = setup()
    codec = Codec(key, True)

    hub._tick()
    assert hub.bus is not None and hub.status()["state"] == "disconnected"

    # chats (encrypted); right-to-left titles must round-trip unchanged
    listener.stage("w", P.CHATS_INDEX, codec.enc_json({"updated": 1, "chats": [{"id": 5, "title": "דוד", "type": "private"}]}))
    # messages: msg + edit + del + duplicate (Hebrew text exercises right-to-left content)
    lines = b"".join(codec.enc_line(P.pack_record(r)) for r in [
        {"op": "msg", "id": 1, "chat_id": 5, "date": 10, "text": "ראשון", "out": False},
        {"op": "msg", "id": 2, "chat_id": 5, "date": 11, "text": "שני", "out": True},
        {"op": "msg", "id": 2, "chat_id": 5, "date": 11, "text": "שני", "out": True},
        {"op": "msg", "id": 3, "chat_id": 5, "date": 12, "text": "שלום עולם", "out": False,
         "media_type": "document", "file": {"id": "5_3", "name": "a.pdf", "size": 3, "mime": "application/pdf"}},
        {"op": "edit", "id": 1, "chat_id": 5, "text": "ראשון (נערך)", "edit_date": 20},
        {"op": "del", "id": 2, "chat_id": 5},
    ])
    listener.stage("a", P.messages_path(5), lines)
    # encrypted file + index
    plain = tmp / "plain.bin"
    plain.write_bytes(os.urandom(2_500_000))
    enc = tmp / "plain.enc"
    encrypt_file(key, str(plain), str(enc), aad=b"file:5_3")
    listener.stage("c", "data/files/5_3/blob.enc", src=str(enc))
    listener.stage("w", P.FILES_INDEX, codec.enc_json({"files": {"5_3": {
        "file_id": "5_3", "name": "a.pdf", "size": 2_500_000, "status": "ready", "encrypted": True}}}))
    listener.stage("w", P.ACTION_HB, P.dumps({"seq": 1, "ts": time.time(), "run_id": "1"}))
    listener.flush("data")

    hub._tick()
    assert hub.chats()["chats"][0]["title"] == "דוד"
    msgs = hub.messages(5)
    assert [m["id"] for m in msgs] == [1, 3]
    assert msgs[0]["text"] == "ראשון (נערך)" and msgs[0]["edit_date"] == 20
    assert msgs[1]["file"]["status"] == "ready"
    name, size, it = hub.file_stream("5_3")
    assert name == "a.pdf" and b"".join(it) == plain.read_bytes()
    for bad in ("../x", "5_3/../../x", ""):
        try:
            hub.file_stream(bad)
        except HubError:
            pass
        else:
            raise AssertionError(bad)

    # action heartbeat: after a change → connected
    listener.stage("w", P.ACTION_HB, P.dumps({"seq": 2, "ts": time.time(), "run_id": "1"}))
    listener.flush("hb")
    hub._tick()
    assert hub.status()["state"] == "connected"

    # command: encrypted, the text must not leak
    cid = hub.command("send", chat_id=5, text="טקסט סודי")
    hub._tick()
    listener.sync()
    raw = listener.read(P.command_path(cid))
    assert raw.startswith(LINE_PREFIX.encode()) and "סודי".encode() not in raw
    assert codec.dec_json(raw)["text"] == "טקסט סודי"
    # result
    listener.stage("w", P.RESULTS, codec.enc_json({"results": {cid: {"ok": True, "out": {"message_id": 9}}}}))
    listener.flush("res")
    hub._tick()
    assert hub.result(cid)["ok"] is True and hub.result("0000000000000-deadbeef") is None

    # the local heartbeat is written and the Coordinator sees it as alive
    listener.sync()
    alive, why = CO.local_is_alive(listener, 180)
    assert alive, why

    # disconnect: stop + inactive heartbeat
    res = hub.disconnect()
    listener.sync()
    assert P.loads(listener.read(P.STOP_CMD))["ts"] > 0
    assert P.loads(listener.read(P.LOCAL_HB))["active"] is False
    assert hub.status()["state"] == "stopped" and res["notes"]
    alive, why = CO.local_is_alive(listener, 180)
    assert not alive and "disconnected" in why
    # reconnect: stop is removed and the heartbeat is active
    out = hub.connect()
    assert out["dispatched"] is False and "Coordinator" in out["note"]
    listener.sync()
    assert listener.read(P.STOP_CMD) is None and P.loads(listener.read(P.LOCAL_HB))["active"] is True

    # settings are written to the bus
    hub.apply_settings({"max_file_mb": 50})
    hub._tick()
    listener.sync()
    assert P.loads(listener.read(P.CONFIG))["max_file_mb"] == 50

    # full purge
    hub.purge("all")
    listener.sync()
    assert listener.read(P.messages_path(5)) is None and listener.read(P.FILES_INDEX) is None
    assert not listener.exists("data/files") and P.loads(listener.read(P.PURGE_MARKER))["ts"] > 0
    assert subprocess.run(["git", "rev-list", "--count", "bus"], cwd=remote, capture_output=True,
                          text=True).stdout.strip() == "1"
    hub._tick()     # the list_chats command is sent after a purge
    listener.sync()
    assert [n for n in listener.listdir("commands") if P.is_command_file(n)]


def test_purge_big_files_only():
    tmp, remote, cfg, key, listener, hub = setup()
    codec = Codec(key, True)
    cfg.update({"big_file_mb": 1}, save=False)
    listener.stage("w", P.FILES_INDEX, codec.enc_json({"files": {
        "1_1": {"file_id": "1_1", "size": 3_000_000, "status": "ready"},
        "1_2": {"file_id": "1_2", "size": 1000, "status": "ready"}}}))
    listener.stage("w", "data/files/1_1/blob.enc", b"big")
    listener.stage("w", "data/files/1_2/blob.enc", b"small")
    listener.stage("a", P.messages_path(1), codec.enc_line({"op": "msg", "id": 1, "chat_id": 1, "date": 1, "text": "keep"}))
    listener.flush("d")
    hub._tick()
    assert hub.purge("big")["removed"] == 1
    listener.sync()
    assert not listener.exists("data/files/1_1") and listener.exists("data/files/1_2")
    assert listener.read(P.messages_path(1)) is not None                 # messages untouched
    assert codec.dec_json(listener.read(P.FILES_INDEX))["files"]["1_1"]["status"] == "purged"


def test_hub_needs_key_error():
    tmp, remote, cfg, key, listener, hub = setup()
    listener.stage("w", P.CHATS_INDEX, Codec(key, True).enc_json({"chats": []}))
    listener.flush("x")
    hub._tick()
    (HOME / "content.key").unlink()
    hub._rebuild_codec()
    hub._cache.clear()
    try:
        hub.chats()
    except HubError as e:
        assert "key" in str(e)
    else:
        raise AssertionError("expected HubError")
    C.ensure_content_key()      # a new (different) key must not silently make the data look corrupted
    hub._rebuild_codec()
    hub._cache.clear()
    try:
        hub.chats()
    except Exception:
        pass
    else:
        raise AssertionError("wrong key must fail loudly")


def test_stale_action_detection_and_autostart_state():
    tmp, remote, cfg, key, listener, hub = setup()
    hub._tick()
    listener.stage("w", P.ACTION_HB, P.dumps({"seq": 1, "ts": 1, "run_id": "1"}))
    listener.flush("hb")
    hub._tick()
    assert hub.status()["state"] == "disconnected"       # stale timestamp (ts=1)
    listener.stage("w", P.ACTION_HB, P.dumps({"seq": 2, "ts": 2, "run_id": "1"}))
    listener.flush("hb")
    hub._tick()
    assert hub.status()["state"] == "connected"
    hub.act_changed = time.monotonic() - 250              # unchanged for > STALE_AFTER
    assert hub.status()["state"] == "disconnected"
    hub.last_dispatch = time.monotonic()
    assert hub.status()["state"] == "connecting"
    hub.actions_state = "disabled"
    assert hub.status()["state"] == "disabled"
    hub.actions_state = "enabled"
    status_doc = {"state": "session_invalid", "ts": 5}
    hub.status_doc = status_doc
    hub.act_changed = -1e9
    assert hub.status()["state"] == "session_invalid"


def test_coordinator():
    tmp, remote, cfg, key, listener, hub = setup()
    listener.stage("w", P.LOCAL_HB, P.dumps({"seq": 1, "ts": time.time(), "active": True}))
    listener.flush("hb")
    os.environ.update({"REPO": "o/r", "GH_TOKEN": "t", "REF_NAME": "main"})
    alive, _ = CO.local_is_alive(listener, 180)
    assert alive
    listener.stage("w", P.LOCAL_HB, P.dumps({"seq": 2, "ts": time.time() - 500, "active": True}))
    listener.flush("hb")
    alive, why = CO.local_is_alive(listener, 180)
    assert not alive and "stale" in why
    # session invalid → not started until a new one is uploaded
    listener.stage("w", P.STATUS, P.dumps({"state": "session_invalid", "ts": 100}))
    listener.flush("st")
    assert CO.session_blocked(listener) is True
    listener.stage("w", P.SESSION_REFRESHED, P.dumps({"ts": 200}))
    listener.flush("ref")
    assert CO.session_blocked(listener) is False


def test_coordinator_main_dispatch_decisions():
    """Runs CO.main with a local remote and a fake ghapi."""
    tmp, remote, cfg, key, listener, hub = setup()
    listener.stage("w", P.LOCAL_HB, P.dumps({"seq": 1, "ts": time.time(), "active": True}))
    listener.flush("hb")
    os.environ.update({"REPO": "o/r", "GH_TOKEN": "t", "REF_NAME": "main"})
    orig_bus = CO.GitBus
    CO.GitBus = lambda workdir, url, token=None, **k: orig_bus(workdir, str(remote), None)
    calls = []
    o1, o2 = ghapi.active_runs, ghapi.dispatch_listener
    try:
        ghapi.active_runs = lambda repo, token: [{"id": 9}]
        ghapi.dispatch_listener = lambda repo, token, ref="main": calls.append(ref) or True
        assert CO.main() == 0 and calls == []                  # already running (fast exit before git clone!)
        ghapi.active_runs = lambda repo, token: []
        assert CO.main() == 0 and calls == ["main"]            # not running + local alive → dispatch
        listener.stage("w", P.LOCAL_HB, P.dumps({"seq": 2, "ts": time.time(), "active": False}))
        listener.flush("off")
        calls.clear()
        assert CO.main() == 0 and calls == []                  # the user disconnected
    finally:
        CO.GitBus = orig_bus
        ghapi.active_runs, ghapi.dispatch_listener = o1, o2


def test_failed_auto_purge_is_not_retried_on_every_tick():
    tmp, remote, cfg, key, listener, hub = setup()
    cfg.update({"auto_purge_hours": 1, "big_file_purge_hours": 1, "last_purge_ts": 0, "last_big_purge_ts": 0}, save=False)
    calls = {"all": 0, "big": 0}

    def failing(scope="all"):
        calls[scope] += 1
        raise BusError("simulated outage")
    hub.purge = failing
    for _ in range(6):
        hub._scheduled_purge()
    assert calls["all"] == 1, calls
    cfg.update({"auto_purge_hours": 0}, save=False)
    for _ in range(6):
        hub._scheduled_purge()
    assert calls["big"] == 1, calls
    # ... but it is retried after the back-off, not only after a whole period
    cfg.update({"last_big_purge_ts": time.time() - 3600 + 5, "big_file_purge_hours": 1}, save=False)   # 595 s after the failure
    hub._scheduled_purge()
    assert calls["big"] == 1
    cfg.update({"last_big_purge_ts": time.time() - 3600 - 5}, save=False)       # 605 s after the failure
    hub._scheduled_purge()
    assert calls["big"] == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("Hub and Coordinator tests passed")
