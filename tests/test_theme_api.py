"""HTTP-level tests for the theme endpoints: starts the real FastAPI app with uvicorn (lifespan off, so no GitHub / hub).

Needs the packages from requirements.txt (fastapi, uvicorn, pydantic).
Run:  python -m tests.test_theme_api
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TG_BRIDGE_HOME"] = tempfile.mkdtemp()

import uvicorn  # noqa: E402

from local_server import main as M  # noqa: E402

HDR = {"X-Requested-With": "tg-bridge", "Content-Type": "application/json"}


def start() -> tuple[uvicorn.Server, int]:
    server = uvicorn.Server(uvicorn.Config(M.app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"
    return server, server.servers[0].sockets[0].getsockname()[1]


def call(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(HDR if method != "GET" else {})
    h.update(headers or {})
    if host:
        h["Host"] = host
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw.decode("utf-8", "replace")
    return r.status, data, r


def theme(name="API theme", **over):
    ember = M.TH.PRESETS["ember"]
    t = {"name": name, "base": "dark", "colors": {"accent": "#00aa66"}, "fonts": {"sans": "Vazirmatn, Tahoma"},
         "metrics": {"radius_msg": 4}, "pattern": False, "css": ".msg { outline: 1px solid red; }"}
    t.update(over)
    assert ember
    return t


def test_theme_api():
    server, port = start()
    try:
        # stylesheet + schema + listing
        st, css, r = call(port, "GET", "/theme.css")
        assert st == 200 and r.getheader("Content-Type").startswith("text/css") and "Theme: Ember" in css.replace("theme", "Theme")
        assert r.getheader("Cache-Control") == "no-store"
        st, schema, _ = call(port, "GET", "/api/theme/schema")
        assert st == 200 and len(schema["colors"]) > 40 and schema["limits"]["css"] == M.TH.MAX_CSS_CHARS
        st, data, _ = call(port, "GET", "/api/themes")
        assert st == 200 and data["active"] == "ember" and [p["id"] for p in data["presets"]][0] == "ember" and data["custom"] == []

        # guards: the same ones as every other state-changing route
        assert call(port, "POST", "/api/themes", theme(), headers={"X-Requested-With": ""})[0] == 403
        assert call(port, "POST", "/api/themes", theme(), headers={"Origin": "http://evil.example"})[0] == 403
        assert call(port, "GET", "/theme.css", host="evil.example")[0] == 403

        # preview does not persist
        st, pv, _ = call(port, "POST", "/api/themes/preview", theme())
        assert st == 200 and "--accent: #00aa66;" in pv["css"] and ".msg { outline" in pv["css"]
        assert call(port, "GET", "/api/themes")[1]["custom"] == []
        st, err, _ = call(port, "POST", "/api/themes/preview", theme(colors={"accent": "url(http://evil/x)"}))
        assert st == 400 and "Accent" in err["error"]

        # create (activates), then the stylesheet follows
        st, saved, _ = call(port, "POST", "/api/themes", theme())
        assert st == 200 and saved["theme"]["id"] and saved["rev"] >= 1
        tid = saved["theme"]["id"]
        st, css, _ = call(port, "GET", "/theme.css")
        assert "--accent: #00aa66;" in css and "--pattern-body: none;" in css and '--sans: "Vazirmatn", "Tahoma";' in css
        st, css_safe, _ = call(port, "GET", "/theme.css?safe=1")
        assert "--accent: #874c39;" in css_safe and ".msg { outline" not in css_safe
        assert M.public_settings()["theme_active"] == tid

        # validation errors are 400 with a readable message; malformed bodies are 422
        st, err, _ = call(port, "POST", "/api/themes", theme(name=""))
        assert st == 400 and "name" in err["error"]
        st, err, _ = call(port, "POST", "/api/themes", theme(fonts={"sans": "a;} body{display:none"}))
        assert st == 400 and "font" in err["error"]
        assert call(port, "POST", "/api/themes", {"name": "x", "colors": "red"})[0] == 422

        # update in place, switch, export
        st, upd, _ = call(port, "POST", "/api/themes", {**theme("Renamed"), "id": tid, "activate": False})
        assert st == 200 and upd["theme"]["id"] == tid and call(port, "GET", "/api/themes")[1]["custom"][0]["name"] == "Renamed"
        assert call(port, "POST", "/api/themes/active", {"id": "paper"})[0] == 200
        assert "color-scheme: light;" in call(port, "GET", "/theme.css")[1]
        assert call(port, "POST", "/api/themes/active", {"id": "nope"})[0] == 400
        st, exp, r = call(port, "GET", f"/api/themes/{tid}/export")
        assert st == 200 and exp["format"] == "tg-bridge-theme" and "id" not in exp
        assert "attachment" in r.getheader("Content-Disposition") and ".tgtheme.json" in r.getheader("Content-Disposition")
        assert call(port, "GET", "/api/themes/missing/export")[0] == 400

        # import: always a new theme, never activated
        st, imp, _ = call(port, "POST", "/api/themes/import", {"theme": {**exp, "id": tid}})
        assert st == 200 and imp["theme"]["id"] != tid and len(call(port, "GET", "/api/themes")[1]["custom"]) == 2
        assert call(port, "POST", "/api/themes/import", {"theme": {"format": "nope", "name": "x"}})[0] == 400
        assert call(port, "POST", "/api/themes/import", {"theme": "text"})[0] == 422

        # derive
        st, d, _ = call(port, "POST", "/api/themes/derive", {"canvas": "#101820", "ink": "#eeeeee", "accent": "#ff7700", "base": "dark"})
        assert st == 200 and d["colors"]["canvas"] == "#101820" and d["colors"]["accent"] == "#ff7700"
        assert call(port, "POST", "/api/themes/derive", {"canvas": "red", "ink": "#fff", "accent": "#000"})[0] == 400
        assert call(port, "POST", "/api/themes/derive", {"canvas": "#000", "ink": "#fff", "accent": "#000", "base": "x"})[0] == 422
        assert call(port, "POST", "/api/themes/derive", {"canvas": "#000", "ink": "#fff", "accent": "#000", "contrast": 9})[0] == 422

        # delete: built-ins are protected, deleting the active theme falls back to the default
        assert call(port, "DELETE", "/api/themes/ember")[0] == 400
        call(port, "POST", "/api/themes/active", {"id": tid})
        assert call(port, "DELETE", f"/api/themes/{tid}")[0] == 200
        assert call(port, "GET", "/api/themes")[1]["active"] == "ember"
        assert call(port, "DELETE", f"/api/themes/{tid}")[0] == 400

        # safe mode page points at the safe stylesheet; the normal page does not
        st, page, _ = call(port, "GET", "/?safe=1")
        assert st == 200 and 'href="/theme.css?safe=1"' in page
        st, page, _ = call(port, "GET", "/")
        assert st == 200 and 'href="/theme.css"' in page and "/static/theme.js" in page

        # the theme is cosmetic: it must not be sent through /api/settings (and so never reaches the bus)
        assert "theme_active" not in M.SettingsIn.model_fields and "custom_themes" not in M.SettingsIn.model_fields
        assert "theme_active" not in M.C.BUS_KEYS and "custom_themes" not in M.C.BUS_KEYS
    finally:
        server.should_exit = True


if __name__ == "__main__":
    test_theme_api()
    print("ok  test_theme_api")
    print("Theme API tests passed")
