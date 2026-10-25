"use strict";
/* Theme settings: pick a theme, create your own, edit every color / font / size, import and export.
   Loaded after app.js and uses its helpers ($, $$, el, api, toast, fail, t, STR, S, noteThemeRev, openSettings).
   The server owns the token list and all validation (local_server/themes.py): this file builds the editor from
   GET /api/theme/schema and asks POST /api/themes/preview for the CSS, so the preview is exactly what will be saved. */

Object.assign(STR, {
  theme_title: "Theme", theme_hint: "Pick a theme or make your own. Theme changes apply immediately and are stored on this device.",
  theme_safe: "Safe mode: your saved theme is not applied on this page. Fix or delete the theme here, then open the app without “?safe=1”.",
  theme_builtin: "Built-in", theme_custom: "Custom", theme_dark: "Dark", theme_light: "Light",
  theme_customize: "Customize a copy…", theme_edit: "Edit theme…", theme_new: "New theme…", theme_import: "Import…", theme_export: "Export",
  theme_delete: "Delete theme", theme_confirm_delete: "Delete this theme? This cannot be undone.",
  theme_import_css: "This theme contains custom CSS, which can change how every part of the app looks. Only import themes you trust. Continue?",
  theme_imported: "Theme imported", theme_import_bad: "That file is not a valid theme.",
  ted_back: "← Settings", ted_name: "Theme name", ted_base: "Base (light or dark)", ted_base_note: "Sets the browser’s own controls (scrollbars, form fields) and is used when generating a palette.",
  ted_from: "Copy settings from", ted_from_btn: "Copy", ted_from_confirm: "Replace the colors, fonts and sizes with those of the selected theme?",
  ted_key: "Key colors", ted_key_note: "Choose three colors and generate every other color from them, then fine-tune below.",
  ted_generate: "Generate palette", ted_border_contrast: "Border contrast", ted_colors: "Colors", ted_fonts: "Fonts", ted_fonts_note: "Only fonts installed on this device can be used. Separate fallbacks with commas.",
  ted_shape: "Size & shape", ted_background: "Background", ted_grid: "Show the faint background grid",
  ted_css: "Custom CSS", ted_css_note: "Advanced. Applied after everything else, so it can restyle anything. Remote resources are blocked.",
  ted_save: "Save and apply", ted_cancel: "Cancel", ted_reset_value: "Undo change", ted_opacity: "opacity",
  ted_discard: "Discard your unsaved theme changes?", ted_invalid: "Invalid value",
  ted_copy_suffix: "copy", ted_new_name: "My theme",
});

const ThemeUI = { schema: null, ed: null, seq: 0, timer: 0 };
const KEY_COLORS = ["canvas", "ink", "accent"];
const FONT_SUGGESTIONS = [
  'system-ui, sans-serif', '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif', 'Tahoma, "Noto Sans Arabic", sans-serif',
  'Georgia, "Times New Roman", serif', '"Iowan Old Style", "Palatino Linotype", Georgia, serif',
  'ui-monospace, Menlo, Consolas, monospace', '"Courier New", monospace',
];
const clone = (x) => JSON.parse(JSON.stringify(x));

/* ---------------------------------------------------------------- colors */
const _cv = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
/* Parses any CSS color the browser understands. Returns null for an invalid value, otherwise {rgb: [r,g,b] | null, a}. */
function parseColor(v) {
  if (!_cv || typeof v !== "string" || !v.trim()) return null;
  _cv.fillStyle = "#000000"; _cv.fillStyle = v; const a = _cv.fillStyle;
  _cv.fillStyle = "#ffffff"; _cv.fillStyle = v; const b = _cv.fillStyle;
  if (a !== b) return null;                       // the setter ignored it both times: not a color
  let m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(a);
  if (m) return { rgb: [1, 2, 3].map((i) => parseInt(m[i], 16)), a: 1 };
  m = /^rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)$/.exec(a);
  if (m) return { rgb: [+m[1], +m[2], +m[3]], a: m[4] === undefined ? 1 : +m[4] };
  return { rgb: null, a: 1 };                     // valid, but in a color space the picker cannot show
}
const hex2 = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
const toHex = (rgb) => "#" + rgb.map(hex2).join("");
const fromRgba = (rgb, a) => (a >= 1 ? toHex(rgb) : `rgba(${rgb.map(Math.round).join(", ")}, ${+a.toFixed(3)})`);

/* ---------------------------------------------------------------- stylesheet preview */
function setPreviewCss(css) {
  let s = $("#theme-preview");
  if (!s) { s = document.createElement("style"); s.id = "theme-preview"; document.head.append(s); }
  s.textContent = css;
}
function removePreview() { const s = $("#theme-preview"); if (s) s.remove(); }

/* ---------------------------------------------------------------- settings section: choose / manage */
async function themeData() {
  if (!ThemeUI.schema) ThemeUI.schema = await api("/theme/schema");
  return api("/themes");
}
function buildThemeSection() {
  const box = el("div", { class: "theme-ui theme-section" });
  renderThemeSection(box).catch(fail);
  return box;
}
function themeCard(th, active, box) {
  const c = th.colors, sw = el("span", { class: "tc-swatch", "aria-hidden": "true" });
  sw.style.background = c.canvas;
  for (const [k, grow] of [["surface", 2], ["bubble-out", 1], ["accent", 1], ["ink", 1]]) {
    const i = el("i"); i.style.background = c[k]; i.style.flexGrow = grow; sw.append(i);
  }
  const tag = `${th.builtin ? t("theme_builtin") : t("theme_custom")} · ${th.base === "light" ? t("theme_light") : t("theme_dark")}`;
  return el("button", { type: "button", class: "theme-card", "aria-pressed": active ? "true" : "false", onclick: () => applyTheme(th.id, box) },
    sw, el("span", { class: "tc-name", dir: "auto", text: th.name }), el("span", { class: "tc-tag", text: tag }));
}
async function applyTheme(id, box) {
  try {
    const r = await api("/themes/active", "POST", { id });
    noteThemeRev(r.rev); if (S.st) S.st.settings.theme_active = id;
    await renderThemeSection(box);
  } catch (e) { fail(e); }
}
async function renderThemeSection(box) {
  const data = await themeData(), all = [...data.presets, ...data.custom];
  const active = all.find((x) => x.id === data.active) || data.presets[0];
  const file = el("input", { type: "file", accept: ".json,application/json", class: "hidden", "aria-hidden": "true", tabindex: "-1" });
  file.addEventListener("change", () => { const f = file.files[0]; file.value = ""; if (f) importThemeFile(f, box); });
  const actions = el("div", { class: "theme-actions" },
    el("button", { type: "button", class: "primary", text: active.builtin ? t("theme_customize") : t("theme_edit"),
      onclick: () => openThemeEditor(active, active.builtin ? null : active.id) }),
    el("button", { type: "button", text: t("theme_new"), onclick: () => openThemeEditor(active, null, true) }),
    el("button", { type: "button", text: t("theme_import"), onclick: () => file.click() }),
    el("button", { type: "button", text: t("theme_export"), onclick: () => downloadTheme(active.id) }),
    active.builtin ? null : el("button", { type: "button", class: "danger", text: t("theme_delete"), onclick: () => deleteTheme(active.id, box) }),
    file);
  // (replaceChildren would turn a null into the text "null", so only real nodes go in)
  box.replaceChildren(...[
    el("h3", { text: t("theme_title") }),
    SAFE_MODE ? el("p", { class: "theme-safe", role: "status", text: t("theme_safe") }) : null,
    el("p", { class: "muted small", text: t("theme_hint") }),
    el("div", { class: "theme-grid", role: "group", "aria-label": t("theme_title") }, ...all.map((x) => themeCard(x, x.id === active.id, box))),
    actions].filter(Boolean));
}
function downloadTheme(id) {
  const a = el("a", { href: `/api/themes/${encodeURIComponent(id)}/export`, download: "" });
  document.body.append(a); a.click(); a.remove();
}
async function deleteTheme(id, box) {
  if (!confirm(t("theme_confirm_delete"))) return;
  try { const r = await api(`/themes/${encodeURIComponent(id)}`, "DELETE"); noteThemeRev(r.rev); if (S.st) S.st.settings.theme_active = "ember"; if (box) await renderThemeSection(box); } catch (e) { fail(e); }
}
async function importThemeFile(file, box) {
  let parsed;
  try { parsed = JSON.parse(await file.text()); } catch (e) { toast(t("theme_import_bad"), "err"); return; }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || typeof parsed.name !== "string") { toast(t("theme_import_bad"), "err"); return; }
  if (typeof parsed.css === "string" && parsed.css.trim() && !confirm(t("theme_import_css"))) return;
  try {
    const r = await api("/themes/import", "POST", { theme: parsed });
    toast(`${t("theme_imported")}: ${r.theme.name}`, "ok"); await renderThemeSection(box);
  } catch (e) { fail(e); }
}

/* ---------------------------------------------------------------- editor */
const editorDirty = () => !!ThemeUI.ed && JSON.stringify(ThemeUI.ed.draft) !== ThemeUI.ed.snapshot;
/* app.js calls this before the settings dialog closes or is rebuilt; false = stay. */
function themeBeforeClose() {
  if (!ThemeUI.ed) return true;
  if (editorDirty() && !confirm(t("ted_discard"))) return false;
  leaveEditor(); return true;
}
/* keepPreview: the stylesheet is about to be reloaded (saved / deleted), and that reload removes the preview once the new
   CSS is in place; removing it now would flash the old theme. */
function leaveEditor(keepPreview = false) {
  ThemeUI.ed = null; ThemeUI.seq++; clearTimeout(ThemeUI.timer);
  if (!keepPreview) removePreview();
  $("#modal").classList.remove("docked");
}
/* source: the theme to start from. editId: id of the custom theme being edited (null = save as a new theme). */
function openThemeEditor(source, editId, blank = false) {
  const draft = { name: source.name, base: source.base, colors: clone(source.colors), fonts: clone(source.fonts),
    metrics: clone(source.metrics), pattern: source.pattern, css: source.css || "" };
  if (!editId) draft.name = blank ? t("ted_new_name") : `${source.name} ${t("ted_copy_suffix")}`.slice(0, ThemeUI.schema.limits.name);
  ThemeUI.ed = { id: editId, draft, original: clone(draft), snapshot: JSON.stringify(draft), invalid: new Set(), serverErr: "", rows: new Map(), refs: {} };
  const body = $("#settings-body");
  $("#modal").classList.add("docked");
  body.replaceChildren(buildEditor());
  $("#modal .modal-card").scrollTop = 0;
  runPreview();
}
function schedulePreview() { clearTimeout(ThemeUI.timer); ThemeUI.timer = setTimeout(runPreview, 90); }
const draftPayload = () => { const d = ThemeUI.ed.draft; return { name: d.name, base: d.base, colors: d.colors, fonts: d.fonts, metrics: d.metrics, pattern: d.pattern, css: d.css }; };
async function runPreview() {
  const ed = ThemeUI.ed; if (!ed) return;
  const seq = ++ThemeUI.seq;
  try {
    const { css } = await api("/themes/preview", "POST", draftPayload());
    if (seq !== ThemeUI.seq || ThemeUI.ed !== ed) return;
    setPreviewCss(css); ed.serverErr = "";
  } catch (e) { if (seq !== ThemeUI.seq || ThemeUI.ed !== ed) return; ed.serverErr = e.message; }
  refreshEditorState();
}
function refreshEditorState() {
  const ed = ThemeUI.ed; if (!ed) return;
  const err = ed.serverErr || (ed.invalid.size ? t("ted_invalid") : "");
  ed.refs.error.textContent = err; ed.refs.error.classList.toggle("hidden", !err);
  ed.refs.save.disabled = !!err || !ed.draft.name.trim();
}
function touch() { schedulePreview(); refreshEditorState(); }

async function saveEditor() {
  const ed = ThemeUI.ed; if (!ed || ed.refs.save.disabled) return;
  try {
    const r = await api("/themes", "POST", { ...draftPayload(), id: ed.id, activate: true });
    noteThemeRev(r.rev); if (S.st) S.st.settings.theme_active = r.theme.id;
    toast(t("saved"), "ok"); ed.snapshot = JSON.stringify(ed.draft);
    leaveEditor(true); openSettings();
  } catch (e) { fail(e); }
}
async function deleteFromEditor() {
  const ed = ThemeUI.ed; if (!ed || !ed.id || !confirm(t("theme_confirm_delete"))) return;
  try {
    const r = await api(`/themes/${encodeURIComponent(ed.id)}`, "DELETE");
    noteThemeRev(r.rev); if (S.st) S.st.settings.theme_active = "ember";
    leaveEditor(true); openSettings();
  } catch (e) { fail(e); }
}

/* One color token: color picker + free text (any CSS color) + optional opacity slider + undo. */
function colorRow(tok) {
  const ed = ThemeUI.ed, id = "tk-" + tok.key;
  const pick = el("input", { type: "color", class: "tk-pick", "aria-label": tok.label });
  const text = el("input", { type: "text", id, class: "tk-text", dir: "ltr", spellcheck: "false", autocomplete: "off", maxlength: "80" });
  const alpha = tok.alpha ? el("input", { type: "range", class: "tk-alpha", min: "0", max: "100", step: "1", "aria-label": `${tok.label} ${t("ted_opacity")}` }) : null;
  const undo = el("button", { type: "button", class: "icon tk-reset", text: "↺", title: t("ted_reset_value"), "aria-label": `${tok.label}: ${t("ted_reset_value")}` });
  const row = el("div", { class: "tk" + (tok.alpha ? " wide" : "") },
    el("label", { class: "tk-label", for: id, text: tok.label }),
    el("div", { class: "tk-ctl" }, pick, text, alpha, undo));
  const show = (v) => {            // value -> controls (never writes back)
    text.value = v;
    const p = parseColor(v);
    text.setAttribute("aria-invalid", p ? "false" : "true");
    if (p && p.rgb) pick.value = toHex(p.rgb);
    if (alpha && p) alpha.value = Math.round(p.a * 100);
    return p;
  };
  const commit = (v) => {          // controls -> draft
    const p = show(v);
    if (p) { ed.invalid.delete(tok.key); ed.draft.colors[tok.key] = v.trim(); } else ed.invalid.add(tok.key);
    touch();
  };
  text.addEventListener("input", () => commit(text.value));
  pick.addEventListener("input", () => { const p = parseColor(text.value); commit(fromRgba(parseColorHex(pick.value), p ? p.a : 1)); });
  if (alpha) alpha.addEventListener("input", () => { const p = parseColor(text.value), rgb = (p && p.rgb) || parseColorHex(pick.value); commit(fromRgba(rgb, alpha.value / 100)); });
  undo.addEventListener("click", () => commit(ed.original.colors[tok.key]));
  ed.rows.set(tok.key, () => { ed.invalid.delete(tok.key); show(ed.draft.colors[tok.key]); });
  show(ed.draft.colors[tok.key]);
  return row;
}
const parseColorHex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

/* A number: slider + number box kept in sync. */
function metricRow(m) {
  const ed = ThemeUI.ed, id = "tm-" + m.key;
  const range = el("input", { type: "range", min: m.min, max: m.max, step: m.step, "aria-label": m.label });
  const num = el("input", { type: "number", id, min: m.min, max: m.max, step: m.step, class: "tm-num" });
  const set = (v, fromUser) => {
    if (v === "" || !isFinite(Number(v))) return;
    const n = Math.min(m.max, Math.max(m.min, Number(v)));
    ed.draft.metrics[m.key] = n; range.value = n; if (!fromUser || document.activeElement !== num) num.value = n;
    touch();
  };
  range.value = num.value = ed.draft.metrics[m.key];
  range.addEventListener("input", () => set(range.value, true));
  num.addEventListener("input", () => set(num.value, true));
  num.addEventListener("change", () => { num.value = ed.draft.metrics[m.key]; });
  return el("div", { class: "tm" }, el("label", { class: "tk-label", for: id, text: m.unit ? `${m.label} (${m.unit})` : m.label }), el("div", { class: "tk-ctl" }, range, num));
}
function fontRow(f) {
  const ed = ThemeUI.ed, id = "tf-" + f.key;
  const input = el("input", { type: "text", id, dir: "ltr", spellcheck: "false", autocomplete: "off", list: "font-suggestions", placeholder: f.default, maxlength: "300", value: ed.draft.fonts[f.key] });
  input.addEventListener("input", () => { ed.draft.fonts[f.key] = input.value; touch(); });
  ed.rows.set("font:" + f.key, () => { input.value = ed.draft.fonts[f.key]; });
  return el("div", { class: "tf" }, el("label", { class: "tk-label", for: id, text: f.label }), input);
}

function buildSample() {
  const chat = (title, last, badge, active) => el("div", { class: "chat-item" + (active ? " active" : "") },
    avatarNode({ id: title.length * 7, title }),
    el("div", { class: "ci-main" },
      el("div", { class: "ci-top" }, el("span", { class: "ci-title", dir: "auto", text: title }), el("span", { class: "muted small", text: "09:41" })),
      el("div", { class: "ci-bot" }, el("span", { class: "ci-last muted", text: last }), badge ? el("span", { class: "badge", text: String(badge) }) : null)));
  return el("div", { class: "theme-sample", inert: true, "aria-hidden": "true" },
    chat("Alex", "See you soon", 3, true), chat("Weekend plans", "Photo", 0, false),
    el("div", { class: "ts-chat" },
      el("div", { class: "msg in" }, el("div", { class: "sender", dir: "auto", text: "Alex" }), el("div", { class: "text", dir: "auto", text: "Is this readable?" }), el("div", { class: "meta" }, el("span", { text: "09:40" }))),
      el("div", { class: "msg out" }, el("div", { class: "reply", dir: "auto", text: "Is this readable?" }), el("div", { class: "text", dir: "auto", text: "Looks great." }), el("div", { class: "meta" }, el("span", { text: "09:41" }))),
      el("div", { class: "file" }, el("div", { class: "ico", text: "📄" }), el("div", { class: "info" }, el("div", { class: "name", dir: "auto", text: "notes.pdf" }), el("div", { class: "muted small", dir: "ltr", text: "1.2 MB" })), el("a", { class: "btn", text: t("download") })),
      el("div", { class: "composer-box" }, el("span", { class: "ts-ph", text: t("type_message") }),
        el("span", { class: "ts-send" }, sendIcon()))),
    el("div", { class: "ts-buttons" }, el("button", { type: "button", class: "primary", text: t("save") }), el("button", { type: "button", text: t("cancel") }), el("button", { type: "button", class: "danger", text: t("del") })));
}
function sendIcon() {
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), path = document.createElementNS(ns, "path");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "20"); svg.setAttribute("height", "20"); svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.6"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  path.setAttribute("d", "M12 19V5M5.5 11.5 12 5l6.5 6.5"); svg.append(path); return svg;
}

function buildEditor() {
  const ed = ThemeUI.ed, d = ed.draft, sc = ThemeUI.schema;
  const wrap = el("div", { class: "theme-ui theme-editor" });

  // header
  const back = el("button", { type: "button", class: "small", text: t("ted_back"), onclick: () => { if (themeBeforeClose()) openSettings(); } });
  wrap.append(el("div", { class: "ted-top" }, back), buildSample());

  // name + base + copy-from
  const name = el("input", { type: "text", id: "ted-name", maxlength: sc.limits.name, dir: "auto", value: d.name });
  name.addEventListener("input", () => { d.name = name.value; refreshEditorState(); schedulePreview(); });
  const base = el("select", { id: "ted-base" }, el("option", { value: "dark", text: t("theme_dark") }), el("option", { value: "light", text: t("theme_light") }));
  base.value = d.base; base.addEventListener("change", () => { d.base = base.value; touch(); });
  const from = el("select", { id: "ted-from" });
  wrap.append(
    el("label", { class: "field" }, el("span", { text: t("ted_name") }), name),
    el("label", { class: "field" }, el("span", { text: t("ted_base") }), base, el("small", { class: "muted", text: t("ted_base_note") })),
    el("div", { class: "field" }, el("span", { text: t("ted_from") }), el("div", { class: "tk-ctl" }, from,
      el("button", { type: "button", class: "small", text: t("ted_from_btn"), onclick: () => copyFrom(from.value) }))));
  themeData().then((data) => {
    for (const x of [...data.presets, ...data.custom]) from.append(el("option", { value: x.id, text: x.name }));
    ed.themes = Object.fromEntries([...data.presets, ...data.custom].map((x) => [x.id, x]));
  }).catch(fail);

  // key colors + generator
  const contrast = el("input", { type: "range", min: "0.5", max: "2", step: "0.1", value: "1", id: "ted-contrast" });
  const keyBox = el("div", { class: "tk-grid" }, ...sc.colors.filter((c) => KEY_COLORS.includes(c.key)).map(colorRow));
  const gen = el("button", { type: "button", class: "primary", text: t("ted_generate"), onclick: async () => {
    try {
      const { colors } = await api("/themes/derive", "POST", { canvas: hexOf(d.colors.canvas), ink: hexOf(d.colors.ink), accent: hexOf(d.colors.accent), base: d.base, contrast: Number(contrast.value) });
      Object.assign(d.colors, colors); for (const [k, f] of ed.rows) if (!k.includes(":")) f(); touch();
    } catch (e) { fail(e); }
  } });
  wrap.append(el("details", { class: "tk-group", open: true }, el("summary", { text: t("ted_key") }),
    el("p", { class: "muted small", text: t("ted_key_note") }), keyBox,
    el("div", { class: "field" }, el("span", { text: t("ted_border_contrast") }), contrast), gen));

  // all colors
  for (const g of sc.groups) {
    const toks = sc.colors.filter((c) => c.group === g.id && !KEY_COLORS.includes(c.key));
    if (!toks.length) continue;
    wrap.append(el("details", { class: "tk-group" }, el("summary", { text: `${t("ted_colors")}: ${g.label}` }), el("div", { class: "tk-grid" }, ...toks.map(colorRow))));
  }

  // fonts
  const dl = el("datalist", { id: "font-suggestions" }, ...FONT_SUGGESTIONS.map((v) => el("option", { value: v })));
  wrap.append(el("details", { class: "tk-group" }, el("summary", { text: t("ted_fonts") }), el("p", { class: "muted small", text: t("ted_fonts_note") }), ...sc.fonts.map(fontRow), dl));

  // sizes & shape
  wrap.append(el("details", { class: "tk-group" }, el("summary", { text: t("ted_shape") }), el("div", { class: "tk-grid" }, ...sc.metrics.map(metricRow))));

  // background grid
  const grid = el("input", { type: "checkbox", id: "ted-grid" }); grid.checked = d.pattern;
  grid.addEventListener("change", () => { d.pattern = grid.checked; touch(); });
  wrap.append(el("details", { class: "tk-group" }, el("summary", { text: t("ted_background") }), el("label", { class: "check" }, grid, el("span", { text: t("ted_grid") }))));

  // custom CSS
  const css = el("textarea", { id: "ted-css", class: "css-edit", dir: "ltr", spellcheck: "false", rows: "9", maxlength: sc.limits.css, placeholder: "/* e.g. .msg { box-shadow: 0 2px 8px rgba(0,0,0,.3); } */" });
  css.value = d.css;
  const count = el("small", { class: "muted ts-count", text: `${css.value.length} / ${sc.limits.css}` });
  css.addEventListener("input", () => { d.css = css.value; count.textContent = `${css.value.length} / ${sc.limits.css}`; touch(); });
  wrap.append(el("details", { class: "tk-group" }, el("summary", { text: t("ted_css") }), el("p", { class: "muted small", text: t("ted_css_note") }), css, count));

  // footer
  ed.refs.error = el("p", { class: "theme-error hidden", role: "alert" });
  ed.refs.save = el("button", { type: "button", class: "primary", text: t("ted_save"), onclick: saveEditor });
  wrap.append(ed.refs.error, el("div", { class: "row" }, ed.refs.save,
    el("button", { type: "button", text: t("ted_cancel"), onclick: () => { if (themeBeforeClose()) openSettings(); } }),
    ed.id ? el("button", { type: "button", class: "danger", text: t("theme_delete"), onclick: deleteFromEditor }) : null));
  refreshEditorState();
  return wrap;
}
/* The generator takes #rrggbb seeds: convert whatever the user typed (names, hsl(), ...) through the browser. */
function hexOf(v) { const p = parseColor(v); return p && p.rgb ? toHex(p.rgb) : "#808080"; }
function copyFrom(id) {
  const ed = ThemeUI.ed, src = ed && ed.themes && ed.themes[id]; if (!src || !confirm(t("ted_from_confirm"))) return;
  const d = ed.draft; d.base = src.base; d.colors = clone(src.colors); d.fonts = clone(src.fonts); d.metrics = clone(src.metrics); d.pattern = src.pattern;
  ed.invalid.clear();
  const next = buildEditor(); $("#settings-body").replaceChildren(next); touch();
}

if (SAFE_MODE) setTimeout(() => toast(t("theme_safe"), "err"), 600);
