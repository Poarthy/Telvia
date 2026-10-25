"use strict";
/* Web UI: no build step or dependencies. Supports themes (/theme.css) and tiny localhost-cached 48px profile avatars. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const STR = {
  setup_title: "Setup", setup_intro: "This app reaches Telegram through GitHub. A few short steps:",
  step_session: "Telegram session", session_help: "If you haven't logged in yet, run in a terminal: python -m tools.login",
  step_token: "GitHub token", token_help: "Classic with repo + workflow scopes, or Fine-grained with Contents/Actions/Secrets/Workflows (Read & write).",
  check: "Check", step_repo: "Repository & encryption", visibility: "Repository visibility", vis_private: "Private (recommended)", vis_public: "Public",
  existing_repo: "Name of an existing empty repo (optional)", passphrase: "Session passphrase (the one you chose at login)",
  encrypt_content: "Encrypt message and file content (mandatory for public repos)",
  public_warn: "A public repo means anyone can clone the bus branch; encryption is forced on.",
  run_setup: "Start setup", search: "Search…", tab_all: "All", tab_groups: "Groups", tab_channels: "Channels", tab_private: "Private",
  pick_chat: "Choose a conversation", refresh: "Refresh", mark_read: "Mark as read", type_message: "Reply", send: "Send",
  settings: "Settings", connect: "Connect", disconnect: "Disconnect", save: "Save", saved: "Saved",
  session_ok: "✅ session.enc found", session_missing: "❌ session.enc not found", keys_missing: "⚠ TG_API_ID / TG_API_HASH not set in .env",
  older: "Older messages", reply: "Reply", edit: "Edit", del: "Delete", cancel: "Cancel", confirm_del: "Delete this message for everyone?",
  editing: "Editing message", replying: "Replying to", edited: "edited", download: "Download", get_file: "Fetch file",
  file_busy: "Fetching…", file_big: "File exceeds the allowed size limit", file_purged: "Purged", file_err: "Fetch failed; retry",
  media_photo: "Photo", media_gif: "GIF", media_video: "Video", media_sticker: "Sticker", media_voice: "Voice message", media_audio: "Audio", media_other: "Media",
  chat_group: "Group", chat_channel: "Channel", chat_private: "Private", chat_other: "Chat", failed: "Not sent", waiting: "Sending…",
  last_sync: "Last sync", empty_chats: "No chats synced yet. Wait a moment or press Refresh.",
  set_encrypt: "Encrypt content", set_max_file: "Max file size (MB)", set_auto_dl: "Auto-download documents", set_mark_read: "Opening a chat marks it read on Telegram",
  set_auto_connect: "Connect automatically on start", set_auto_start: "Restart the Action if it drops", set_shutdown: "Shut down this server when the Action disconnects",
  set_purge_h: "Auto-purge everything every … hours (0 = off)", set_big_mb: "\"Big\" file means larger than (MB)", set_big_h: "Auto-purge big files every … hours (0 = off)",
  set_poll: "Sync interval (seconds)", set_backfill: "Chats synced at startup", purge_title: "Purge data",
  purge_all: "Purge all messages and files", purge_big: "Purge big files only", confirm_purge: "The bus history on GitHub will be rewritten (force-push) and data erased. Continue?",
  purge_done: "Purge complete", purge_note: "GitHub may keep old objects until garbage collection; delete/recreate the repo for hard removal.",
  repo: "Repository", unread: "unread",
  set_dir: "Layout direction", dir_ltr: "Left-to-right (LTR)", dir_rtl: "Right-to-left (RTL)", toggle_dir: "Switch layout direction",
};

const S = {
  dir: "ltr", st: null, chats: [], tab: "all", q: "", cur: null, msgs: [], pending: [], reply: null, editing: null, drafts: {},
  hash: { chats: "", msgs: "" }, requested: new Set(), view: "", setupPoll: null, token_ok: false, lastSyncSeen: 0,
  busy: { state: false, chats: false, msgs: false },
};
const t = (k) => STR[k] || k;
const LOCALE = "en";

/* ---------------------------------------------------------------- helpers */
async function api(path, method = "GET", body) {
  let r;
  try {
    r = await fetch("/api" + path, {
      method, headers: { "X-Requested-With": "tg-bridge", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) { throw new Error("server unreachable"); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) {
    const msg = (data && data.error) || (data && data.detail ? "invalid input" : "HTTP " + r.status);
    throw new Error(msg);
  }
  return data;
}
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind; el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), "err");
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) n.append(k.nodeType ? k : document.createTextNode(k));
  return n;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitResult(cid, timeout = 180000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try {
      const { result } = await api("/commands/" + cid);
      if (result) return result;
    } catch (e) {
      await sleep(350);
    }
    await sleep(60);
  }
  return { ok: false, error: "timeout" };
}
function fmtSize(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
function fmtChatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? fmtTime(ts) : d.toLocaleDateString(LOCALE, { month: "short", day: "numeric" });
}
const MEDIA_ICON = { photo: "🖼", gif: "🎞", video: "🎬", sticker: "🏷", voice: "🎙", audio: "🎵", document: "📄", other: "📎" };
const mediaLabel = (m) => t("media_" + (["photo", "gif", "video", "sticker", "voice", "audio"].includes(m) ? m : "other"));
function colorFor(id) { return `hsl(${Math.abs(Number(id) * 47) % 360} var(--avatar-s, 45%) var(--avatar-l, 45%))`; }
function initial(title) { return ([...(title || "?").trim()][0] || "?").toUpperCase(); }
function avatarUrl(chat) {
  return `/api/chats/${encodeURIComponent(chat.id)}/avatar?v=${encodeURIComponent(chat.avatar)}`;
}
function avatarNode(chat) {
  const avatar = el("span", { class: "avatar", "aria-hidden": "true", style: `background:${colorFor(chat.id)}` });
  if (chat.avatar) {
    const image = el("img", {
      class: "avatar-image", src: avatarUrl(chat), alt: "",
      onload: () => avatar.classList.add("has-image"),
      onerror: () => image.remove(),
    });
    if (image.complete && image.naturalWidth > 0) avatar.classList.add("has-image");
    avatar.append(image);
  }
  avatar.append(el("span", { class: "avatar-initial", text: initial(chat.title) }));
  return avatar;
}

/* Layout direction */
const DIR_KEY = "tgb-dir";
function setDirection(pref) {
  S.dir = pref === "rtl" ? "rtl" : "ltr";
  document.documentElement.dir = S.dir;
  document.documentElement.lang = "en";
  try { localStorage.setItem(DIR_KEY, S.dir); } catch (e) { /* storage unavailable */ }
  $$("[data-i]").forEach((n) => { n.textContent = t(n.dataset.i); });
  $$("[data-i-ph]").forEach((n) => { n.placeholder = t(n.dataset.iPh); });
  $$("[data-i-title]").forEach((n) => { n.title = t(n.dataset.iTitle); n.setAttribute("aria-label", t(n.dataset.iTitle)); });
  const b = $("#btn-dir");
  if (b) { b.title = t("toggle_dir"); b.setAttribute("aria-label", t("toggle_dir")); b.dataset.dir = S.dir; }
}
function initialDirection() {
  try { return localStorage.getItem(DIR_KEY) === "rtl" ? "rtl" : "ltr"; } catch (e) { return "ltr"; }
}

/* Theme stylesheet */
const SAFE_MODE = new URLSearchParams(location.search).has("safe");
function reloadThemeLink() {
  const old = $("#theme-link"); if (!old) return;
  const next = document.createElement("link");
  next.rel = "stylesheet"; next.href = "/theme.css?" + (SAFE_MODE ? "safe=1&" : "") + "v=" + Date.now();
  next.addEventListener("load", () => { old.remove(); next.id = "theme-link"; const p = $("#theme-preview"); if (p) p.remove(); });
  next.addEventListener("error", () => next.remove());
  old.after(next);
}
function noteThemeRev(rev) { S.themeRev = rev; reloadThemeLink(); }

/* ---------------------------------------------------------------- wizard */
function showView(v) {
  if (S.view === v) return;
  S.view = v;
  $("#setup").classList.toggle("hidden", v !== "setup");
  $("#main").classList.toggle("hidden", v !== "main");
}
function renderSetup() {
  const st = S.st;
  $("#session-status").textContent = st.session_file ? t("session_ok") : t("session_missing");
  if (st.session_file && !st.api_keys_env) $("#session-status").textContent += "  —  " + t("keys_missing");
  const log = (st.setup && st.setup.log) || [];
  $("#setup-log").textContent = log.join("\n");
  $("#setup-log").scrollTop = 1e9;
  $("#btn-setup").disabled = st.setup.state === "running" || !st.session_file || !(S.token_ok || st.token_set);
  const pub = $("#visibility").value === "public";
  $("#encrypt").checked = pub ? true : $("#encrypt").checked; $("#encrypt").disabled = pub;
  $("#pub-warn").classList.toggle("hidden", !pub);
}
$("#visibility").addEventListener("change", () => S.st && renderSetup());
$("#btn-token").addEventListener("click", async () => {
  const ul = $("#token-msgs"); ul.textContent = "";
  try {
    const info = await api("/setup/token", "POST", { token: $("#token").value.trim() });
    S.token_ok = true; $("#token").value = "";
    ul.append(el("li", { text: `✅ ${info.login} (${info.kind})`, style: "color:var(--ok)" }));
    info.warnings.forEach((w) => ul.append(el("li", { text: w })));
    refreshState();
  } catch (e) { S.token_ok = false; ul.append(el("li", { text: "❌ " + e.message, style: "color:var(--danger)" })); }
});
$("#btn-setup").addEventListener("click", async () => {
  try {
    await api("/setup/run", "POST", {
      visibility: $("#visibility").value, existing_repo: $("#existing-repo").value.trim() || null,
      passphrase: $("#passphrase").value, encrypt: $("#encrypt").checked,
    });
    $("#passphrase").value = "";
    refreshState();
  } catch (e) { fail(e); }
});

/* ---------------------------------------------------------------- status */
async function refreshState() {
  if (S.busy.state) return;
  S.busy.state = true;
  try {
    let st;
    try { st = await api("/state"); } catch (e) { $("#conn-label").textContent = "✖"; return; }
    S.st = st;
    if (st.settings && st.settings.theme_rev !== undefined && S.themeRev !== st.settings.theme_rev) {
      const first = S.themeRev === undefined; S.themeRev = st.settings.theme_rev; if (!first) reloadThemeLink();
    }
    if (st.settings && st.settings.ui_dir && S.dirPref !== st.settings.ui_dir) { S.dirPref = st.settings.ui_dir; setDirection(S.dirPref); S.hash = { chats: "", msgs: "" }; }
    if (!st.configured || st.setup.state === "running" || (st.setup.state === "error" && S.view === "setup")) {
      showView("setup"); renderSetup(); return;
    }
    showView("main");
    const c = $("#conn"); c.dataset.state = st.status.state; c.title = st.status.label; c.setAttribute("aria-label", st.status.label); $("#conn-label").textContent = st.status.label;
    const on = ["connected", "connecting"].includes(st.status.state) && !st.user_stopped;
    $("#btn-toggle").textContent = on ? t("disconnect") : t("connect");
    $("#btn-toggle").dataset.on = on ? "1" : "";
    const foot = [];
    if (st.last_sync) foot.push(`${t("last_sync")}: ${fmtTime(st.last_sync)}`);
    if (st.action_detail) foot.push(st.action_detail);
    if (st.error) foot.push("⚠ " + st.error);
    if (st.api_msg) foot.push("ℹ " + st.api_msg);
    $("#sidebar-foot").textContent = foot.join(" · ");

    if (st.last_sync && st.last_sync !== S.lastSyncSeen) {
      S.lastSyncSeen = st.last_sync;
      refreshChats();
      if (S.cur && !document.hidden) loadMessages();
    }
  } finally {
    S.busy.state = false;
  }
}
$("#btn-dir").addEventListener("click", async () => {
  const next = S.dir === "rtl" ? "ltr" : "rtl";
  setDirection(next);
  try { await api("/settings", "POST", { ui_dir: next }); S.dirPref = next; } catch (e) { fail(e); }
});
$("#btn-toggle").addEventListener("click", async () => {
  try {
    const r = $("#btn-toggle").dataset.on ? await api("/disconnect", "POST") : await api("/connect", "POST");
    (r.notes || []).forEach((n) => toast(n)); if (r.note) toast(r.note);
    refreshState();
  } catch (e) { fail(e); }
});

/* ---------------------------------------------------------------- chat list */
async function refreshChats() {
  if (!S.st || !S.st.configured || S.busy.chats) return;
  S.busy.chats = true;
  try {
    let d;
    try { d = await api("/chats"); } catch (e) { return; }
    const h = JSON.stringify([d.chats, S.tab, S.q, S.cur && S.cur.id, S.dir]);
    S.chats = d.chats;
    if (h === S.hash.chats) return;
    S.hash.chats = h; renderChats();
  } finally {
    S.busy.chats = false;
  }
}
function renderChats() {
  const box = $("#chatlist"), q = S.q.trim().toLowerCase();
  const list = S.chats.filter((c) => (S.tab === "all" || c.type === S.tab) && (!q || (c.title || "").toLowerCase().includes(q)))
    .sort((a, b) => (b.pinned - a.pinned) || (b.last_date - a.last_date));
  box.textContent = "";
  if (!list.length) { box.append(el("p", { class: "muted small", style: "padding:16px", text: t("empty_chats") })); return; }
  for (const c of list) {
    const prefix = c.last_media ? `${MEDIA_ICON[c.last_media] || "📎"} ${c.last_media === "document" ? "" : mediaLabel(c.last_media)}`.trim() : "";
    const lastText = (c.last_text || "").trim();
    const last = el("span", { class: "ci-last muted", dir: lastText ? "auto" : null },
      prefix ? el("bdi", { dir: "ltr", text: prefix }) : null, prefix && lastText ? " " : null, lastText || null);
    box.append(el("button", { type: "button", class: "chat-item" + (S.cur && S.cur.id === c.id ? " active" : ""), onclick: () => openChat(c) },
      avatarNode(c),
      el("div", { class: "ci-main" },
        el("div", { class: "ci-top" }, el("span", { class: "ci-title", dir: "auto", text: c.title || "—" }), el("span", { class: "muted small", text: fmtChatTime(c.last_date) })),
        el("div", { class: "ci-bot" }, last,
          c.unread ? el("span", { class: "badge", title: t("unread"), text: c.unread > 999 ? "999+" : String(c.unread) }) : null))));
  }
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  S.tab = b.dataset.tab; $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b)); refreshChats();
});
$("#search").addEventListener("input", (e) => { S.q = e.target.value; refreshChats(); });

/* ---------------------------------------------------------------- chat */
async function openChat(c) {
  if (S.cur) { const text = $("#input").value; if (S.editing || !text.trim()) delete S.drafts[S.cur.id]; else S.drafts[S.cur.id] = text; }
  S.cur = c; S.msgs = []; S.pending = []; S.hash.msgs = ""; cancelContext();
  setInput(S.drafts[c.id] || "");
  document.body.classList.add("in-chat");
  $("#empty").classList.add("hidden"); $("#chat-view").classList.remove("hidden");
  $("#chat-title").textContent = c.title || "—";
  $("#chat-sub").textContent = t("chat_" + c.type);
  $("#messages").textContent = ""; S.hash.chats = ""; renderChats();
  await loadMessages(true);
  if (!S.msgs.length) fetchLatest();
  if (S.st && S.st.settings.mark_read_on_open) api(`/chats/${c.id}/mark_read`, "POST").then(() => refreshChats()).catch(() => {});
}
async function fetchLatest() {
  if (!S.cur) return;
  try { const { command_id } = await api(`/chats/${S.cur.id}/fetch`, "POST", { limit: 40, offset_id: 0 }); const r = await waitResult(command_id); if (!r.ok) toast(r.error, "err"); await loadMessages(); } catch (e) { fail(e); }
}
async function loadMessages(forceScroll = false) {
  const cur = S.cur; if (!cur || (S.busy.msgs && !forceScroll)) return;
  S.busy.msgs = true;
  try {
    let d;
    try { d = await api(`/chats/${cur.id}/messages?limit=150`); } catch (e) { return; }
    if (!S.cur || S.cur.id !== cur.id) return;
    S.msgs = d.messages;
    const h = JSON.stringify([d.messages, S.pending.map((p) => p.state + p.text), S.dir]);
    if (h === S.hash.msgs && !forceScroll) return;
    S.hash.msgs = h; renderMessages(forceScroll);
  } finally {
    S.busy.msgs = false;
  }
}
function replyPreview(id) {
  const m = S.msgs.find((x) => x.id === id);
  if (!m) return "…";
  return m.text ? m.text.slice(0, 120) : (m.media_type ? `${MEDIA_ICON[m.media_type] || ""} ${mediaLabel(m.media_type)}` : "…");
}
function fileCard(m) {
  const f = m.file, box = el("div", { class: "file" }, el("div", { class: "ico", text: "📄" }));
  const info = el("div", { class: "info" }, el("div", { class: "name", dir: "auto", text: f.name }), el("div", { class: "muted small", text: fmtSize(f.size) }));
  box.append(info);
  const fid = f.id, st = f.status;
  if (st === "ready") box.append(el("a", { class: "btn", href: `/api/files/${encodeURIComponent(fid)}/download`, download: f.name, text: t("download") }));
  else if (st === "too_large_for_git") info.append(el("div", { class: "warn", text: `${t("file_big")} (${f.limit_mb} MB)` }));
  else if (st === "purged") info.append(el("div", { class: "warn", text: t("file_purged") }));
  else if (st === "downloading" || S.requested.has(fid)) info.append(el("div", { class: "muted small", text: t("file_busy") }));
  else box.append(el("button", { text: st === "error" ? t("file_err") : t("get_file"), onclick: () => requestFile(m) }));
  return box;
}
async function requestFile(m) {
  S.requested.add(m.file.id); S.hash.msgs = ""; renderMessages(false);
  try {
    const { command_id } = await api("/files/request", "POST", { chat_id: m.chat_id, message_id: m.id });
    const r = await waitResult(command_id, 600000);
    if (!r.ok) toast(r.error || "error", "err"); else if (r.out && r.out.status === "too_large_for_git") toast(t("file_big"), "err");
  } catch (e) { fail(e); }
  S.requested.delete(m.file.id); S.hash.msgs = ""; loadMessages();
}
function msgNode(m) {
  const isGroup = S.cur && S.cur.type !== "private";
  const n = el("div", { class: "msg " + (m.out ? "out" : "in"), "data-id": m.id });
  if (m.reply_to) n.append(el("div", { class: "reply", dir: "auto", text: replyPreview(m.reply_to), onclick: () => { const x = $(`.msg[data-id="${m.reply_to}"]`); if (x) x.scrollIntoView({ block: "center", behavior: "smooth" }); } }));
  if (!m.out && isGroup && m.sender) n.append(el("div", { class: "sender", dir: "auto", text: m.sender }));
  if (m.media_type && m.media_type !== "document") n.append(el("div", { class: "media" }, el("span", { class: "ico", text: MEDIA_ICON[m.media_type] || "📎" }), el("span", { class: "lbl", text: mediaLabel(m.media_type) })));
  if (m.file) n.append(fileCard(m));
  if (m.text) n.append(el("div", { class: "text", dir: "auto", text: m.text }));
  n.append(el("div", { class: "meta" }, m.edit_date ? el("span", { text: t("edited") }) : null, el("span", { text: fmtTime(m.date) })));
  if (!m.service) {
    const acts = el("div", { class: "acts" }, el("button", { type: "button", text: "↩", title: t("reply"), "aria-label": t("reply"), onclick: () => setReply(m) }));
    if (m.out && !m.media_type) acts.append(el("button", { type: "button", text: "✎", title: t("edit"), "aria-label": t("edit"), onclick: () => setEdit(m) }));
    if (m.out) acts.append(el("button", { type: "button", text: "🗑", title: t("del"), "aria-label": t("del"), onclick: () => delMsg(m) }));
    n.append(acts);
  }
  return n;
}
function renderMessages(forceScroll) {
  const box = $("#messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const prevTop = box.scrollTop;
  box.textContent = "";
  if (S.msgs.length) box.append(el("button", { id: "older", class: "small", text: t("older"), onclick: loadOlder }));
  let day = "";
  for (const m of S.msgs) {
    const d = new Date(m.date * 1000).toLocaleDateString(LOCALE, { year: "numeric", month: "long", day: "numeric" });
    if (d !== day) { day = d; box.append(el("div", { class: "day", text: d })); }
    box.append(msgNode(m));
  }
  for (const p of S.pending) {
    box.append(el("div", { class: "msg out pending" + (p.state === "failed" ? " failed" : "") },
      el("div", { class: "text", dir: "auto", text: p.text }),
      el("div", { class: "meta", text: p.state === "failed" ? `${t("failed")}: ${p.error || ""}` : t("waiting") })));
  }
  if (forceScroll || nearBottom || S.pending.length) box.scrollTop = box.scrollHeight; else box.scrollTop = prevTop;
}
async function loadOlder() {
  if (!S.cur || !S.msgs.length) return;
  const oldest = Math.min(...S.msgs.map((m) => m.id));
  try {
    const { command_id } = await api(`/chats/${S.cur.id}/fetch`, "POST", { limit: 50, offset_id: oldest });
    const r = await waitResult(command_id); if (!r.ok) toast(r.error, "err");
    await loadMessages();
  } catch (e) { fail(e); }
}
$("#btn-fetch").addEventListener("click", async () => { toast("…"); await fetchLatest(); api("/chats/refresh", "POST").catch(() => {}); });
$("#btn-markread").addEventListener("click", () => { if (S.cur) api(`/chats/${S.cur.id}/mark_read`, "POST").then(() => { refreshChats(); toast("✓", "ok"); }).catch(fail); });
$("#btn-back").addEventListener("click", () => document.body.classList.remove("in-chat"));

/* ---------------------------------------------------------------- send / edit / delete */
function autosize() { const a = $("#input"); a.style.height = "auto"; a.style.height = Math.min(a.scrollHeight, 168) + "px"; }
function updateSend() { $("#btn-send").disabled = !S.cur || !$("#input").value.trim(); }
function setInput(v) { $("#input").value = v; autosize(); updateSend(); }
function cancelContext() { S.reply = null; S.editing = null; $("#composer-ctx").classList.add("hidden"); }
function showContext(label, text) { $("#ctx-text").replaceChildren(el("bdi", { text: `${label}:` }), " ", el("bdi", { dir: "auto", text })); $("#composer-ctx").classList.remove("hidden"); $("#input").focus(); }
function setReply(m) { S.editing = null; S.reply = m.id; showContext(t("replying"), replyPreview(m.id)); }
function setEdit(m) { S.reply = null; S.editing = m.id; setInput(m.text || ""); showContext(t("editing"), (m.text || "").slice(0, 80)); }
$("#ctx-cancel").addEventListener("click", () => { const wasEditing = !!S.editing; cancelContext(); if (wasEditing) setInput(""); $("#input").focus(); });
async function delMsg(m) {
  if (!confirm(t("confirm_del"))) return;
  try {
    const { command_id } = await api(`/chats/${S.cur.id}/delete`, "POST", { message_ids: [m.id] });
    loadMessages();
    const r = await waitResult(command_id);
    if (!r.ok) toast(r.error, "err");
    loadMessages();
  } catch (e) { fail(e); }
}
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ta = $("#input"), text = ta.value.trim();
  if (!text || !S.cur) return;
  const chatId = S.cur.id;
  setInput("");
  if (S.editing) {
    const id = S.editing; cancelContext();
    const restore = () => { if (S.cur && S.cur.id === chatId && !$("#input").value) { S.editing = id; setInput(text); showContext(t("editing"), text.slice(0, 80)); } };
    try {
      const { command_id } = await api(`/chats/${chatId}/edit`, "POST", { message_id: id, text });
      loadMessages();
      const r = await waitResult(command_id); if (!r.ok) { toast(r.error, "err"); restore(); loadMessages(); } else loadMessages();
    } catch (err) { fail(err); restore(); loadMessages(); }
    return;
  }
  const p = { text, state: "pending", error: "" };
  S.pending.push(p); const reply = S.reply; cancelContext(); S.hash.msgs = ""; renderMessages(true);
  try {
    const { command_id } = await api(`/chats/${chatId}/send`, "POST", { text, reply_to: reply });
    const r = await waitResult(command_id);
    if (r.ok) S.pending = S.pending.filter((x) => x !== p); else { p.state = "failed"; p.error = r.error; }
  } catch (err) { p.state = "failed"; p.error = err.message; }
  S.hash.msgs = ""; if (S.cur && S.cur.id === chatId) loadMessages(true);
  refreshChats();
});
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (!$("#btn-send").disabled) $("#composer").requestSubmit(); } });
$("#input").addEventListener("input", () => { autosize(); updateSend(); });
$("#btn-send").addEventListener("pointerdown", (e) => e.preventDefault());
$(".composer-box").addEventListener("pointerdown", (e) => { if (e.target.closest("textarea, button")) return; e.preventDefault(); $("#input").focus(); });
$("#input").addEventListener("keydown", (e) => { if (e.key === "Escape" && (S.editing || S.reply)) { e.preventDefault(); $("#ctx-cancel").click(); } });

(function trackDock() {
  const dock = $("#dock"); if (!dock || !window.ResizeObserver) return;
  new ResizeObserver(() => {
    const box = $("#messages"), stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    document.documentElement.style.setProperty("--composer-h", dock.offsetHeight + "px");
    if (stick) box.scrollTop = box.scrollHeight;
  }).observe(dock);
})();

(function trackViewport() {
  const vv = window.visualViewport; if (!vv) return;
  const root = document.documentElement.style;
  const sync = () => {
    const box = $("#messages"), stick = box && box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (Math.abs(vv.scale - 1) > 0.01) { root.removeProperty("--vv-h"); root.removeProperty("--vv-top"); return; }
    root.setProperty("--vv-h", vv.height + "px"); root.setProperty("--vv-top", vv.offsetTop + "px");
    if (stick) box.scrollTop = box.scrollHeight;
  };
  vv.addEventListener("resize", sync); vv.addEventListener("scroll", sync); sync();
})();

/* ---------------------------------------------------------------- settings */
function settingRow(key, label, type, value, extra = {}) {
  if (type === "bool") {
    const cb = el("input", { type: "checkbox", "data-k": key, disabled: extra.disabled }); cb.checked = !!value;
    return el("label", { class: "check" }, cb, el("span", { text: label }));
  }
  if (type === "select") {
    const s = el("select", { "data-k": key }, ...extra.options.map(([v, l]) => el("option", { value: v, text: l })));
    s.value = value; return el("label", { class: "field" }, el("span", { text: label }), s);
  }
  return el("label", { class: "field" }, el("span", { text: label }), el("input", { type: "number", "data-k": key, value, min: 0, step: "any" }));
}
function openSettings() {
  if (typeof themeBeforeClose === "function" && !themeBeforeClose()) return;
  const s = S.st.settings, body = $("#settings-body"); body.textContent = "";
  body.append(el("p", { class: "muted small" }, el("bdi", { text: `${t("repo")}:` }), " ", el("bdi", { dir: "ltr", text: `${S.st.repo} (${s.visibility})` })));
  body.append(
    settingRow("encrypt", t("set_encrypt"), "bool", s.encrypt, { disabled: s.visibility === "public" }),
    settingRow("max_file_mb", t("set_max_file"), "num", s.max_file_mb),
    settingRow("auto_download_documents", t("set_auto_dl"), "bool", s.auto_download_documents),
    settingRow("mark_read_on_open", t("set_mark_read"), "bool", s.mark_read_on_open),
    settingRow("auto_connect", t("set_auto_connect"), "bool", s.auto_connect),
    settingRow("auto_start", t("set_auto_start"), "bool", s.auto_start),
    settingRow("shutdown_on_disconnect", t("set_shutdown"), "bool", s.shutdown_on_disconnect),
    settingRow("poll_interval", t("set_poll"), "num", s.poll_interval),
    settingRow("startup_backfill_chats", t("set_backfill"), "num", s.startup_backfill_chats),
    settingRow("ui_dir", t("set_dir"), "select", s.ui_dir, { options: [["ltr", t("dir_ltr")], ["rtl", t("dir_rtl")]] }),
    typeof buildThemeSection === "function" ? buildThemeSection() : document.createComment("no theme settings"),
    el("h3", { text: t("purge_title") }),
    settingRow("auto_purge_hours", t("set_purge_h"), "num", s.auto_purge_hours),
    settingRow("big_file_mb", t("set_big_mb"), "num", s.big_file_mb),
    settingRow("big_file_purge_hours", t("set_big_h"), "num", s.big_file_purge_hours),
    el("div", { class: "row", style: "margin-top:14px" },
      el("button", { class: "primary", text: t("save"), onclick: saveSettings }),
      el("button", { class: "danger", text: t("purge_all"), onclick: () => purge("all") }),
      el("button", { text: t("purge_big"), onclick: () => purge("big") })),
    el("p", { class: "muted small", text: t("purge_note") }));
  $("#modal").classList.remove("hidden");
}
async function saveSettings() {
  const v = {};
  $$("#settings-body [data-k]").forEach((n) => {
    const k = n.dataset.k;
    if (n.type === "checkbox") { if (!n.disabled) v[k] = n.checked; }
    else if (n.type === "number") { if (n.value !== "") v[k] = Number(n.value); }
    else v[k] = n.value;
  });
  for (const k of ["max_file_mb", "big_file_mb", "startup_backfill_chats"]) if (k in v) v[k] = Math.round(v[k]);
  try { await api("/settings", "POST", v); toast(t("saved"), "ok"); closeSettings(); refreshState(); } catch (e) { fail(e); }
}
async function purge(scope) {
  if (!confirm(t("confirm_purge"))) return;
  try { const r = await api("/purge", "POST", { scope }); toast(`${t("purge_done")} (${r.removed || 0})`, "ok"); S.hash = { chats: "", msgs: "" }; S.msgs = []; if (S.cur) renderMessages(true); refreshChats(); } catch (e) { fail(e); }
}
function closeSettings() {
  if (typeof themeBeforeClose === "function" && !themeBeforeClose()) return;
  $("#modal").classList.add("hidden");
}
$("#btn-menu").addEventListener("click", openSettings);
$("#modal-close").addEventListener("click", closeSettings);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeSettings(); });

/* ---------------------------------------------------------------- start */
setDirection(initialDirection());
refreshState().then(refreshChats);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refreshState(); refreshChats(); if (S.cur) loadMessages(); }
});
setInterval(() => { if (!document.hidden || S.view === "setup") refreshState(); }, 1500);
setInterval(() => { if (!document.hidden) refreshChats(); }, 3000);
setInterval(() => { if (S.cur && !document.hidden) loadMessages(); }, 1500);