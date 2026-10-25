"""One-time Telegram login that creates the encrypted session (Native Windows Console Edition).

Run (from the project root):   python -m tools.login [--proxy socks5://127.0.0.1:1080]

- Uses standard visible console input while typing (so typing, Backspace, Ctrl+V, and right-click
  paste work 100% normally on Windows), then automatically masks the line with '********' after Enter.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.crypto import encrypt_with_passphrase  # noqa: E402
from local_server import config as C  # noqa: E402


def _setup_windows_console() -> bool:
    """Configures UTF-8 output and enables ANSI escape sequences on Windows 10/11."""
    if os.name != "nt":
        return True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE = -11
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


_VT_ENABLED = _setup_windows_console()


def ask_line(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_secret(prompt: str, allow_empty: bool = False) -> str:
    """Reads password using standard visible input() so typing & pasting never freeze on Windows,
    then immediately overwrites the terminal line with '********' once Enter is pressed.
    """
    while True:
        try:
            # Off Windows (Linux, macOS, Termux) the standard hidden prompt works fine, so the secret is never shown.
            if os.name != "nt" and sys.stdin.isatty():
                import getpass
                val = getpass.getpass(prompt).strip("\r\n")
            else:
                val = input(prompt).strip("\r\n")
        except EOFError:
            val = ""
        if val or allow_empty:
            if _VT_ENABLED and val and os.name == "nt":
                # Move cursor up 1 line, clear the plaintext password from screen, and show stars
                sys.stdout.write(f"\x1b[1A\r\x1b[2K{prompt}{'*' * min(len(val), 12)}\n")
                sys.stdout.flush()
            return val
        print("  Input cannot be empty. Please type your password and press Enter.")


def parse_proxy(url: str):
    from urllib.parse import urlparse
    try:
        import socks  # PySocks
    except ImportError:
        sys.exit("PySocks is required for a proxy:  pip install pysocks")
    u = urlparse(url)
    kinds = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}
    if u.scheme not in kinds or not u.hostname or not u.port:
        sys.exit("Invalid proxy format; example: socks5://127.0.0.1:1080")
    return (kinds[u.scheme], u.hostname, u.port, True, u.username, u.password)


def ask_passphrase() -> str:
    while True:
        p1 = ask_secret("Choose a passphrase to encrypt the session (at least 8 characters): ")
        if len(p1) < 8:
            print("  Too short (must be at least 8 characters).")
            continue
        p2 = ask_secret("Repeat the passphrase: ")
        if p2 != p1:
            print("  They did not match. Please try again.")
            continue
        return p1


async def interactive_login(client) -> None:
    from telethon import errors

    await client.connect()
    if await client.is_user_authorized():
        return

    while True:
        phone = ask_line("Phone number (with country code, e.g. +98... or +1...): ").replace(" ", "")
        if phone:
            break

    print("Requesting verification code from Telegram…")
    sent = await client.send_code_request(phone)
    phone_code_hash = sent.phone_code_hash

    need_2fa = False
    while True:
        code = ask_line("Verification code Telegram sent you: ").replace(" ", "").replace("-", "")
        if not code:
            continue
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            break
        except errors.SessionPasswordNeededError:
            need_2fa = True
            break
        except errors.PhoneCodeInvalidError:
            print("  Invalid verification code. Please check and enter it again.")
        except errors.PhoneCodeExpiredError:
            print("  Verification code expired. Requesting a new code…")
            sent = await client.send_code_request(phone)
            phone_code_hash = sent.phone_code_hash

    if need_2fa:
        while True:
            pwd = ask_secret("Two-step password (Cloud Password): ")
            try:
                await client.sign_in(password=pwd)
                break
            except errors.PasswordHashInvalidError:
                print("  Invalid password. Please try again.")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default=os.environ.get("TG_PROXY"), help="e.g. socks5://127.0.0.1:1080")
    args = ap.parse_args()

    C.load_env()
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TG_API_ID") or ask_line("api_id: ")
    api_hash = os.environ.get("TG_API_HASH") or ask_line("api_hash: ")
    if not api_id.isdigit() or len(api_hash) < 16:
        sys.exit("api_id must be a number and api_hash a 32-character string (from my.telegram.org).")

    if C.SESSION_ENC_PATH.exists():
        ans = ask_line(f"{C.SESSION_ENC_PATH} already exists. Replace it? [y/N] ").lower()
        if ans != "y":
            return

    proxy = parse_proxy(args.proxy) if args.proxy else None
    client = TelegramClient(
        StringSession(),
        int(api_id),
        api_hash,
        proxy=proxy,
        device_model="Desktop",
        system_version="Windows 11 x64",
        app_version="4.16.8 x64",
        lang_code="en",
        system_lang_code="en-US",
    )
    try:
        await interactive_login(client)
        me = await client.get_me()
        session = client.session.save()
    finally:
        await client.disconnect()

    print(f"\nLogged in as: {me.first_name or ''} (id={me.id})")

    passphrase = ask_passphrase()
    C.secure_write(C.SESSION_ENC_PATH, encrypt_with_passphrase(session, passphrase))
    C.ensure_content_key()
    print(f"\nEncrypted session saved: {C.SESSION_ENC_PATH}")
    print(f"Content encryption key: {C.CONTENT_KEY_PATH}")
    print("\nNext step: run the local server (python -m local_server.main) and finish the setup wizard.")
    print("This session is equivalent to full access to your account; keep session.enc and the passphrase somewhere safe.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
