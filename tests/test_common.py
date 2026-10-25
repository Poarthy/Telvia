"""Network-independent tests: encryption and GitBus logic against a local bare remote.

Run:  python -m tests.test_common
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import protocol as P  # noqa: E402
from common.crypto import (  # noqa: E402
    LINE_PREFIX, Codec, CryptoError, decrypt_bytes, decrypt_with_passphrase, encrypt_bytes,
    encrypt_file, encrypt_with_passphrase, generate_key, is_stream_encrypted, iter_decrypt_file,
)
from common.gitbus import GitBus  # noqa: E402


def test_passphrase():
    tok = encrypt_with_passphrase("1ABC-session-string", "סיסמה")
    assert decrypt_with_passphrase(tok, "סיסמה") == b"1ABC-session-string"
    try:
        decrypt_with_passphrase(tok, "wrong")
    except CryptoError:
        pass
    else:
        raise AssertionError("wrong passphrase accepted")


def test_codec():
    key = generate_key()
    c = Codec(key, True)
    obj = {"text": "שלום עולם " * 10, "n": 1}
    blob = c.enc_json(obj)
    assert blob.startswith(LINE_PREFIX.encode()) and "שלום".encode() not in blob
    assert c.dec_json(blob) == obj
    lines = c.enc_line({"a": 1}) + c.enc_line({"a": 2}) + b"garbage\n"
    assert [r["a"] for r in c.dec_lines(lines)] == [1, 2]
    plain = Codec(None, False)
    assert plain.dec_json(plain.enc_json(obj)) == obj
    assert plain.enc_json(obj).startswith(b"{")
    # encrypted data without a key must raise an error
    try:
        plain.dec_json(blob)
    except CryptoError:
        pass
    else:
        raise AssertionError("encrypted data read without key")
    # wrong key
    try:
        decrypt_bytes(generate_key(), encrypt_bytes(key, b"x"))
    except CryptoError:
        pass
    else:
        raise AssertionError("wrong key accepted")


def test_stream_file():
    key = generate_key()
    d = Path(tempfile.mkdtemp())
    for size in (0, 1, 1024 * 1024, 1024 * 1024 + 7, 3 * 1024 * 1024):
        src, enc = d / f"s{size}", d / f"s{size}.enc"
        src.write_bytes(os.urandom(size))
        encrypt_file(key, str(src), str(enc), aad=b"test-aad")
        assert is_stream_encrypted(str(enc), key)
        assert b"".join(iter_decrypt_file(key, str(enc), aad=b"test-aad")) == src.read_bytes(), size
    # truncation at a chunk boundary must be detected
    src, enc = d / "t", d / "t.enc"
    src.write_bytes(os.urandom(3 * 1024 * 1024))
    encrypt_file(key, str(src), str(enc))
    raw = enc.read_bytes()
    enc.write_bytes(raw[: 12 + 2 * (1024 * 1024 + 16)])
    try:
        b"".join(iter_decrypt_file(key, str(enc)))
    except CryptoError:
        pass
    else:
        raise AssertionError("truncation not detected")


def test_gitbus():
    tmp = Path(tempfile.mkdtemp())
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    a = GitBus(tmp / "a", str(remote))          # plays the Listener
    b = GitBus(tmp / "b", str(remote))          # plays the local side
    assert a.init() is False                    # the branch does not exist yet

    a.stage("w", "data/status.json", b'{"state":"running"}')
    a.stage("a", "data/messages/1.jsonl", b'{"id":1}\n')
    assert a.flush("first") is True
    assert b.init() is True
    assert b.read("data/status.json") == b'{"state":"running"}'

    # both sides write to separate files at the same time → must settle without conflict
    b.stage("w", "commands/c1.json", b"{}")
    a.stage("a", "data/messages/1.jsonl", b'{"id":2}\n')
    assert a.flush("a2")
    assert b.flush("b1")                        # rejected first (non-ff), then replayed
    a.sync()
    assert a.read("commands/c1.json") == b"{}"
    assert a.read("data/messages/1.jsonl") == b'{"id":1}\n{"id":2}\n'

    # delete + append during replay
    a.stage("d", "commands/c1.json")
    assert a.flush("del")
    b.sync()
    assert b.read("commands/c1.json") is None

    # squash: history becomes one commit and the other side rebuilds itself
    for i in range(5):
        a.stage("w", "data/hb.json", str(i).encode())
        a.flush(f"hb{i}")
    a.squash(message="compact")
    count = subprocess.run(["git", "rev-list", "--count", "bus"], cwd=remote, capture_output=True, text=True)
    assert count.stdout.strip() == "1", count.stdout
    b.stage("w", "commands/c2.json", b"{}")
    assert b.flush("after-squash")              # push is rejected → fetch → replay
    a.sync()
    assert a.read("commands/c2.json") == b"{}"
    assert a.read("data/hb.json") == b"4"

    # binary file with copy + cleanup
    blob = tmp / "blob.bin"
    blob.write_bytes(os.urandom(200_000))
    a.stage("c", "data/files/1_5/blob", src=str(blob), cleanup=True)
    assert a.flush("file")
    assert not blob.exists()
    b.sync()
    assert len(b.read("data/files/1_5/blob")) == 200_000

    # purge: delete everything + squash
    b.squash([__import__("common.gitbus", fromlist=["Op"]).Op("d", "data/files"),
              __import__("common.gitbus", fromlist=["Op"]).Op("d", "data/messages")], "purge")
    a.stage("w", "data/status.json", b"{}")
    a.flush("post-purge")
    assert a.read("data/files/1_5/blob") is None and a.read("data/messages/1.jsonl") is None

    # dangerous path
    try:
        a.stage("w", "../evil", b"x")
    except Exception:
        pass
    else:
        raise AssertionError("path traversal allowed")


def test_threads():
    """Several threads stage/flush at once; no operation may be lost."""
    tmp = Path(tempfile.mkdtemp())
    remote = tmp / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    a = GitBus(tmp / "a", str(remote))
    a.init()
    stop = threading.Event()

    def writer():
        for i in range(40):
            a.stage("a", "data/log.txt", f"{i}\n".encode())
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    while not stop.is_set() or a.pending_count():
        a.flush("x")
    t.join()
    a.sync()
    assert len(a.read("data/log.txt").split()) == 40


def test_protocol():
    P.set_path_key(None)
    assert P.safe_filename("../../etc/passwd") == "passwd"
    assert P.safe_filename("a<b>.txt") == "a_b_.txt"
    assert P.safe_filename("") == "file"
    assert P.valid_file_id("-100123_45") and not P.valid_file_id("../x")
    cfg = P.merge_bus_config({"encrypt": False, "max_file_mb": "x", "evil": 1, "local_timeout": 60})
    assert cfg["encrypt"] is False and cfg["max_file_mb"] == 90 and "evil" not in cfg and cfg["local_timeout"] == 60
    cid = P.new_command_id()
    assert P.command_path(cid).startswith("commands/")
    assert P.avatar_path(-100123).endswith("data/avatars/-100123.jpg")
    assert P.avatar_path(-100123, True).endswith("data/avatars/-100123.jpg.enc")

    # Test path obfuscation and record compaction
    key = generate_key()
    P.set_path_key(key)
    assert "-100123" not in P.messages_path(-100123)
    assert "-100123" not in P.avatar_path(-100123, True)
    fid = P.file_id_for(-100123, 45)
    assert "-100123" not in fid and P.valid_file_id(fid)
    P.set_path_key(None)

    orig = {
        "op": "msg", "id": 7, "chat_id": -100, "date": 123, "out": True,
        "sender_id": 9, "sender": "Ali", "text": "hi", "reply_to": None,
        "media_type": None, "fwd": False,
    }
    packed = P.pack_record(orig)
    assert "reply_to" not in packed and packed["o"] == "m"
    assert P.unpack_record(packed) == orig

    try:
        P.command_path("../x")
    except ValueError:
        pass
    else:
        raise AssertionError


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("All tests passed")
