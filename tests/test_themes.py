"""Tests for the theme engine (local_server/themes.py); no network and no FastAPI.

Run:  python -m tests.test_themes
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from local_server import config as C  # noqa: E402
from local_server import themes as TH  # noqa: E402

STYLE_CSS = ROOT / "local_server" / "static" / "style.css"


def new_cfg() -> C.Config:
    return C.Config(Path(tempfile.mkdtemp()) / "cfg.json")


def full_theme(name="Mine", base="dark", **over) -> dict:
    ref = TH.PRESETS["ember" if base == "dark" else "paper"]
    t = {"name": name, "base": base, "colors": dict(ref["colors"]), "fonts": dict(ref["fonts"]),
         "metrics": dict(ref["metrics"]), "pattern": True, "css": ""}
    t.update(over)
    return t


def raises(fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except TH.ThemeError as e:
        return str(e)
    raise AssertionError("ThemeError expected")


def root_block() -> dict[str, str]:
    """The ``:root { ... }`` custom properties of style.css."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {m.group(1): " ".join(m.group(2).split()) for m in re.finditer(r"--([a-z0-9-]+):\s*([^;]+);", block)}


def test_style_css_defaults_match_the_ember_theme():
    """style.css is the fallback when /theme.css cannot load: its :root must equal the built-in default theme."""
    root, ember = root_block(), TH.PRESETS["ember"]
    for key in TH.COLOR_KEYS:
        assert key in root, f"--{key} missing from style.css :root"
        assert root[key].lower() == ember["colors"][key].lower(), (key, root[key], ember["colors"][key])
    for key, (_label, _lo, _hi, default, unit, _step, var) in TH.METRICS.items():
        assert root[var] == f"{TH._fmt(default)}{unit}", (var, root[var])
    for key, stack in TH.DEFAULT_FONTS.items():
        assert TH.font_names(root[key]) == TH.font_names(stack), key
    # every var(--x) used by the stylesheet is defined somewhere
    css = STYLE_CSS.read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--([a-z0-9-]+)", css))
    defined = set(root) | {"vv-h", "vv-top", "composer-h"}      # set by app.js
    assert not (used - defined), used - defined


def test_no_hard_coded_colors_left_in_stylesheet():
    css = STYLE_CSS.read_text(encoding="utf-8")
    body = css[css.index("\n}\n") + 3:]                            # everything after the :root block
    hits = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body)
    assert not hits, hits


def test_ember_css_roundtrip():
    css = TH.theme_css(TH.PRESETS["ember"])
    assert css.startswith("/* TG Bridge theme: Ember */") and "color-scheme: dark;" in css
    assert "--canvas: #111110;" in css and "--font-size: 15px;" in css and "--line-height: 1.45;" in css
    assert "--avatar-radius: 50%;" in css and "--pattern-body" not in css
    # normalizing a preset gives back the same theme (the editor round-trips presets)
    again = TH.normalize_theme({k: v for k, v in TH.PRESETS["ember"].items() if k not in ("id", "builtin")})
    assert TH.theme_css({**again}) == css.replace("Ember", again["name"])


def test_pattern_off_and_custom_css():
    t = TH.normalize_theme(full_theme(pattern=False, css=".msg { outline: 1px solid red; }"))
    css = TH.theme_css(t)
    assert "--pattern-body: none;" in css and "--pattern-chat: none;" in css
    assert css.rstrip().endswith(".msg { outline: 1px solid red; }")
    assert css.index("--canvas") < css.index(".msg {")             # custom CSS comes last


def test_color_validation():
    ok = ["#fff", "#FFFFFF", "#1234", "#11223344", "rgb(1, 2, 3)", "rgba(0,0,0,.5)", "hsl(120 40% 50% / 0.5)",
          "oklch(60% 0.1 200)", "Red", "rebeccapurple", "transparent", "  #abc  "]
    for v in ok:
        assert TH.clean_color(v)
    bad = ["", "  ", "#12", "#12345", "#gg0000", "red; } body { display:none", "url(http://evil/x)", "var(--x)",
           "rgb(1,2,3) url(x)", "calc(1px)", "rgb(url(x))", "expression(alert(1))", "notacolor", 5, None, "x" * 100,
           "red /* c */", "#fff;", "rgb(0,0,0)}", "rgba(0,0,0,.5)\n}"]
    for v in bad:
        raises(TH.clean_color, v, "x")


def test_font_validation():
    assert TH.clean_font_stack('Segoe UI, "Noto Sans", Georgia, SANS-SERIF') == '"Segoe UI", "Noto Sans", "Georgia", sans-serif'
    assert TH.clean_font_stack("Vazirmatn, Tahoma") == '"Vazirmatn", "Tahoma"'
    assert TH.clean_font_stack("B Nazanin, \"فونت\"") == '"B Nazanin", "فونت"'      # non-Latin names work
    assert TH.clean_font_stack("  ,, ") == ""
    for bad in ['a; } * { display:none', "Arial}", "x{y}", "url(x)", "a" * 70, "a,b,c,d,e,f,g,h,i", 'a"b', "x\\y", "a<b"]:
        raises(TH.clean_font_stack, bad, "f")


def test_normalize_fills_missing_and_clamps():
    t = TH.normalize_theme({"name": "  Tiny   theme ", "base": "light", "colors": {"accent": "#ff0000"},
                            "metrics": {"font_size": 99, "radius": -5, "bubble_width": 55.5}})
    assert t["name"] == "Tiny theme" and "id" not in t
    assert t["colors"]["accent"] == "#ff0000"
    assert t["colors"]["canvas"] == TH.PRESETS["paper"]["colors"]["canvas"]          # missing -> light base
    assert t["metrics"]["font_size"] == 22 and t["metrics"]["radius"] == 0 and t["metrics"]["bubble_width"] == 55.5
    assert t["fonts"] == TH.PRESETS["paper"]["fonts"] and t["pattern"] is True and t["css"] == ""
    assert set(t["colors"]) == set(TH.COLOR_KEYS) and set(t["metrics"]) == set(TH.METRICS)


def test_normalize_rejects_garbage():
    bad = [None, [], "x", {}, {"name": ""}, {"name": "x" * 41}, {"name": "a\x01b"}, {"name": "a", "base": "blue"},
           {"name": "a", "colors": "red"}, {"name": "a", "colors": {"canvas": "javascript:alert(1)"}},
           {"name": "a", "colors": {"canvas": 5}}, {"name": "a", "metrics": {"radius": "big"}},
           {"name": "a", "metrics": {"radius": True}}, {"name": "a", "metrics": {"radius": float("nan")}},
           {"name": "a", "pattern": "yes"}, {"name": "a", "css": 5}, {"name": "a", "css": "x" * (TH.MAX_CSS_CHARS + 1)},
           {"name": "a", "css": "a\x00b"}, {"name": "a", "id": "../x"}, {"name": "a", "id": 5},
           {"name": "a", "format": "something-else"}, {"name": "a", "fonts": {"sans": "a;b"}}]
    for raw in bad:
        raises(TH.normalize_theme, raw)
    # unknown keys are ignored (files from a newer version)
    t = TH.normalize_theme({"name": "a", "colors": {"future-token": "#fff"}, "metrics": {"future": 1}, "extra": 1, "version": 9})
    assert "future-token" not in t["colors"]


def test_generated_css_cannot_be_injected_through_validated_fields():
    t = TH.normalize_theme(full_theme(name="x */ body{display:none} /*"))
    css = TH.theme_css(t)
    header = css.splitlines()[0]
    assert header.count("*/") == 1 and header.endswith("*/")        # the name cannot close the comment early
    # every declaration in :root is a single, simple line
    root = css[css.index(":root {"):css.index("\n}")]
    for line in root.splitlines()[1:]:
        assert re.fullmatch(r"  --[a-z0-9-]+: [^;{}]+;|  color-scheme: (dark|light);", line), line


def test_derive_palette():
    for mode, args in (("dark", ("#0c1118", "#e4ebf3", "#3b7ddd")), ("light", ("#f2ede4", "#2b2622", "#a8472a")),
                       ("dark", ("#000000", "#ffffff", "#ffd60a")), ("light", ("#ffffff", "#000000", "#00aa55")),
                       ("dark", ("#333", "#ccc", "#f0f")), ("light", ("#fafafa", "#111", "#ff0"))):
        p = TH.derive_palette(*args, mode)
        assert set(p) == set(TH.COLOR_KEYS)
        for key, v in p.items():
            assert TH.clean_color(v, key) == v.lower()
        rgb = TH._rgb
        canvas, surface, raised = rgb(p["canvas"]), rgb(p["surface"]), rgb(p["surface-raised"])
        assert TH.contrast_ratio(rgb(p["ink"]), canvas) >= 4.5
        for k, need in (("muted", 4.5), ("muted-strong", 4.5), ("accent-text", 4.5)):
            for bg in (canvas, surface, raised):
                assert TH.contrast_ratio(rgb(p[k]), bg) >= need, (mode, args, k)
        assert TH.contrast_ratio(rgb(p["on-accent"]), rgb(p["accent"])) >= 3.0, (mode, args)
        for bub in ("bubble-in", "bubble-out"):                                   # message text stays readable on solid bubbles
            assert TH.contrast_ratio(rgb(p["ink"]), rgb(p[bub])) >= 4.5, (mode, args, bub)
        assert p["bubble-in-line"] == p["bubble-out-line"] == "transparent"
        assert TH.contrast_ratio(rgb(p["active-muted"]), rgb(p["accent-pale"])) >= 4.5, (mode, args)
        assert TH.contrast_ratio(rgb(p["badge-active-fg"]), rgb(p["badge-active-bg"])) >= 3.0, (mode, args)
    # deterministic, and the contrast knob changes only borders
    a = TH.derive_palette("#101010", "#eeeeee", "#3366cc", "dark")
    assert a == TH.derive_palette("#101010", "#eeeeee", "#3366cc", "dark")
    hi = TH.derive_palette("#101010", "#eeeeee", "#3366cc", "dark", contrast=2)
    assert hi["line"] != a["line"] and hi["surface"] == a["surface"]
    raises(TH.derive_palette, "red", "#fff", "#000")
    raises(TH.derive_palette, "#000", "#fff", "#000", "purple")


def test_presets_are_valid_and_distinct():
    assert list(TH.PRESETS) == ["ember", "paper", "midnight", "forest", "contrast"]
    seen = set()
    for pid, p in TH.PRESETS.items():
        n = TH.normalize_theme({k: v for k, v in p.items() if k not in ("id", "builtin")})
        assert n["colors"] == p["colors"], pid
        assert p["id"] == pid and p["builtin"] is True
        assert p["colors"]["canvas"] not in seen
        seen.add(p["colors"]["canvas"])
    assert TH.PRESETS["paper"]["base"] == "light"


def test_schema_is_complete():
    s = TH.schema()
    assert [c["key"] for c in s["colors"]] == TH.COLOR_KEYS
    assert {c["group"] for c in s["colors"]} <= {g["id"] for g in s["groups"]}
    assert {g["id"] for g in s["groups"]} <= {c["group"] for c in s["colors"]}      # no empty groups
    assert [m["key"] for m in s["metrics"]] == list(TH.METRICS)
    assert {f["key"] for f in s["fonts"]} == {"display", "sans", "mono"}
    assert all(c["default"] == TH.COLOR_DEFAULTS[c["key"]] for c in s["colors"])
    json.dumps(s)
    json.dumps(TH.list_themes(new_cfg()))


def test_crud_and_persistence():
    cfg = new_cfg()
    assert TH.active_theme(cfg)["id"] == "ember" and TH.css_for(cfg).count("--canvas: #111110;") == 1

    t = TH.save_theme(cfg, full_theme("Neon"))
    assert t["id"].startswith("c") and cfg["theme_active"] == t["id"] and cfg["theme_rev"] == 1
    # reload from disk
    cfg2 = C.Config(cfg.path)
    assert TH.active_theme(cfg2)["name"] == "Neon" and cfg2["theme_rev"] == 1

    # update in place keeps the id and the list length
    upd = TH.save_theme(cfg, {**full_theme("Neon 2"), "id": t["id"]})
    assert upd["id"] == t["id"] and len(TH.list_themes(cfg)["custom"]) == 1 and TH.active_theme(cfg)["name"] == "Neon 2"

    # an unknown or built-in id never overwrites anything: it becomes a new theme
    other = TH.save_theme(cfg, {**full_theme("Copy"), "id": "ember"}, activate=False)
    assert other["id"] != "ember" and TH.PRESETS["ember"]["name"] == "Ember"
    assert cfg["theme_active"] == t["id"] and len(TH.list_themes(cfg)["custom"]) == 2

    # switching
    assert TH.set_active(cfg, "paper")["id"] == "paper" and cfg["theme_active"] == "paper"
    assert "color-scheme: light;" in TH.css_for(cfg)
    assert "color-scheme: dark;" in TH.css_for(cfg, safe=True)                  # safe mode ignores the saved theme
    raises(TH.set_active, cfg, "nope")

    # deleting the active theme falls back to the default; built-ins cannot be deleted
    TH.set_active(cfg, other["id"])
    TH.delete_theme(cfg, other["id"])
    assert cfg["theme_active"] == "ember" and [x["id"] for x in TH.list_themes(cfg)["custom"]] == [t["id"]]
    raises(TH.delete_theme, cfg, "ember")
    raises(TH.delete_theme, cfg, "missing")

    # limit
    cfg3 = new_cfg()
    for i in range(TH.MAX_CUSTOM_THEMES):
        TH.save_theme(cfg3, full_theme(f"t{i}"), activate=False)
    assert "at most" in raises(TH.save_theme, cfg3, full_theme("one too many"))


def test_corrupted_config_does_not_break_the_app():
    cfg = new_cfg()
    good = TH.save_theme(cfg, full_theme("Good"))
    cfg.update({"custom_themes": [good, {"id": "bad", "name": "Bad", "colors": {"canvas": "url(x)"}}, "junk", None,
                                  {**good}, {**good, "id": "ember"}], "theme_active": "does-not-exist"})
    assert [x["id"] for x in TH.list_themes(cfg)["custom"]] == [good["id"]]     # invalid / duplicate / built-in id dropped
    assert TH.active_theme(cfg)["id"] == "ember"                                  # unknown active id -> default
    cfg.update({"custom_themes": "not a list"})
    assert TH.list_themes(cfg)["custom"] == []
    assert TH.css_for(cfg).startswith("/* TG Bridge theme: Ember */")


def test_export_import_roundtrip():
    cfg = new_cfg()
    t = TH.save_theme(cfg, full_theme("Shared", css=".x { color: red; }", pattern=False, fonts={"sans": "Vazirmatn, Tahoma"}))
    exported = TH.export_theme(t)
    assert exported["format"] == TH.FORMAT and "id" not in exported
    blob = json.loads(json.dumps(exported))
    imp = TH.import_theme(cfg, {**blob, "id": t["id"]})                            # an id inside the file is ignored
    assert imp["id"] != t["id"] and imp["name"] == "Shared" and imp["css"] == ".x { color: red; }"
    assert imp["colors"] == t["colors"] and imp["fonts"]["sans"] == '"Vazirmatn", "Tahoma"' and imp["pattern"] is False
    assert cfg["theme_active"] == t["id"]                                          # importing never activates
    raises(TH.import_theme, cfg, {"format": "other", "name": "x"})
    raises(TH.import_theme, cfg, ["not", "a", "theme"])


def test_preview_tolerates_an_empty_name_but_not_bad_values():
    css = TH.preview_css({**full_theme(""), "name": ""})
    assert ":root {" in css
    raises(TH.preview_css, full_theme("x", colors={"canvas": "nope"}))


def test_theme_name_cannot_break_out_of_the_css_comment():
    for name in ("**//body{display:none}/*", "**//", "a*/b", "x\\*/y"):
        css = TH.theme_css(TH.normalize_theme(full_theme(name)))
        head = css.splitlines()[0]
        assert head.startswith("/* TG Bridge theme: ") and head.endswith(" */") and head.count("*/") == 1, head
        assert "display:none" not in css.replace(head, "")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("Theme tests passed")
