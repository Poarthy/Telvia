"""Theme engine: built-in presets, user-made custom themes, validation and CSS generation.

Framework-free on purpose (no FastAPI import) so it can be unit-tested on its own; ``main.py`` only wires it to HTTP.

A theme is a plain dict:

    {"id", "name", "base": "dark" | "light",
     "colors":  {token: css color},          # every key of COLOR_TOKENS
     "fonts":   {"display", "sans", "mono"},  # font-family stacks
     "metrics": {key: number},                # every key of METRICS (sizes, radii, ...)
     "pattern": bool,                         # faint background grid on/off
     "css":     str}                          # free-form CSS appended last (advanced)

Everything except ``css`` is validated against strict allow-lists and re-emitted by this module, so the generated
stylesheet can never contain anything the user did not intend. ``css`` is the "complete freedom" escape hatch; it is
contained by the server's Content-Security-Policy (``default-src 'self'``): it cannot load remote resources.

The base values equal the ``:root`` block of ``static/style.css`` (a test keeps the two in sync).
"""
from __future__ import annotations

import re
import uuid

FORMAT = "tg-bridge-theme"
FORMAT_VERSION = 1
MAX_CUSTOM_THEMES = 30
MAX_CSS_CHARS = 20_000
MAX_NAME_CHARS = 40
DEFAULT_ID = "ember"


class ThemeError(ValueError):
    """A theme (or a request about themes) is invalid; the message is safe to show to the user."""


# ---------------------------------------------------------------------------------------
# Token tables
# ---------------------------------------------------------------------------------------
GROUPS = [
    ("surfaces", "Surfaces"),
    ("text", "Text"),
    ("lines", "Borders & hover"),
    ("accent", "Accent"),
    ("status", "Status"),
    ("messages", "Messages"),
    ("composer", "Composer"),
    ("misc", "Brand & misc"),
]

# (key, label, group, default for the built-in "Ember" theme, supports alpha in the editor)
COLOR_TOKENS: list[tuple[str, str, str, str, bool]] = [
    ("canvas", "Page background", "surfaces", "#111110", False),
    ("surface", "Sidebar & panels", "surfaces", "#191817", False),
    ("surface-raised", "Raised surfaces & inputs", "surfaces", "#211f1d", False),
    ("surface-soft", "Buttons & chips", "surfaces", "#292724", False),
    ("surface-deep", "Recessed areas", "surfaces", "#151413", False),
    ("hover-bg", "Button hover background", "surfaces", "#32302d", False),
    ("backdrop", "Dialog backdrop", "surfaces", "rgba(0, 0, 0, .72)", True),
    ("pattern", "Background grid lines", "surfaces", "rgba(255, 255, 255, .018)", True),

    ("ink", "Main text", "text", "#ece6df", False),
    ("muted", "Secondary text", "text", "#a39d96", False),
    ("muted-strong", "Labels", "text", "#c2bbb2", False),
    ("placeholder", "Placeholder text", "text", "#888782", False),
    ("accent-text", "Sender names & headings", "text", "#d99880", False),
    ("on-accent", "Text on accent & danger", "text", "#fffaf4", False),

    ("line", "Borders", "lines", "#3b3834", False),
    ("line-soft", "Subtle borders", "lines", "#2c2a27", False),
    ("line-strong", "Card & dialog borders", "lines", "#4b4640", False),
    ("hover-line", "Hover borders", "lines", "#59544e", False),

    ("accent", "Accent", "accent", "#874c39", False),
    ("accent-hot", "Accent (hover & focus)", "accent", "#a85d43", False),
    ("accent-deep", "Accent (deep)", "accent", "#633629", False),
    ("accent-pale", "Accent (tint)", "accent", "#39251f", False),
    ("active-line", "Selected chat border", "accent", "#6a4a3e", False),
    ("active-muted", "Selected chat secondary text", "accent", "#d2bbb0", False),
    ("badge-active-bg", "Unread badge in selected chat", "accent", "#d7ab98", False),
    ("badge-active-fg", "Unread badge text in selected chat", "accent", "#382019", False),

    ("live", "Connected", "status", "#8c9e78", False),
    ("warn", "Warning", "status", "#c09261", False),
    ("offline", "Disconnected", "status", "#ad604b", False),
    ("danger", "Danger", "status", "#c16056", False),
    ("danger-pale", "Danger (tint)", "status", "#412522", False),

    ("bubble-in", "Incoming message", "messages", "#262422", False),
    ("bubble-in-line", "Incoming message border", "messages", "transparent", False),
    ("bubble-out", "Outgoing message", "messages", "#2d2a27", False),
    ("bubble-out-line", "Outgoing message border", "messages", "transparent", False),
    ("quote-bg", "Reply quote", "messages", "#111110", False),
    ("media-bg", "File & media cards", "messages", "#171615", False),
    ("media-line", "File & media card border", "messages", "#46413c", False),

    ("composer-bg", "Message box", "composer", "#20201e", False),
    ("composer-line", "Message box border", "composer", "#343432", False),
    ("composer-line-focus", "Message box border (focused)", "composer", "#4d4843", False),
    ("send-fg", "Send button icon", "composer", "#f3e9e2", False),
    ("send-bg-off", "Send button (disabled)", "composer", "#613a2b", False),
    ("send-fg-off", "Send button icon (disabled)", "composer", "#8f827c", False),

    ("mark-bg", "Logo mark", "misc", "#262522", False),
    ("mark-line", "Logo mark border", "misc", "#646058", False),
    ("avatar-ring", "Avatar ring", "misc", "rgba(255, 255, 255, .16)", True),
]
COLOR_KEYS = [t[0] for t in COLOR_TOKENS]
COLOR_DEFAULTS = {t[0]: t[3] for t in COLOR_TOKENS}
_COLOR_LABELS = {t[0]: t[1] for t in COLOR_TOKENS}

# key -> (label, min, max, default, unit, step, css variable)
METRICS: dict[str, tuple[str, float, float, float, str, float, str]] = {
    "font_size": ("Base font size", 12, 22, 15, "px", 1, "font-size"),
    "line_height": ("Line height", 1.2, 2.0, 1.45, "", 0.05, "line-height"),
    "radius_sm": ("Corner radius: small (buttons, inputs)", 0, 24, 9, "px", 1, "radius-sm"),
    "radius": ("Corner radius: medium (message box)", 0, 32, 15, "px", 1, "radius"),
    "radius_lg": ("Corner radius: large (cards, dialogs)", 0, 40, 18, "px", 1, "radius-lg"),
    "radius_msg": ("Corner radius: message bubbles", 0, 28, 14, "px", 1, "radius-msg"),
    "radius_tail": ("Corner radius: bubble tail corner", 0, 20, 5, "px", 1, "radius-tail"),
    "avatar_radius": ("Avatar roundness", 0, 50, 50, "%", 1, "avatar-radius"),
    "bubble_width": ("Message bubble max width", 40, 100, 70, "%", 1, "bubble-width"),
    "avatar_sat": ("Avatar colour saturation", 0, 100, 45, "%", 1, "avatar-s"),
    "avatar_light": ("Avatar colour lightness", 10, 90, 45, "%", 1, "avatar-l"),
}
METRIC_DEFAULTS = {k: v[3] for k, v in METRICS.items()}

DEFAULT_FONTS = {
    "display": '"Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif',
    "sans": '"Avenir Next", "Segoe UI", "Noto Sans", sans-serif',
    "mono": 'ui-monospace, "SFMono-Regular", Consolas, monospace',
}
FONT_LABELS = {"display": "Headings font", "sans": "Interface font", "mono": "Monospace font"}

_GENERIC_FONTS = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-serif", "ui-sans-serif",
    "ui-monospace", "ui-rounded", "emoji", "math", "fangsong",
}

_NAMED_COLORS = set((
    "transparent aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown "
    "burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod "
    "darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon "
    "darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray "
    "dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green "
    "greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon "
    "lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon "
    "lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen magenta "
    "maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen "
    "mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive "
    "olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru "
    "pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen "
    "seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato "
    "turquoise violet wheat white whitesmoke yellow yellowgreen").split())

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# One level of function syntax only: no nested parentheses, so no url(), var(), calc(), ... can sneak in.
_FUNC_RE = re.compile(r"^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\(\s*[0-9a-zA-Z.,%\s/+\-]*\)$")
_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_FONT_NAME_RE = re.compile(r"^[\w][\w .\-]{0,59}$")


# ---------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------
def clean_color(value, label: str = "color") -> str:
    if not isinstance(value, str):
        raise ThemeError(f"{label}: a color is required.")
    v = " ".join(value.split())
    if not v or len(v) > 80:
        raise ThemeError(f"{label}: a color is required.")
    if _HEX_RE.match(v):
        return v.lower()
    if _FUNC_RE.match(v):
        return v.lower()
    if v.lower() in _NAMED_COLORS:
        return v.lower()
    raise ThemeError(f"{label}: “{v[:40]}” is not a valid color. Use #rrggbb, rgb(), rgba(), hsl(), hsla() or a color name.")


def clean_font_stack(value, label: str = "font") -> str:
    if not isinstance(value, str):
        raise ThemeError(f"{label}: expected a list of font names.")
    names = []
    for part in value.split(","):
        name = part.strip().strip("\"'").strip()
        if not name:
            continue
        if name.lower() in _GENERIC_FONTS:
            names.append(name.lower())
        elif _FONT_NAME_RE.match(name):
            names.append(f'"{name}"')
        else:
            raise ThemeError(f"{label}: “{name[:30]}” is not a valid font name (letters, digits, spaces, dots and dashes only).")
    if len(names) > 8:
        raise ThemeError(f"{label}: at most 8 fonts.")
    return ", ".join(names)


def font_names(stack: str) -> list[str]:
    """Names in a stack with quotes removed (used to compare stacks regardless of quoting)."""
    return [p.strip().strip("\"'").lower() for p in stack.split(",") if p.strip()]


def _num(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThemeError(f"{label}: a number is required.")
    if value != value or value in (float("inf"), float("-inf")):
        raise ThemeError(f"{label}: a number is required.")
    return float(value)


def _fmt(n: float) -> str:
    n = round(n, 3)
    return str(int(n)) if n == int(n) else str(n)


def normalize_theme(raw, *, fallback: dict | None = None) -> dict:
    """Validate ``raw`` (user input / imported file / stored data) and return a complete, normalized theme.

    Missing colors/fonts/metrics are taken from the built-in theme of the same ``base`` (or ``fallback``). Unknown keys are
    ignored (themes exported by a newer version still import). Numbers are clamped into their allowed range; everything
    else that is invalid raises ThemeError with a message meant for the user.
    """
    if not isinstance(raw, dict):
        raise ThemeError("This is not a theme.")
    if "format" in raw and raw["format"] != FORMAT:
        raise ThemeError("This file is not a Telvia theme.")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ThemeError("Give the theme a name.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_CHARS:
        raise ThemeError(f"The theme name can be at most {MAX_NAME_CHARS} characters.")
    if any(ord(c) < 32 for c in name):
        raise ThemeError("The theme name contains invalid characters.")

    base = raw.get("base", "dark")
    if base not in ("dark", "light"):
        raise ThemeError("Base must be “dark” or “light”.")
    ref = fallback or PRESETS["ember" if base == "dark" else "paper"]

    theme_id = raw.get("id")
    if theme_id is not None and not (isinstance(theme_id, str) and _ID_RE.match(theme_id)):
        raise ThemeError("Invalid theme id.")

    colors_in = raw.get("colors", {})
    fonts_in = raw.get("fonts", {})
    metrics_in = raw.get("metrics", {})
    if not all(isinstance(x, dict) for x in (colors_in, fonts_in, metrics_in)):
        raise ThemeError("The theme data is malformed.")

    colors = {}
    for key in COLOR_KEYS:
        colors[key] = clean_color(colors_in[key], _COLOR_LABELS[key]) if key in colors_in else ref["colors"][key]

    fonts = {}
    for key in DEFAULT_FONTS:
        stack = clean_font_stack(fonts_in[key], FONT_LABELS[key]) if key in fonts_in else ""
        fonts[key] = stack or ref["fonts"][key]

    metrics = {}
    for key, (label, lo, hi, _default, _unit, _step, _var) in METRICS.items():
        if key in metrics_in:
            metrics[key] = min(hi, max(lo, round(_num(metrics_in[key], label), 3)))
        else:
            metrics[key] = ref["metrics"][key]

    pattern = raw.get("pattern", ref["pattern"])
    if not isinstance(pattern, bool):
        raise ThemeError("Pattern must be on or off.")

    css = raw.get("css", "")
    if not isinstance(css, str):
        raise ThemeError("Custom CSS must be text.")
    if len(css) > MAX_CSS_CHARS:
        raise ThemeError(f"Custom CSS can be at most {MAX_CSS_CHARS:,} characters.")
    if "\x00" in css:
        raise ThemeError("Custom CSS contains invalid characters.")

    out = {"name": name, "base": base, "colors": colors, "fonts": fonts, "metrics": metrics, "pattern": pattern, "css": css}
    if theme_id:
        out = {"id": theme_id, **out}
    return out


# ---------------------------------------------------------------------------------------
# CSS generation
# ---------------------------------------------------------------------------------------
def theme_css(theme: dict) -> str:
    """The stylesheet served at /theme.css: one ``:root`` block of custom properties + the user's custom CSS."""
    # Drop every "*" and "/" (a single replace('*/', '') can re-create "*/" out of "**//") so the name can never close the comment.
    safe_name = re.sub(r"[*/\\]", "", theme["name"])
    lines = [f"/* TG Bridge theme: {safe_name} */", ":root {", f"  color-scheme: {theme['base']};"]
    for key in COLOR_KEYS:
        lines.append(f"  --{key}: {theme['colors'][key]};")
    for key in DEFAULT_FONTS:
        lines.append(f"  --{key}: {theme['fonts'][key]};")
    for key, (_label, _lo, _hi, _default, unit, _step, var) in METRICS.items():
        lines.append(f"  --{var}: {_fmt(theme['metrics'][key])}{unit};")
    if not theme["pattern"]:
        lines += ["  --pattern-body: none;", "  --pattern-chat: none;"]
    lines.append("}")
    if theme["css"].strip():
        lines += ["", "/* Custom CSS */", theme["css"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------
# Palette generator: three seed colors -> every color token
# ---------------------------------------------------------------------------------------
def _rgb(value: str) -> tuple[float, float, float]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", v):
        raise ThemeError("Pick colors as #rrggbb.")
    return tuple(float(int(v[i:i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(c) -> str:
    return "#" + "".join(f"{max(0, min(255, round(x))):02x}" for x in c)


def _mix(a, b, t: float):
    return tuple(x * (1 - t) + y * t for x, y in zip(a, b))


def _lum(c) -> float:
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ensure(fg, bgs, target: float, toward):
    """Move ``fg`` toward ``toward`` until it reaches ``target`` contrast against every color in ``bgs``."""
    for i in range(21):
        c = _mix(fg, toward, i / 20)
        if min(contrast_ratio(c, bg) for bg in bgs) >= target:
            return c
    return toward


def _rgba(c, a: float) -> str:
    r, g, b = (max(0, min(255, round(x))) for x in c)
    return f"rgba({r}, {g}, {b}, {_fmt(a)})"


def derive_palette(canvas: str, ink: str, accent: str, mode: str = "dark", contrast: float = 1.0) -> dict[str, str]:
    """Build a complete, coherent set of color tokens from a background, a text color and an accent color.

    ``contrast`` (0.5–2) scales how visible borders are. Text colors are nudged until they are readable on the surfaces
    they are drawn on (WCAG AA for body text).
    """
    if mode not in ("dark", "light"):
        raise ThemeError("Base must be “dark” or “light”.")
    k = min(2.0, max(0.5, float(contrast)))
    cv, iv, av = _rgb(canvas), _rgb(ink), _rgb(accent)
    white, black = (255.0, 255.0, 255.0), (0.0, 0.0, 0.0)
    dark = mode == "dark"

    def edge(t):  # neutral border color: canvas pulled toward the text color
        return _mix(cv, iv, min(0.9, t * k))

    if dark:
        surface, raised = _mix(cv, iv, .035), _mix(cv, iv, .065)
        soft, deep, hover_bg = _mix(cv, iv, .10), _mix(cv, iv, .015), _mix(cv, iv, .14)
        mark_bg, mark_line = _mix(cv, iv, .065), edge(.36)
        composer_bg = _mix(cv, iv, .07)
        accent_hot = _mix(av, white, .16)
        accent_pale = _mix(cv, av, .32)
        # Flat, solid bubbles: the outgoing one is the incoming one lifted a touch toward the text color (like Ember),
        # so the two read as one family and alignment tells them apart. ``contrast`` widens the gap.
        bubble_out = _ensure(_mix(raised, iv, min(.12, .035 * k)), [iv], 4.5, cv)
        status = {"live": (140, 158, 120), "warn": (192, 146, 97), "offline": (173, 96, 75), "danger": (193, 96, 86)}
        danger_pale = _mix(cv, status["danger"], .22)
        backdrop, pattern, ring = "rgba(0, 0, 0, .72)", "rgba(255, 255, 255, .018)", "rgba(255, 255, 255, .16)"
    else:
        surface, raised = _mix(cv, white, .55), _mix(cv, white, .85)
        soft, deep, hover_bg = _mix(cv, iv, .06), _mix(cv, iv, .035), _mix(cv, iv, .10)
        mark_bg, mark_line = _mix(cv, iv, .06), edge(.30)
        composer_bg = raised
        accent_hot = _mix(av, black, .14)
        accent_pale = _mix(cv, av, .18)
        # Same idea on light themes: the incoming bubble is the lightest surface, so the outgoing one gets a faint accent
        # tint instead (it cannot go lighter, and darkening it would make it merge with the page background).
        bubble_out = _ensure(_mix(raised, av, min(.12, .04 * k)), [iv], 4.5, cv)
        status = {"live": (79, 107, 58), "warn": (138, 90, 31), "offline": (162, 74, 53), "danger": (179, 56, 45)}
        danger_pale = _mix(cv, status["danger"], .14)
        backdrop, pattern, ring = "rgba(30, 25, 20, .45)", "rgba(0, 0, 0, .035)", "rgba(0, 0, 0, .14)"

    pole = iv  # the direction in which text becomes more readable
    surfaces = [cv, surface, raised]
    muted = _ensure(_mix(cv, iv, .62), surfaces, 4.5, pole)
    muted_strong = _ensure(_mix(cv, iv, .78), surfaces, 7.0 if dark else 5.5, pole)
    placeholder = _ensure(_mix(composer_bg, iv, .5), [composer_bg], 3.0, pole)
    accent_hot = _ensure(accent_hot, [cv], 3.0, pole)
    accent_text = _ensure(_mix(accent_hot, iv, .35), [cv, surface, raised], 4.5, pole)
    light_txt, dark_txt = (255.0, 250.0, 244.0), (20.0, 17.0, 15.0)

    def readable_on(bg):
        return light_txt if contrast_ratio(light_txt, bg) >= contrast_ratio(dark_txt, bg) else dark_txt

    on_accent = readable_on(av)
    active_line = _mix(accent_pale, av, .5)
    active_muted = _ensure(_mix(iv, av, .25), [accent_pale], 4.5, pole)
    badge_active = _mix(accent_hot, iv, .45)
    send_off = _mix(av, cv, .35)
    line, strong = edge(.18), edge(.24)

    colors = {
        "canvas": _hex(cv), "surface": _hex(surface), "surface-raised": _hex(raised), "surface-soft": _hex(soft),
        "surface-deep": _hex(deep), "hover-bg": _hex(hover_bg), "backdrop": backdrop, "pattern": pattern,
        "ink": _hex(iv), "muted": _hex(muted), "muted-strong": _hex(muted_strong), "placeholder": _hex(placeholder),
        "accent-text": _hex(accent_text), "on-accent": _hex(on_accent),
        "line": _hex(line), "line-soft": _hex(edge(.10)), "line-strong": _hex(strong), "hover-line": _hex(edge(.30)),
        "accent": _hex(av), "accent-hot": _hex(accent_hot), "accent-deep": _hex(_mix(av, cv, .35)),
        "accent-pale": _hex(accent_pale), "active-line": _hex(active_line), "active-muted": _hex(active_muted),
        "badge-active-bg": _hex(badge_active), "badge-active-fg": _hex(readable_on(badge_active)),
        "live": _hex(status["live"]), "warn": _hex(status["warn"]), "offline": _hex(status["offline"]),
        "danger": _hex(status["danger"]), "danger-pale": _hex(danger_pale),
        "bubble-in": _hex(raised), "bubble-in-line": "transparent", "bubble-out": _hex(bubble_out),
        "bubble-out-line": "transparent", "quote-bg": _hex(cv), "media-bg": _hex(deep), "media-line": _hex(strong),
        "composer-bg": _hex(composer_bg), "composer-line": _hex(line), "composer-line-focus": _hex(strong),
        "send-fg": _hex(on_accent), "send-bg-off": _hex(send_off), "send-fg-off": _hex(_mix(send_off, iv, .45)),
        "mark-bg": _hex(mark_bg), "mark-line": _hex(mark_line), "avatar-ring": ring,
    }
    assert set(colors) == set(COLOR_KEYS)
    return colors


# ---------------------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------------------
def _preset(theme_id: str, name: str, base: str, colors: dict, **extra) -> dict:
    return {"id": theme_id, "name": name, "base": base, "colors": colors,
            "fonts": {k: clean_font_stack(v) for k, v in DEFAULT_FONTS.items()},
            "metrics": dict(METRIC_DEFAULTS), "pattern": True, "css": "", "builtin": True, **extra}


PRESETS: dict[str, dict] = {}
PRESETS["ember"] = _preset("ember", "Ember", "dark", dict(COLOR_DEFAULTS))
PRESETS["paper"] = _preset("paper", "Paper", "light", derive_palette("#f2ede4", "#2b2622", "#a8472a", "light"))
PRESETS["midnight"] = _preset("midnight", "Midnight", "dark", derive_palette("#0c1118", "#e4ebf3", "#3b7ddd", "dark"))
PRESETS["forest"] = _preset("forest", "Forest", "dark", derive_palette("#0e1411", "#e2eae3", "#3f8d5c", "dark"))
PRESETS["contrast"] = _preset("contrast", "High contrast", "dark",
                              derive_palette("#000000", "#ffffff", "#ffd60a", "dark", contrast=2.0))


# ---------------------------------------------------------------------------------------
# Persistence (config.json) and operations used by the HTTP layer
# ---------------------------------------------------------------------------------------
def _customs(cfg) -> list[dict]:
    """Stored custom themes, re-validated (a hand-edited or corrupted config.json must not break the app)."""
    raw = cfg.get("custom_themes") or []
    out, seen = [], set(PRESETS)
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            theme = normalize_theme(item)
        except ThemeError:
            continue
        if "id" not in theme or theme["id"] in seen:
            continue
        seen.add(theme["id"])
        out.append(theme)
    return out


def get_theme(cfg, theme_id: str) -> dict | None:
    if theme_id in PRESETS:
        return PRESETS[theme_id]
    return next((t for t in _customs(cfg) if t["id"] == theme_id), None)


def active_theme(cfg) -> dict:
    return get_theme(cfg, cfg.get("theme_active") or DEFAULT_ID) or PRESETS[DEFAULT_ID]


def _bump(cfg, values: dict) -> None:
    cfg.update({**values, "theme_rev": int(cfg.get("theme_rev") or 0) + 1})


def list_themes(cfg) -> dict:
    return {"active": active_theme(cfg)["id"], "presets": list(PRESETS.values()), "custom": _customs(cfg),
            "rev": int(cfg.get("theme_rev") or 0)}


def css_for(cfg, safe: bool = False) -> str:
    return theme_css(PRESETS[DEFAULT_ID] if safe else active_theme(cfg))


def preview_css(raw) -> str:
    """CSS for an unsaved theme (live preview in the editor). An empty name is fine while the user is still typing."""
    if isinstance(raw, dict) and not str(raw.get("name") or "").strip():
        raw = {**raw, "name": "Preview"}
    return theme_css(normalize_theme(raw))


def save_theme(cfg, raw, *, activate: bool = True) -> dict:
    """Create a custom theme (no ``id``, or an unknown / built-in one) or update an existing custom theme."""
    theme = normalize_theme(raw)
    with cfg.lock:
        items = _customs(cfg)
        existing = {t["id"] for t in items}
        tid = theme.get("id")
        if tid in existing:
            items = [{**theme, "id": tid} if t["id"] == tid else t for t in items]
        else:
            if len(items) >= MAX_CUSTOM_THEMES:
                raise ThemeError(f"You can keep at most {MAX_CUSTOM_THEMES} custom themes. Delete one first.")
            tid = "c" + uuid.uuid4().hex[:10]
            theme = {**theme, "id": tid}
            items.append(theme)
        values = {"custom_themes": items}
        if activate:
            values["theme_active"] = tid
        _bump(cfg, values)
        return next(t for t in items if t["id"] == tid)


def import_theme(cfg, raw) -> dict:
    """Import a theme file: always becomes a new custom theme (any id in the file is ignored)."""
    if isinstance(raw, dict):
        raw = {k: v for k, v in raw.items() if k != "id"}
    return save_theme(cfg, raw, activate=False)


def set_active(cfg, theme_id: str) -> dict:
    with cfg.lock:
        theme = get_theme(cfg, theme_id)
        if theme is None:
            raise ThemeError("That theme no longer exists.")
        _bump(cfg, {"theme_active": theme["id"]})
        return theme


def delete_theme(cfg, theme_id: str) -> None:
    with cfg.lock:
        if theme_id in PRESETS:
            raise ThemeError("Built-in themes cannot be deleted.")
        items = _customs(cfg)
        if theme_id not in {t["id"] for t in items}:
            raise ThemeError("That theme no longer exists.")
        values = {"custom_themes": [t for t in items if t["id"] != theme_id]}
        if cfg.get("theme_active") == theme_id:
            values["theme_active"] = DEFAULT_ID
        _bump(cfg, values)


def export_theme(theme: dict) -> dict:
    return {"format": FORMAT, "version": FORMAT_VERSION, "name": theme["name"], "base": theme["base"],
            "colors": theme["colors"], "fonts": theme["fonts"], "metrics": theme["metrics"],
            "pattern": theme["pattern"], "css": theme["css"]}


def schema() -> dict:
    """Everything the editor UI needs to build itself: no token list is duplicated in JavaScript."""
    return {
        "groups": [{"id": g, "label": label} for g, label in GROUPS],
        "colors": [{"key": k, "label": label, "group": g, "default": d, "alpha": a} for k, label, g, d, a in COLOR_TOKENS],
        "fonts": [{"key": k, "label": FONT_LABELS[k], "default": clean_font_stack(v)} for k, v in DEFAULT_FONTS.items()],
        "metrics": [{"key": k, "label": v[0], "min": v[1], "max": v[2], "default": v[3], "unit": v[4], "step": v[5]}
                    for k, v in METRICS.items()],
        "limits": {"css": MAX_CSS_CHARS, "name": MAX_NAME_CHARS, "themes": MAX_CUSTOM_THEMES},
    }
