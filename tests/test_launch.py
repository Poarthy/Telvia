"""Tests for how the local server opens the browser (needs fastapi, like test_theme_api).

Regression: on a machine without a graphical session (Termux, SSH, headless Linux) Python's webbrowser used to pick a
terminal browser (w3m/lynx/links), which took over the server's terminal and showed the raw HTML without CSS/JS.

Run:  python -m tests.test_launch
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TG_BRIDGE_HOME"] = tempfile.mkdtemp()

import webbrowser  # noqa: E402

from local_server import main as M  # noqa: E402


def _fake_text_browser(name: str) -> Path:
    d = Path(tempfile.mkdtemp())
    exe = d / name
    exe.write_text("#!/bin/sh\necho launched >> \"$LAUNCH_LOG\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return d


def test_terminal_browser_is_never_started():
    if sys.platform in ("win32", "darwin"):
        return
    log = Path(tempfile.mkdtemp()) / "launch.log"
    saved = dict(os.environ)
    try:
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "TERMUX_VERSION"):
            os.environ.pop(var, None)
        os.environ.update({"TERM": "xterm", "LAUNCH_LOG": str(log),
                           "PATH": f"{_fake_text_browser('w3m')}{os.pathsep}{os.environ['PATH']}"})
        webbrowser.register_standard_browsers()
        assert isinstance(webbrowser.get(), webbrowser.GenericBrowser)       # the old code would have used this one
        assert M._gui_browser_available() is False
        assert M.open_browser("http://127.0.0.1:8765") is False
        assert not log.exists(), "a terminal browser was launched"
    finally:
        os.environ.clear()
        os.environ.update(saved)
        webbrowser.register_standard_browsers()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("Launch tests passed")
