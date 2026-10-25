"""A fake Telethon (for tests only), so the Listener can run without a network or the real package.

install() puts the `telethon*` modules into sys.modules. Even if the real Telethon is installed it is not used
(the tests deliberately stay fake so that they are deterministic).
"""
from __future__ import annotations

import datetime as dt
import base64
import sys
import types
from types import SimpleNamespace as NS


# A valid tiny JPEG lets avatar-cache tests cover the real image re-encoding path
# without pulling image fixtures into the repository.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNS"
    "Mj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T0//wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
    "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4"
    "eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
    "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqK"
    "mqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDHooormO0//9k="
)


def install() -> None:
    errors = types.ModuleType("telethon.errors")

    class RPCError(Exception):
        def __init__(self, *a, request=None, **k):
            super().__init__(*a)

    for n in ("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
              "UserDeactivatedError", "UserDeactivatedBanError", "AuthKeyDuplicatedError"):
        setattr(errors, n, type(n, (RPCError,), {}))

    events = types.ModuleType("telethon.events")
    for n in ("NewMessage", "MessageEdited", "MessageDeleted"):
        setattr(events, n, type(n, (), {"__init__": lambda self, *a, **k: None}))

    utils = types.ModuleType("telethon.utils")
    utils.get_display_name = lambda e: getattr(e, "name", None) or ""

    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = lambda s="": s

    tl = types.ModuleType("telethon.tl")
    tl_types = types.ModuleType("telethon.tl.types")

    class MessageMediaWebPage:  # noqa: D401
        pass

    tl_types.MessageMediaWebPage = MessageMediaWebPage

    root = types.ModuleType("telethon")
    root.TelegramClient = FakeClient
    root.errors, root.events, root.utils, root.sessions = errors, events, utils, sessions
    root.tl = tl
    for name, mod in (("telethon", root), ("telethon.errors", errors), ("telethon.events", events),
                      ("telethon.utils", utils), ("telethon.sessions", sessions), ("telethon.tl", tl),
                      ("telethon.tl.types", tl_types)):
        sys.modules[name] = mod


def make_msg(id, chat_id, text="", *, out=False, date=1_700_000_000, media=None, kind=None, file=None,
             reply_to=None, sender="Ali"):
    """kind: photo|gif|video|sticker|voice|audio|document|None"""
    m = NS(id=id, chat_id=chat_id, raw_text=text, out=out, sender_id=7, reply_to_msg_id=reply_to,
           date=dt.datetime.fromtimestamp(date, dt.timezone.utc), edit_date=None, action=None, fwd_from=None,
           photo=None, gif=None, video=None, video_note=None, sticker=None, voice=None, audio=None, document=None,
           media=media, file=file)
    if kind:
        m.media = m.media or object()
        setattr(m, kind, True)
    m.get_sender = lambda: _sender(sender)
    return m


async def _sender(name):
    return NS(name=name)


def make_file(name, size, mime="application/pdf", ext=".pdf"):
    return NS(name=name, size=size, mime_type=mime, ext=ext)


class FakeClient:
    instances: list["FakeClient"] = []
    unauthorized = False

    def __init__(self, session, api_id, api_hash, **kw):
        self.session, self.api_id = session, api_id
        self.parse_mode = "md"
        self.handlers: dict[str, list] = {}
        self.dialogs: list[NS] = []
        self.history: dict[int, list] = {}
        self.sent: list[tuple] = []
        self.downloads: list[int] = []
        self.profile_downloads: list[int] = []
        self.payloads: dict[tuple, bytes] = {}
        self.log: list[str] = []
        self.connected = False
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True
        self.log.append("connect")

    async def disconnect(self):
        self.connected = False
        self.log.append("disconnect")

    async def is_user_authorized(self):
        return not FakeClient.unauthorized

    async def get_me(self):
        return NS(id=1)

    def on(self, ev):
        def deco(fn):
            self.handlers.setdefault(type(ev).__name__, []).append(fn)
            return fn
        return deco

    async def emit(self, kind, **kw):
        for fn in self.handlers.get(kind, []):
            await fn(NS(**kw))

    async def iter_dialogs(self, limit=None):
        for d in self.dialogs[:limit]:
            yield d

    async def iter_messages(self, chat_id, limit=None, offset_id=0):
        msgs = sorted(self.history.get(chat_id, []), key=lambda m: -m.id)
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        for m in msgs[:limit]:
            yield m

    async def get_messages(self, chat_id, ids=None):
        return next((m for m in self.history.get(chat_id, []) if m.id == ids), None)

    async def send_message(self, chat_id, text, reply_to=None):
        mid = 1000 + len(self.sent)
        m = make_msg(mid, chat_id, text, out=True, reply_to=reply_to, date=1_700_001_000 + mid)
        self.history.setdefault(chat_id, []).append(m)
        self.sent.append((chat_id, text, reply_to))
        return m

    async def edit_message(self, chat_id, mid, text):
        m = await self.get_messages(chat_id, mid)
        m.raw_text = text
        m.edit_date = dt.datetime.now(dt.timezone.utc)
        return m

    async def delete_messages(self, chat_id, ids, revoke=True):
        self.history[chat_id] = [m for m in self.history.get(chat_id, []) if m.id not in ids]

    async def send_read_acknowledge(self, chat_id):
        self.log.append(f"read:{chat_id}")

    async def download_media(self, m, file=None):
        self.downloads.append(m.id)
        with open(file, "wb") as f:
            f.write(self.payloads[(m.chat_id, m.id)])
        return file

    async def download_profile_photo(self, entity, file=None, download_big=True):
        self.profile_downloads.append(entity.id)
        with open(file, "wb") as f:
            f.write(getattr(entity, "photo_bytes", _TINY_JPEG))
        return file


def make_dialog(id, name, kind="private", unread=0, last=None, pinned=False, photo_id=None):
    entity = NS(id=id, photo=NS(photo_id=photo_id) if photo_id is not None else None, photo_bytes=_TINY_JPEG)
    return NS(id=id, name=name, entity=entity, is_user=kind == "private", is_group=kind == "group",
              is_channel=kind == "channel", unread_count=unread, message=last, pinned=pinned, archived=False)
