"""Tests for the Listener with a fake Telethon and a local git remote (no network).

Run:  python tests/test_listener.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_telethon as ft  # noqa: E402

ft.install()

from bot import listener as L  # noqa: E402
from common import ghapi  # noqa: E402
from common import protocol as P  # noqa: E402
from common.crypto import (  # noqa: E402
    LINE_PREFIX, Codec, encrypt_with_passphrase, generate_key, iter_decrypt_file, key_to_b64,
)
from common.gitbus import GitBus, Op  # noqa: E402

KEY = generate_key()
PASS = "test-pass-123"


class Env:
    def __init__(self, cfg: dict | None = None, *, encrypt=True, env: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.local = GitBus(self.tmp / "local", str(self.remote))
        self.local.init()
        bus_cfg = {**P.DEFAULT_BUS_CONFIG, "encrypt": encrypt, "max_file_mb": 1, **(cfg or {})}
        self.local.stage("w", P.CONFIG, P.dumps(bus_cfg))
        self.local.stage("w", P.LOCAL_HB, P.dumps({"seq": 0, "ts": time.time(), "active": True}))
        self.local.flush("init")
        P.set_path_key(KEY if encrypt else None)
        self.codec = Codec(KEY, encrypt)
        self.seq = 0
        self.last_beat = time.monotonic()
        os.environ.update({
            "REMOTE_URL": str(self.remote), "TG_SESSION_ENC": encrypt_with_passphrase("sess", PASS),
            "TG_SESSION_KEY": PASS, "TG_API_ID": "1", "TG_API_HASH": "h" * 32, "TG_CONTENT_KEY": key_to_b64(KEY),
            "REPO": "o/r", "GH_TOKEN": "", "POLL_SECONDS": "0.1", "RUN_ID": "42",
        })
        os.environ.pop("HANDOFF_AFTER_SECONDS", None)
        os.environ.update(env or {})
        ft.FakeClient.instances.clear()
        ft.FakeClient.unauthorized = False

    def populate(self, client_setup):
        """A FakeClient is built each time; settings are applied to the class before connect."""
        orig = ft.FakeClient.__init__

        def init(c, *a, **k):
            orig(c, *a, **k)
            client_setup(c)
        ft.FakeClient.__init__ = init
        self._orig = orig

    def restore(self):
        if hasattr(self, "_orig"):
            ft.FakeClient.__init__ = self._orig

    # -- behaviour of the "local server" -----------------------------------------------------------
    def beat(self):
        self.seq += 1
        self.last_beat = time.monotonic()
        self.local.stage("w", P.LOCAL_HB, P.dumps({"seq": self.seq, "ts": time.time(), "active": True}))

    def send_cmd(self, typ, **kw) -> str:
        cid = P.new_command_id()
        self.local.stage("w", P.command_path(cid), self.codec.enc_json({"type": typ, "ts": time.time(), **kw}))
        self.beat()
        self.local.flush("cmd")
        return cid

    async def wait(self, pred, timeout=15, beat=True):
        end = time.time() + timeout
        while time.time() < end:
            await asyncio.to_thread(self.local.sync)
            if pred():
                return True
            if beat and (time.monotonic() - self.last_beat >= 1.2):
                self.beat()
                await asyncio.to_thread(self.local.flush, "beat")
            await asyncio.sleep(0.15)
        return False

    def result(self, cid):
        doc = self.codec.dec_json(self.local.read(P.RESULTS), {}) or {}
        return (doc.get("results") or {}).get(cid)

    def messages(self, chat_id):
        return [P.unpack_record(r) for r in self.codec.dec_lines(self.local.read(P.messages_path(chat_id)))]

    def status(self):
        return P.loads(self.local.read(P.STATUS), {}) or {}


def basic_setup(c: "ft.FakeClient") -> None:
    last = ft.make_msg(5, 100, "הודעה אחרונה", date=1_700_000_500)
    c.dialogs = [
        ft.make_dialog(100, "אברהם", "private", unread=2, last=last, photo_id=101),
        ft.make_dialog(-200, "קבוצת עבודה", "group", photo_id=202),
        ft.make_dialog(-300, "News", "channel", photo_id=303),
    ]
    doc_file = ft.make_file("report.pdf", 5000)
    big_file = ft.make_file("huge.zip", 3 * 1024 * 1024, "application/zip", ".zip")
    c.history[100] = [
        ft.make_msg(1, 100, "שלום"),
        ft.make_msg(2, 100, "Hello there", out=True),
        ft.make_msg(3, 100, "look", kind="photo", file=ft.make_file("p.jpg", 10, "image/jpeg", ".jpg")),
        ft.make_msg(4, 100, "doc", kind="document", file=doc_file),
        ft.make_msg(6, 100, "big", kind="document", file=big_file),
        ft.make_msg(7, 100, "clip", kind="video", file=ft.make_file("v.mp4", 10, "video/mp4", ".mp4")),
        ft.make_msg(8, 100, "", kind="sticker"),
    ]
    c.payloads[(100, 4)] = os.urandom(5000)
    c.payloads[(100, 6)] = b"x"


async def start(env: Env, setup=basic_setup):
    env.populate(setup)
    lis = L.Listener()
    task = asyncio.create_task(lis.run())
    return lis, task


async def test_full_flow():
    env = Env()
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running"), env.status()
        client = ft.FakeClient.instances[-1]
        assert client.parse_mode is None

        # the chat list is encrypted and is read correctly
        raw = env.local.read(P.CHATS_INDEX)
        assert raw.startswith(LINE_PREFIX.encode()) and "אברהם".encode() not in raw
        chats = env.codec.dec_json(raw)["chats"]
        assert {c["type"] for c in chats} == {"private", "group", "channel"}
        assert {c.get("avatar") for c in chats} == {"101", "202", "303"}
        assert client.profile_downloads == [100, -200, -300]
        for chat_id in (100, -200, -300):
            avatar = env.local.abs(P.avatar_path(chat_id, True))
            data = b"".join(iter_decrypt_file(KEY, str(avatar), f"av:{chat_id}".encode()))
            assert data.startswith(b"\xff\xd8") and len(data) <= 12 * 1024
            assert not env.local.exists(P.avatar_path(chat_id, False))

        # An unchanged photo id is served from the tiny cache, not downloaded again.
        cid = env.send_cmd("list_chats")
        assert await env.wait(lambda: env.result(cid))
        assert client.profile_downloads == [100, -200, -300]

        # Telegram photo ids are cache versions: a new id replaces only that dialog's tiny avatar.
        client.dialogs[0].entity.photo.photo_id = 404
        cid = env.send_cmd("list_chats")
        assert await env.wait(lambda: env.result(cid))
        assert client.profile_downloads == [100, -200, -300, 100]
        chats = env.codec.dec_json(env.local.read(P.CHATS_INDEX))["chats"]
        assert next(c for c in chats if c["id"] == 100)["avatar"] == "404"

        # backfill: photos/videos/stickers are labels only
        msgs = {m["id"]: m for m in env.messages(100)}
        assert msgs[3]["media_type"] == "photo" and "file" not in msgs[3]
        assert msgs[7]["media_type"] == "video" and msgs[8]["media_type"] == "sticker"
        assert msgs[4]["media_type"] == "document" and msgs[4]["file"]["name"] == "report.pdf"

        fid_4 = P.file_id_for(100, 4)
        fid_6 = P.file_id_for(100, 6)
        fid_3 = P.file_id_for(100, 3)

        # file index: the document is recorded but not downloaded yet
        files = env.codec.dec_json(env.local.read(P.FILES_INDEX))["files"]
        assert files[fid_4]["status"] == "remote" and not client.downloads

        # sending a message with a reply
        cid = env.send_cmd("send", chat_id=100, text="התשובה שלי", reply_to=1)
        assert await env.wait(lambda: env.result(cid))
        assert env.result(cid)["ok"] and client.sent == [(100, "התשובה שלי", 1)]
        assert any(m.get("text") == "התשובה שלי" and m["out"] and m["reply_to"] == 1 for m in env.messages(100))

        # document download: it is encrypted and can be decrypted
        cid = env.send_cmd("download_file", chat_id=100, message_id=4)
        assert await env.wait(lambda: env.result(cid)), "no download result"
        assert env.result(cid)["ok"], env.result(cid)
        files = env.codec.dec_json(env.local.read(P.FILES_INDEX))["files"]
        assert files[fid_4]["status"] == "ready" and files[fid_4]["encrypted"]
        blob = env.local.abs(f"data/files/{fid_4}/blob.enc")
        assert b"".join(iter_decrypt_file(KEY, str(blob), f"file:{fid_4}".encode())) == client.payloads[(100, 4)]

        # document larger than the limit: not downloaded
        before = list(client.downloads)
        cid = env.send_cmd("download_file", chat_id=100, message_id=6)
        assert await env.wait(lambda: env.result(cid))
        assert env.result(cid)["out"]["status"] == "too_large_for_git"
        assert client.downloads == before
        files = env.codec.dec_json(env.local.read(P.FILES_INDEX))["files"]
        assert files[fid_6]["status"] == "too_large_for_git" and files[fid_6]["size"] == 3 * 1024 * 1024
        assert not env.local.exists(f"data/files/{fid_6}")

        # photos/videos are never downloaded
        for mid in (3, 7, 8):
            cid = env.send_cmd("download_file", chat_id=100, message_id=mid)
            assert await env.wait(lambda: env.result(cid))
            assert env.result(cid)["ok"] is False
        assert client.downloads == before
        assert not env.local.exists(f"data/files/{fid_3}")

        # edit and delete
        cid = env.send_cmd("edit", chat_id=100, message_id=1000, text="נערך")
        assert await env.wait(lambda: env.result(cid) and env.result(cid)["ok"])
        assert any(r.get("op") == "edit" and r["text"] == "נערך" for r in env.messages(100))
        cid = env.send_cmd("delete", chat_id=100, message_ids=[1000])
        assert await env.wait(lambda: env.result(cid) and env.result(cid)["ok"])
        assert any(r.get("op") == "del" and r["id"] == 1000 for r in env.messages(100))

        # mark_read only on an explicit command
        assert not any(x.startswith("read:") for x in client.log)
        cid = env.send_cmd("mark_read", chat_id=100)
        assert await env.wait(lambda: env.result(cid))
        assert "read:100" in client.log

        # live message
        await client.emit("NewMessage", message=ft.make_msg(50, -200, "הודעה חיה", sender="Sara"))
        assert await env.wait(lambda: any(m.get("text") == "הודעה חיה" for m in env.messages(-200)))

        # unknown command and corrupted command
        cid = env.send_cmd("hack")
        assert await env.wait(lambda: env.result(cid))
        assert env.result(cid)["ok"] is False
        cid = env.send_cmd("send", chat_id="abc", text="x")
        assert await env.wait(lambda: env.result(cid))
        assert env.result(cid)["ok"] is False

        # commands are deleted
        assert await env.wait(lambda: not [n for n in env.local.listdir("commands") if P.is_command_file(n)])

        # explicit stop
        env.local.stage("w", P.STOP_CMD, P.dumps({"ts": time.time()}))
        env.local.flush("stop")
        code = await asyncio.wait_for(task, 15)
        assert code == 0
        await env.wait(lambda: env.status().get("state") == "stopped_by_command", beat=False)
        assert client.log[-1] == "disconnect" and not client.connected
        hb = P.loads(env.local.read(P.ACTION_HB))
        assert hb["seq"] > 1
    finally:
        env.restore()
        if not task.done():
            task.cancel()


async def test_local_timeout():
    env = Env({"local_timeout": 2})
    env.local.stage("w", P.LOCAL_HB, P.dumps({"seq": 0, "ts": time.time() - 10, "active": True}))
    env.local.flush("stale-init")
    lis, task = await start(env)
    try:
        code = await asyncio.wait_for(task, 20)      # no heartbeat ever arrives from the local side
        assert code == 0
        env.local.sync()
        assert env.status()["state"] == "stopped_by_local_timeout"
        assert not ft.FakeClient.instances[-1].connected
    finally:
        env.restore()


async def test_local_inactive_flag():
    env = Env()
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running")
        env.local.stage("w", P.LOCAL_HB, P.dumps({"seq": 999, "ts": time.time(), "active": False}))
        env.local.flush("inactive")
        assert await asyncio.wait_for(task, 15) == 0
    finally:
        env.restore()


async def test_stale_stop_ignored():
    """A stale stop command (from before this Listener started) must not kill it; it is only deleted."""
    env = Env()
    env.local.stage("w", P.STOP_CMD, P.dumps({"ts": time.time() - 3600}))
    env.local.flush("old-stop")
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running")
        assert await env.wait(lambda: not env.local.exists(P.STOP_CMD))
        assert not task.done()
        env.local.stage("w", P.STOP_CMD, P.dumps({"ts": time.time()}))
        env.local.flush("stop")
        assert await asyncio.wait_for(task, 15) == 0
    finally:
        env.restore()
        if not task.done():
            task.cancel()


async def test_handoff():
    env = Env(env={"HANDOFF_AFTER_SECONDS": "2"})
    calls = []
    orig = ghapi.dispatch_listener
    ghapi.dispatch_listener = lambda repo, token, ref="main": (calls.append((repo, ft.FakeClient.instances[-1].connected)) or True)
    lis, task = await start(env)
    try:
        async def keep():
            while not task.done():
                await asyncio.sleep(1.0)
                if not task.done():
                    env.beat()
                    await asyncio.to_thread(env.local.flush, "beat")

        k = asyncio.create_task(keep())
        assert await asyncio.wait_for(task, 20) == 0
        k.cancel()
        env.local.sync()
        assert calls == [("o/r", False)], calls          # dispatch exactly once, after a complete disconnect
        assert P.loads(env.local.read(P.ACTION_HB))["handoff"] is True
        assert env.status()["state"] == "handoff"
    finally:
        ghapi.dispatch_listener = orig
        env.restore()


async def test_session_invalid():
    env = Env()
    ft.FakeClient.unauthorized = True
    lis, task = await start(env)
    try:
        assert await asyncio.wait_for(task, 20) == 1
        env.local.sync()
        assert env.status()["state"] == "session_invalid"
    finally:
        ft.FakeClient.unauthorized = False
        env.restore()


async def test_plaintext_mode_and_auto_download():
    env = Env({"auto_download_documents": True}, encrypt=False)
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running")
        client = ft.FakeClient.instances[-1]

        def ready():
            doc = P.loads(env.local.read(P.FILES_INDEX), {}) or {}
            return doc.get("files", {}).get("100_4", {}).get("status") == "ready"
        assert await env.wait(ready), "auto download did not finish"
        assert env.local.read("data/files/100_4/report.pdf") == client.payloads[(100, 4)]
        assert env.local.read(P.CHATS_INDEX).startswith(b"{")           # plain text when encryption is off
        assert env.local.read(P.avatar_path(100))[:2] == b"\xff\xd8"
        assert not env.local.exists(P.avatar_path(100, True))
        assert 3 not in client.downloads and 7 not in client.downloads
        env.local.stage("w", P.STOP_CMD, P.dumps({"ts": time.time()}))
        env.local.flush("stop")
        assert await asyncio.wait_for(task, 15) == 0
    finally:
        env.restore()
        if not task.done():
            task.cancel()


async def test_purge_marker_resets_cache():
    env = Env()
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running")
        assert lis.seen
        env.local.squash([Op("w", P.PURGE_MARKER, P.dumps({"ts": 123}))], "purge")
        assert await env.wait(lambda: lis.purge_marker == 123), "listener did not notice purge"
        env.local.stage("w", P.STOP_CMD, P.dumps({"ts": time.time()}))
        env.local.flush("stop")
        assert await asyncio.wait_for(task, 15) == 0
    finally:
        env.restore()
        if not task.done():
            task.cancel()


async def test_malformed_command_type_does_not_drop_other_commands():
    """A command whose "type" is not a string (e.g. a list) used to raise TypeError and silently lose its neighbours."""
    env = Env()
    lis, task = await start(env)
    try:
        assert await env.wait(lambda: env.status().get("state") == "running"), env.status()
        bad = P.new_command_id()
        env.local.stage("w", P.command_path(bad), env.codec.enc_json({"type": ["send"], "ts": time.time()}))
        good = env.send_cmd("mark_read", chat_id=100)           # pushed together with the bad one
        assert await env.wait(lambda: env.result(good) and env.result(bad)), "both commands must get a result"
        assert env.result(good)["ok"] is True
        assert env.result(bad)["ok"] is False and "unknown" in env.result(bad)["error"]
    finally:
        env.restore()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.iscoroutinefunction(fn):
            await fn()
            print("ok ", name)
    print("Listener tests passed")


if __name__ == "__main__":
    asyncio.run(main())
