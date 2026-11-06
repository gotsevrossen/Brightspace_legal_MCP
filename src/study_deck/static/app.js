/* Study Deck front end. No framework; every piece of server text goes through esc(). */
"use strict";

const main = document.getElementById("main");
const topRight = document.getElementById("topRight");
const toastEl = document.getElementById("toast");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ICON = {
  check: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  up: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>',
  alert: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  info: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/></svg>',
  file: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M12 17v-6M9.5 13.5 12 11l2.5 2.5"/></svg>',
  gear: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
  cards: '<svg viewBox="0 0 64 64" width="64" height="64" aria-hidden="true"><rect x="8" y="10" width="30" height="42" rx="6" fill="#CFB991" transform="rotate(-8 23 31)"/><rect x="22" y="10" width="32" height="44" rx="6" fill="#000"/><path d="M29 24h18M29 32h18M29 40h11" stroke="#CFB991" stroke-width="3" stroke-linecap="round"/></svg>',
};

/* ------------------------------------------------------------------ helpers */

async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, headers: { "X-Study-Deck": "1" }, credentials: "same-origin" };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  if (form) opts.body = form;
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.classList.toggle("err", isError);
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), isError ? 6000 : 3200);
}

async function guarded(button, fn) {
  if (button) button.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { if (button && button.isConnected) button.disabled = false; }
}

/* segmented control: buttons with role=radio and a sliding thumb */
function segHTML(name, options, value, label) {
  return `<div class="seg" role="radiogroup" aria-label="${esc(label)}" data-seg="${esc(name)}"><span class="thumb" aria-hidden="true"></span>${options
    .map(([id, text]) => `<button type="button" role="radio" data-value="${esc(id)}" aria-checked="${id === value}" tabindex="${id === value ? 0 : -1}">${esc(text)}</button>`)
    .join("")}</div>`;
}
function initSeg(root, onChange) {
  const buttons = $$("button", root);
  const place = () => {
    const at = Math.max(0, buttons.findIndex((b) => b.getAttribute("aria-checked") === "true"));
    root.style.setProperty("--n", buttons.length);
    root.style.setProperty("--i", at);
  };
  const pick = (b, focus) => {
    buttons.forEach((x) => { const yes = x === b; x.setAttribute("aria-checked", yes); x.tabIndex = yes ? 0 : -1; });
    if (focus) b.focus();
    place();
    onChange(b.dataset.value);
  };
  buttons.forEach((b, i) => {
    b.addEventListener("click", () => pick(b, false));
    b.addEventListener("keydown", (e) => {
      const d = ["ArrowRight", "ArrowDown"].includes(e.key) ? 1 : ["ArrowLeft", "ArrowUp"].includes(e.key) ? -1 : 0;
      if (d) { e.preventDefault(); pick(buttons[(i + d + buttons.length) % buttons.length], true); }
    });
  });
  place();
}

/* ------------------------------------------------------------------ top bar */

let me = null;
async function loadMe() {
  me = await api("/api/me");
  const bs = me.brightspace;
  topRight.innerHTML = `${me.demo_mode ? '<span class="pill gold">Demo mode</span>' : ""}
    <span class="pill"><span class="dot ${bs.connected ? "on" : ""}"></span>${bs.connected ? `Brightspace · ${bs.courses} course${bs.courses === 1 ? "" : "s"}` : "Brightspace not connected"}</span>
    <a class="pill pill-link" href="#/settings">${ICON.gear}Settings</a>`;
  return me;
}

/* ----------------------------------------------------------------- routing */

let stopPolling = () => {};
async function route() {
  stopPolling();
  const hash = location.hash || "#/";
  if (hash === "#connected") { history.replaceState(null, "", "#/"); toast("Brightspace connected."); }
  if (hash === "#signin-failed") { history.replaceState(null, "", "#/"); toast("Brightspace sign-in didn't finish. Try again.", true); }
  try {
    await loadMe();
    const parts = (location.hash || "#/").slice(2).split("/");
    if (parts[0] === "host") await hostView();
    else if (parts[0] === "settings") settingsView();
    else if (parts[0] === "game" && parts[1]) await gameView(parts[1].toUpperCase());
    else homeView(parts[0] === "join" ? (parts[1] || "") : "");
  } catch (e) {
    main.innerHTML = `<section class="card"><h2>Something went wrong</h2><p class="muted">${esc(e.message)}</p><a class="btn dark" href="#/">Back to start</a></section>`;
  }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

/* -------------------------------------------------------------------- home */

function connectCard() {
  const bs = me.brightspace;
  if (bs.connected) {
    return `<section class="card">
      <div class="card-head"><div><p class="eyebrow">Brightspace</p><h2>Connected</h2></div><span class="pill"><span class="dot on"></span>${esc(bs.courses)} courses</span></div>
      <p class="muted small">${bs.mode === "demo" ? "Sample courses for trying the game. No real Brightspace data is used." : "Signed in through Purdue. Your session is held in memory only and ends when you disconnect or go idle."}</p>
      <div><button class="btn line sm" id="disconnect" type="button">Disconnect</button></div>
    </section>`;
  }
  return `<section class="card">
    <p class="eyebrow">Brightspace</p><h2>Bring your courses</h2>
    <p class="muted small">${me.demo_mode
      ? "This server runs in demo mode: connecting loads two sample courses. Real Brightspace sign-in turns on once Purdue registers this app."
      : "Sign in on Purdue's own page. This app never sees your password, and only reads course content from courses you pick."}</p>
    <div><a class="btn dark" href="/auth/brightspace/start">${me.demo_mode ? "Load sample courses" : "Connect Brightspace"}</a></div>
    <p class="hint">You can also play with your own notes and files only.</p>
  </section>`;
}

function homeView(prefill) {
  main.innerHTML = `
    <section>
      <p class="eyebrow">Board game companion</p>
      <h1>Study<br>Deck</h1>
      <p class="lede">Pick what you're studying, pool course material and notes with your group, and play with a deck of questions that keeps refilling itself.</p>
    </section>
    ${me.demo_mode ? `<div class="notice demo">${ICON.info}<p class="small"><b>Demo mode.</b> Sample courses${me.genai.offline_demo ? " and a simple offline card writer" : ""}. Real games use Purdue Brightspace and Purdue GenAI Studio once they're set up.</p></div>` : ""}
    <div class="grid g2">
      <section class="card lead">
        <p class="eyebrow">Host</p><h2>Start a game</h2>
        <p class="muted">Choose courses, modules or topics, pick the question style, and share the join code with your table.</p>
        <div><a class="btn dark big" href="#/host">Host a game</a></div>
      </section>
      <section class="card">
        <p class="eyebrow">Player</p><h2>Join a game</h2>
        <form id="joinForm" class="grid" novalidate>
          <label class="f">Join code<input class="input code-input" name="code" maxlength="6" autocomplete="off" autocapitalize="characters" spellcheck="false" value="${esc(prefill)}" required></label>
          <label class="f">Your first name<input class="input" name="name" maxlength="24" autocomplete="given-name" value="${esc(me.name)}" required></label>
          <button class="btn gold wide" type="submit">Join</button>
        </form>
      </section>
    </div>
    ${connectCard()}`;
  $("#joinForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    const code = f.code.value.trim().toUpperCase();
    guarded($("button[type=submit]", f), async () => {
      if (code.length !== 6) throw new Error("Join codes have 6 characters.");
      await api(`/api/sessions/${encodeURIComponent(code)}/join`, { method: "POST", json: { name: f.name.value } });
      location.hash = `#/game/${code}`;
    });
  });
  const dc = $("#disconnect");
  if (dc) dc.addEventListener("click", () => guarded(dc, async () => { await api("/api/brightspace/disconnect", { method: "POST" }); route(); }));
}

/* ---------------------------------------------------------------- settings */

function genaiCardHTML(g, models) {
  const source = { yours: "entered here, kept in this app's memory until you go idle", saved: "remembered in this computer's system keychain", server: "set up by whoever runs this server" }[g.source];
  const list = models && models.length ? models : g.configured ? [g.model] : [];
  return `
    <div class="card-head"><div><p class="eyebrow">Question writer</p><h2>Purdue GenAI Studio</h2></div>
      <span class="pill"><span class="dot ${g.configured ? "on" : ""}"></span>${g.configured ? `Connected · ${esc(g.model)}` : "Not set up"}</span></div>
    ${g.configured ? `<p class="small muted">Using the key ${esc(g.key_hint || "")} ${source ? `(${esc(source)})` : ""}.</p>` : ""}
    <form id="genaiForm" class="grid" novalidate>
      <label class="f">API key
        <input class="input" name="key" type="password" autocomplete="off" spellcheck="false" placeholder="${g.configured ? "Paste a new key to replace it" : "Paste your API key"}">
        <span class="hint">Get one at <a href="https://genai.rcac.purdue.edu" target="_blank" rel="noopener noreferrer">genai.rcac.purdue.edu</a>: sign in with your Purdue account, then create an API key in your account settings.</span>
      </label>
      <div class="row grow-first">
        <label class="f">Model
          <select class="input" name="model" ${list.length ? "" : "disabled"}>${list.length ? list.map((m) => `<option ${m === g.model ? "selected" : ""}>${esc(m)}</option>`).join("") : "<option>Check your key to see models</option>"}</select>
        </label>
        <button class="btn line" type="button" id="checkKey">Check key</button>
      </div>
      ${g.can_remember ? `<label class="check"><input type="checkbox" name="remember" ${g.remembered ? "checked" : ""}><span>Remember on this computer. It's stored in your system keychain, not in a file, and only used when you open the app here.</span></label>` : ""}
      <div class="row">
        <button class="btn gold" type="submit">Save</button>
        ${g.source === "yours" || g.source === "saved" ? '<button class="linkbtn" type="button" id="forgetKey">Remove key</button>' : ""}
      </div>
      <p class="hint">Your key is only sent to Purdue GenAI Studio and is never shown to other players. Only course text goes to the model: no names or grades.</p>
    </form>`;
}

function settingsView() {
  main.innerHTML = `
    <section><p class="eyebrow">Settings</p><h1>Settings</h1>
      <p class="lede">Connect the services Study Deck uses. Everything here applies to you only.</p></section>
    <section class="card" id="genaiCard"></section>
    ${connectCard()}`;
  const card = $("#genaiCard");
  const paint = (g, models) => {
    card.innerHTML = genaiCardHTML(g, models);
    const form = $("#genaiForm", card);
    const fill = (list) => {
      const sel = form.model;
      sel.disabled = false;
      sel.innerHTML = list.map((m) => `<option ${m === g.model ? "selected" : ""}>${esc(m)}</option>`).join("");
    };
    $("#checkKey", card).addEventListener("click", (e) => guarded(e.currentTarget, async () => {
      const { models: list } = await api("/api/settings/genai/models", { method: "POST", json: { key: form.key.value } });
      fill(list);
      toast(`Key works. ${list.length} model${list.length === 1 ? "" : "s"} available.`);
    }));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      guarded($("button[type=submit]", form), async () => {
        const res = await api("/api/settings/genai", {
          method: "POST",
          json: { key: form.key.value, model: form.model.disabled ? "" : form.model.value, remember: form.remember ? form.remember.checked : false },
        });
        me.genai = res.genai;
        paint(res.genai, res.models);
        toast("Saved. New games will use Purdue GenAI Studio.");
      });
    });
    const forget = $("#forgetKey", card);
    if (forget) forget.addEventListener("click", () => guarded(forget, async () => {
      const res = await api("/api/settings/genai/forget", { method: "POST" });
      me.genai = res.genai;
      paint(res.genai);
      toast("Key removed.");
    }));
  };
  paint(me.genai);
  const dc = $("#disconnect");
  if (dc) dc.addEventListener("click", () => guarded(dc, async () => { await api("/api/brightspace/disconnect", { method: "POST" }); route(); }));
}

/* -------------------------------------------------------------------- host */

const setup = { entries: [], types: "both", size: 20 };

async function hostView() {
  const { courses } = await api("/api/courses");
  setup.courses = courses;
  setup.entries = setup.entries.filter((e) => !e.key || courses.some((c) => c.key === e.key));
  main.innerHTML = `
    <section>
      <p class="eyebrow">Host a game</p>
      <h1>What are you<br>studying?</h1>
      <p class="lede">Pick one or more courses. For each, study the whole course, chosen modules, or specific topics. The deck is dealt evenly across every module and topic you pick.</p>
    </section>
    <section class="card step">
      <h2><span class="step-num">1</span>Courses</h2>
      ${courses.length ? `<div class="chips" id="courseChips">${courses.map((c) => `<button type="button" class="chip" data-key="${esc(c.key)}" aria-pressed="${setup.entries.some((e) => e.key === c.key)}"><span class="tick">${ICON.check}</span>${esc(c.name)}</button>`).join("")}</div>`
        : `<div class="notice">${ICON.info}<p class="small">No Brightspace courses connected. <a href="/auth/brightspace/start">${me.demo_mode ? "Load sample courses" : "Connect Brightspace"}</a>, or add a course below and use your own notes and files.</p></div>`}
      <form id="manualForm" class="row grow-first" novalidate>
        <input class="input" name="manual" maxlength="60" placeholder="Course not on Brightspace, e.g. PSY 1200" aria-label="Course name for your own material">
        <button class="btn line" type="submit">Add course</button>
      </form>
      <div id="blocks" class="grid"></div>
    </section>
    <section class="card step">
      <h2><span class="step-num">2</span>Cards</h2>
      <div class="spread">
        <div class="grid"><b>Question style</b>${segHTML("types", [["mcq", "Multiple choice"], ["fill_blank", "Fill in the blank"], ["both", "Mix"]], setup.types, "Question style")}</div>
        <div class="grid"><b>Starting deck</b><div class="stepper"><button type="button" data-step="-2" aria-label="Fewer cards">−</button><output id="sizeOut" aria-live="polite">${setup.size}</output><button type="button" data-step="2" aria-label="More cards">+</button></div></div>
      </div>
      <p class="hint">When the deck runs low, new questions are written automatically from the same sections.</p>
    </section>
    <section class="card step">
      <h2><span class="step-num">3</span>Start</h2>
      <label class="f">Your first name<input class="input" id="hostName" maxlength="24" autocomplete="given-name" value="${esc(me.name)}"></label>
      ${me.genai.configured
        ? `<div class="notice demo">${ICON.info}<p class="small">Cards will be written by <b>Purdue GenAI Studio</b> using <b>${esc(me.genai.model)}</b>. <a href="#/settings">Change in Settings</a></p></div>`
        : `<div class="notice">${ICON.info}<p class="small">${me.genai.offline_demo ? "Cards will be written by the simple offline demo writer. For real AI questions, " : "To write questions, "}<a href="#/settings">add your Purdue GenAI Studio key in Settings</a>.</p></div>`}
      <div id="permissions" class="grid"></div>
      <button class="btn gold big wide" id="createBtn" type="button">Create game</button>
    </section>`;

  $$("#courseChips .chip").forEach((chip) => chip.addEventListener("click", () => {
    const key = chip.dataset.key;
    const at = setup.entries.findIndex((e) => e.key === key);
    if (at >= 0) setup.entries.splice(at, 1);
    else setup.entries.push({ key, name: courses.find((c) => c.key === key).name, kind: "course", picked: new Set(), topics: "" });
    chip.setAttribute("aria-pressed", at < 0);
    renderBlocks();
  }));
  $("#manualForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = e.currentTarget.manual.value.trim();
    if (!name) return;
    setup.entries.push({ key: null, name, kind: "topic", picked: new Set(), topics: "" });
    e.currentTarget.manual.value = "";
    renderBlocks();
  });
  initSeg($('[data-seg="types"]'), (v) => { setup.types = v; });
  $$(".stepper button").forEach((b) => b.addEventListener("click", () => {
    setup.size = Math.max(6, Math.min(60, setup.size + Number(b.dataset.step)));
    $("#sizeOut").textContent = setup.size;
  }));
  $("#createBtn").addEventListener("click", (e) => guarded(e.currentTarget, createGame));
  renderBlocks();
}

async function renderBlocks() {
  const blocks = $("#blocks");
  if (!blocks) return;
  if (!setup.entries.length) { blocks.innerHTML = '<p class="muted small">Pick a course above to choose what to study in it.</p>'; renderPermissions(); return; }
  blocks.innerHTML = setup.entries.map((e, i) => `
    <div class="course-block" data-i="${i}">
      <header><div><span class="tag">${e.key ? "Brightspace course" : "Your material only"}</span><h3>${esc(e.name)}</h3></div>
        <button type="button" class="linkbtn small" data-remove="${i}">Remove</button></header>
      ${segHTML(`kind${i}`, e.key ? [["course", "Whole course"], ["module", "Modules"], ["topic", "Topics"]] : [["course", "Whole course"], ["topic", "Topics"]], e.kind, `What to study in ${e.name}`)}
      <div class="kind-body"></div>
    </div>`).join("");
  setup.entries.forEach((e, i) => {
    const block = $(`.course-block[data-i="${i}"]`, blocks);
    initSeg($(".seg", block), (v) => { e.kind = v; renderKind(e, block); });
    $("[data-remove]", block).addEventListener("click", () => {
      setup.entries.splice(i, 1);
      const chip = e.key && $(`#courseChips .chip[data-key="${e.key}"]`);
      if (chip) chip.setAttribute("aria-pressed", "false");
      renderBlocks();
    });
    renderKind(e, block);
  });
  renderPermissions();
}

async function renderKind(e, block) {
  const body = $(".kind-body", block);
  if (e.kind === "course") {
    body.innerHTML = `<p class="muted small">${e.key ? "Every module's files, plus anything players add." : "Whatever notes and files players add for this course."}</p>`;
  } else if (e.kind === "topic") {
    body.innerHTML = `<label class="f">Topics<input class="input" maxlength="200" placeholder="e.g. glycolysis, electron transport chain" value="${esc(e.topics)}"><span class="hint">Separate topics with commas. Each one becomes its own section of the deck.</span></label>`;
    $("input", body).addEventListener("input", (ev) => { e.topics = ev.target.value; });
  } else {
    body.innerHTML = '<p class="muted small">Loading modules…</p>';
    try {
      if (!e.modules) e.modules = (await api(`/api/courses/${encodeURIComponent(e.key)}/modules`)).modules;
      body.innerHTML = e.modules.length
        ? `<div class="chips">${e.modules.map((m) => `<button type="button" class="chip" data-mod="${esc(m.id)}" aria-pressed="${e.picked.has(m.id)}"><span class="tick">${ICON.check}</span>${esc(m.title)} <small>${m.files} file${m.files === 1 ? "" : "s"}</small></button>`).join("")}</div><p class="hint">Each module you pick becomes its own section of the deck.</p>`
        : '<p class="muted small">This course has no modules with readable files. Try the whole course or a topic.</p>';
      $$("[data-mod]", body).forEach((chip) => chip.addEventListener("click", () => {
        const id = chip.dataset.mod;
        if (e.picked.has(id)) e.picked.delete(id); else e.picked.add(id);
        chip.setAttribute("aria-pressed", e.picked.has(id));
      }));
    } catch (err) { body.innerHTML = `<p class="muted small">${esc(err.message)}</p>`; }
  }
}

function renderPermissions() {
  const box = $("#permissions");
  const bs = setup.entries.filter((e) => e.key);
  if (!bs.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="grid"><b>Course AI rules</b>${bs.map((e) => `
    <label class="check"><input type="checkbox" data-perm="${esc(e.key)}" ${e.permitted ? "checked" : ""}>
    <span>I checked the <b>${esc(e.name)}</b> syllabus: using AI tools to study its materials is allowed.</span></label>`).join("")}
    <p class="hint">Instructors set AI rules for each course. Course material is only sent to Purdue GenAI Studio, and only shared with players who are enrolled in the course.</p></div>`;
  $$("[data-perm]", box).forEach((cb) => cb.addEventListener("change", () => {
    setup.entries.find((e) => e.key === cb.dataset.perm).permitted = cb.checked;
  }));
}

async function createGame() {
  if (!setup.entries.length) throw new Error("Pick at least one course.");
  const strata = [];
  for (const e of setup.entries) {
    const base = e.key ? { course: e.key } : { manual_course: e.name };
    if (e.kind === "course") strata.push({ ...base, kind: "course" });
    else if (e.kind === "module") {
      if (!e.picked.size) throw new Error(`Pick at least one module in ${e.name}.`);
      e.picked.forEach((id) => strata.push({ ...base, kind: "module", modules: [id] }));
    } else {
      const topics = e.topics.split(",").map((t) => t.trim()).filter(Boolean);
      if (!topics.length) throw new Error(`Type at least one topic for ${e.name}.`);
      topics.forEach((topic) => strata.push({ ...base, kind: "topic", topic }));
    }
  }
  if (strata.length > 12) throw new Error("That's more than 12 sections. Pick fewer modules or topics.");
  const keyInput = $("#genaiKey");
  const { code } = await api("/api/sessions", {
    method: "POST",
    json: {
      name: $("#hostName").value,
      strata,
      types: setup.types === "both" ? ["mcq", "fill_blank"] : [setup.types],
      deck_size: setup.size,
      ai_permission_courses: setup.entries.filter((e) => e.key && e.permitted).map((e) => e.key),
      genai_key: keyInput ? keyInput.value : "",
    },
  });
  if (keyInput) keyInput.value = "";
  location.hash = `#/game/${code}`;
}

/* -------------------------------------------------------------------- game */

let game = null;
const painted = new Map();

function patch(id, html, bind) {
  const el = document.getElementById(id);
  if (!el || painted.get(id) === html) return;
  painted.set(id, html);
  el.innerHTML = html;
  if (bind) bind(el);
}

async function gameView(code) {
  painted.clear();
  game = { code, status: null };
  const tick = async () => {
    try { render(await api(`/api/sessions/${code}`)); }
    catch (e) {
      stopPolling();
      main.innerHTML = `<section class="card"><h2>Game unavailable</h2><p class="muted">${esc(e.message)}</p>
        <div class="row"><a class="btn dark" href="#/join/${esc(code)}">Join this game</a><a class="btn line" href="#/">Back to start</a></div></section>`;
    }
  };
  await tick();
  const timer = setInterval(() => { if (!document.hidden) tick(); }, 1500);
  stopPolling = () => clearInterval(timer);
}

function render(s) {
  game.state = s;
  const phase = s.status === "ready" ? "play" : s.status;
  if (game.phase !== phase) {
    game.phase = phase;
    painted.clear();
    main.innerHTML = phase === "play"
      ? `<div id="r-head"></div><div class="grid g-side"><div class="grid"><div id="r-card"></div><div id="r-notices"></div></div>
          <div class="grid"><div id="r-host"></div><div id="r-players"></div><details class="card tight" id="r-more"><summary><b>Add more material</b></summary><div id="r-upload"></div></details><div id="r-leave"></div></div></div>`
      : `<div id="r-head"></div><div id="r-notices"></div><div class="grid g-side"><div class="grid"><div id="r-sections"></div><div id="r-upload"></div></div>
          <div class="grid"><div id="r-host"></div><div id="r-players"></div><div id="r-leave"></div></div></div>`;
  }
  patch("r-head", headHTML(s));
  patch("r-notices", noticesHTML(s));
  patch("r-sections", sectionsHTML(s));
  patch("r-players", playersHTML(s));
  patch("r-host", hostHTML(s), bindHost);
  patch("r-upload", uploadHTML(s), bindUpload);
  patch("r-card", cardHTML(s), bindCard);
  patch("r-leave", `<button class="btn line wide" type="button" id="leaveBtn">${s.role === "host" ? "End game for everyone" : "Leave game"}</button>`, bindLeave);
}

function headHTML(s) {
  const status = { lobby: "Gathering material", building: "Building the deck", ready: "In play" }[s.status] || s.status;
  return `<section class="card lead">
    <div class="spread">
      <div><p class="eyebrow">Join code</p><div class="code" aria-label="Join code ${esc(s.code.split("").join(" "))}">${esc(s.code)}</div></div>
      <div class="grid end-items">
        <span class="pill ink">${esc(status)}</span>
        <span class="small muted">${esc(s.players.length)} player${s.players.length === 1 ? "" : "s"} · ${esc(s.types.map((t) => (t === "mcq" ? "multiple choice" : "fill in the blank")).join(" + "))}</span>
      </div>
    </div>
    <p class="muted small">Players open this site, choose <b>Join a game</b> and type the code.</p>
  </section>`;
}

function noticesHTML(s) {
  const held = s.held_back.map((h) => `<div class="notice">${ICON.alert}<p class="small"><b>${esc(h.course)}</b> Brightspace material is held back until ${esc(h.waiting_on.join(", "))} ${h.waiting_on.length === 1 ? "connects" : "connect"} Brightspace and ${h.waiting_on.length === 1 ? "is" : "are"} confirmed as enrolled. Uploaded notes still count.</p></div>`);
  const notes = s.notices.map((n) => `<div class="notice">${ICON.info}<p class="small">${esc(n)}</p></div>`);
  return held.concat(notes).join("") ? `<div class="grid">${held.concat(notes).join("")}</div>` : "";
}

function sectionsHTML(s) {
  return `<section class="card">
    <div class="card-head"><div><p class="eyebrow">The deck is dealt evenly across</p><h2>${s.strata.length} section${s.strata.length === 1 ? "" : "s"}</h2></div></div>
    <ul class="sections">${s.strata.map((st) => {
      const c = st.counts;
      const tag = (n, word) => `<span class="${n ? "" : "zero"}">${n} ${word}</span>`;
      return `<li><b>${esc(st.label)}</b><div class="counts">${tag(c.brightspace, "from Brightspace")}${tag(c.upload, "from files")}${tag(c.notes, "from notes")}</div></li>`;
    }).join("")}</ul>
    <p class="hint">Counts are passages of material. Brightspace material is read when the host builds the deck.</p>
  </section>`;
}

function playersHTML(s) {
  const play = s.status === "ready";
  return `<section class="card">
    <p class="eyebrow">${play ? "Scoreboard" : "Players"}</p>
    <ul class="players">${s.players.slice().sort((a, b) => (play ? b.score - a.score : 0)).map((p) => {
      let note = p.host ? "Host" : "Player";
      if (!play && s.has_brightspace) note += p.missing.length ? ` · not verified for ${p.missing.join(", ")}` : " · verified for every course";
      if (!play && p.uploads) note += ` · ${p.uploads} file${p.uploads === 1 ? "" : "s"}`;
      if (play && s.card) note = p.answered_current ? (p.correct_current ? "Got this one" : "Missed this one") : "Thinking…";
      return `<li class="${p.me ? "me" : ""}"><span class="avatar" aria-hidden="true">${esc(p.name.slice(0, 1).toUpperCase())}</span>
        <span><b>${esc(p.name)}${p.me ? " (you)" : ""}</b><small>${esc(note)}</small></span>
        ${play ? `<span class="score" aria-label="${esc(p.score)} points">${esc(p.score)}</span>` : (!s.has_brightspace ? "" : p.missing.length ? '<span class="dot" title="Not verified"></span>' : '<span class="dot on" title="Verified"></span>')}</li>`;
    }).join("")}</ul>
  </section>`;
}

function hostHTML(s) {
  if (s.status === "building") {
    return `<section class="card ink"><p class="eyebrow">Building</p><h2>Dealing the deck</h2>
      <div class="dealing" aria-hidden="true"><i></i><i></i><i></i></div>
      <p class="muted small" role="status">${esc(s.progress || "Working…")}</p></section>`;
  }
  if (s.status === "ready") {
    const pct = s.deck.total ? Math.round(((s.deck.total - s.deck.remaining) / s.deck.total) * 100) : 0;
    return `<section class="card ${s.role === "host" ? "lead" : ""}">
      <div class="spread"><div><p class="eyebrow">Deck</p><h2>${esc(s.deck.remaining)} left</h2></div>${s.deck.refilling ? '<span class="pill">Writing more…</span>' : ""}</div>
      <div class="meter" data-pct="${pct}"><i></i></div>
      <p class="small muted">${esc(s.deck.total)} written so far by ${esc(s.generator)}. ${s.deck.refilling ? "New cards are on the way." : "Refills automatically when it runs low."}</p>
      ${s.role === "host" ? `<button class="btn dark big wide" type="button" id="drawBtn">${s.card ? "Draw next card" : "Draw first card"}</button>` : '<p class="small muted">The host draws each card.</p>'}
    </section>`;
  }
  const mine = s.players.find((p) => p.me);
  let share = "";
  if (s.has_brightspace && !me.brightspace.connected) {
    share = `<div class="grid"><p class="small muted">Connect Brightspace so the game can confirm you're enrolled. Until everyone is, that course's Brightspace material stays out of the deck.</p>
      <a class="btn line" href="/auth/brightspace/start?next=${encodeURIComponent(`/#/game/${s.code}`)}">${me.demo_mode ? "Load sample courses" : "Connect Brightspace"}</a></div>`;
  } else if (s.has_brightspace) {
    share = `<label class="switch"><span><b>Use my Brightspace material</b><span class="hint">Only for courses every player is enrolled in.</span></span>
      <input type="checkbox" id="shareToggle" ${mine && mine.share_brightspace ? "checked" : ""}><span class="track" aria-hidden="true"></span></label>`;
  }
  if (s.role !== "host") return `<section class="card"><p class="eyebrow">Waiting</p><h2>For the host</h2><p class="muted small">Add notes or files while you wait. The host builds the deck when everyone's ready.</p>${share}</section>`;
  return `<section class="card lead"><p class="eyebrow">When everyone's in</p><h2>Build the deck</h2>
    <p class="muted small">Starts with ${esc(s.deck_size)} cards from ${esc(s.generator)}, dealt across every section.</p>
    ${share}<button class="btn dark big wide" type="button" id="buildBtn">Build deck</button></section>`;
}

function bindHost(el) {
  const meter = $(".meter", el);
  if (meter) $("i", meter).style.width = `${meter.dataset.pct}%`;
  const build = $("#buildBtn", el);
  if (build) build.addEventListener("click", () => guarded(build, async () => render(await api(`/api/sessions/${game.code}/build`, { method: "POST" }))));
  const draw = $("#drawBtn", el);
  if (draw) draw.addEventListener("click", () => guarded(draw, async () => render(await api(`/api/sessions/${game.code}/draw`, { method: "POST" }))));
  const share = $("#shareToggle", el);
  if (share) share.addEventListener("change", () => guarded(null, async () => render(await api(`/api/sessions/${game.code}/share`, { method: "POST", json: { share: share.checked } }))));
}

function uploadHTML(s) {
  if (s.status === "building") return "";
  const inPlay = s.status === "ready";
  return `${inPlay ? "" : '<section class="card"><p class="eyebrow">Add material</p><h2>Your notes &amp; files</h2>'}
    <form id="uploadForm" class="grid${inPlay ? " pad-top" : ""}" novalidate>
      <label class="f">Section<select class="input" name="stratum">${s.strata.map((st) => `<option value="${esc(st.id)}">${esc(st.label)}</option>`).join("")}</select></label>
      <label class="drop" id="drop">${ICON.file}<b>Drop files or browse</b><span class="hint">PDF, Word, PowerPoint, text. Up to 15 MB each.</span>
        <input type="file" name="files" multiple accept=".pdf,.docx,.pptx,.txt,.md,.html,.htm"></label>
      <ul class="files" id="fileList"></ul>
      <label class="f">Or paste notes<textarea class="input" name="notes" maxlength="60000" placeholder="Paste your own study notes"></textarea></label>
      <label class="check"><input type="checkbox" name="attest" value="yes"><span>This is my own work or course material I'm allowed to share with these players. No exams, quizzes, answer keys or other students' personal information.</span></label>
      <button class="btn gold wide" type="submit">Add to the deck</button>
    </form>${inPlay ? "" : "</section>"}`;
}

function bindUpload(el) {
  const form = $("#uploadForm", el);
  if (!form) return;
  const input = $("input[type=file]", form);
  const drop = $("#drop", form);
  const list = $("#fileList", form);
  const show = () => { list.innerHTML = [...input.files].map((f) => `<li>${esc(f.name)} <span class="muted">· ${(f.size / 1048576).toFixed(1)} MB</span></li>`).join(""); };
  input.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { input.files = e.dataTransfer.files; show(); });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    guarded($("button[type=submit]", form), async () => {
      if (!form.attest.checked) throw new Error("Confirm you're allowed to share this material.");
      const res = await api(`/api/sessions/${game.code}/upload`, { method: "POST", form: new FormData(form) });
      form.reset(); list.innerHTML = "";
      const what = `Added ${res.added} passage${res.added === 1 ? "" : "s"} of material.`;
      toast(res.skipped.length ? `${what} Skipped: ${res.skipped.join("; ")}` : what, res.skipped.length > 0);
      render(res.state);
    });
  });
}

function promptHTML(text) {
  return esc(text).replace("_____", '<span class="blank" aria-label="blank"></span>');
}

function cardHTML(s) {
  if (!s.card) {
    const msg = s.deck.remaining || !s.deck.refilling ? (s.role === "host" ? "Draw the first card when your board is set up." : "Waiting for the host to draw a card.") : "The deck ran out. New cards are being written…";
    return `<section class="card empty-card">${ICON.cards}<h2>Ready to play</h2><p class="muted">${esc(msg)}</p></section>`;
  }
  const c = s.card, mine = s.my_answer;
  let body;
  if (c.type === "mcq") {
    body = `<div class="options">${c.options.map((o, i) => {
      let cls = "";
      if (mine) { if (o === mine.expected) cls = "right"; else if (o === mine.given) cls = "wrong"; }
      return `<button type="button" class="opt ${cls}" data-opt="${i}" ${mine ? "disabled" : ""}><span class="letter">${"ABCDE"[i]}</span><span>${esc(o)}</span></button>`;
    }).join("")}</div>`;
  } else {
    body = mine ? "" : `<form class="dock" id="blankForm" novalidate><input name="answer" maxlength="80" autocomplete="off" spellcheck="false" placeholder="Type the missing word" aria-label="Your answer">
      <button class="send" type="submit" aria-label="Submit answer">${ICON.up}</button></form>`;
  }
  const result = mine ? `<div class="result ${mine.correct ? "ok" : "no"}" role="status"><b>${mine.correct ? "Correct" : "Not quite"}</b>
      ${mine.correct ? "" : `<p>Answer: <b>${esc(mine.expected)}</b>${c.type === "fill_blank" ? ` · you wrote “${esc(mine.given)}”` : ""}</p>`}
      ${mine.explanation ? `<p class="small">${esc(mine.explanation)}</p>` : ""}</div>` : "";
  return `<article class="qcard" aria-label="Question card ${esc(c.number)}">
    <div class="strip"><b>${esc(c.label)}</b><span class="small">Card ${esc(c.number)} · ${c.type === "mcq" ? "Multiple choice" : "Fill in the blank"}</span></div>
    <div class="body"><p class="prompt">${promptHTML(c.prompt)}</p>${body}${result}</div>
  </article>`;
}

function bindCard(el) {
  const s = game.state;
  if (!s.card) return;
  const submit = (answer) => api(`/api/sessions/${game.code}/answer`, { method: "POST", json: { question_id: s.card.id, answer } }).then(render);
  $$("[data-opt]", el).forEach((b) => b.addEventListener("click", () => {
    b.classList.add("picked");
    guarded(null, () => submit(Number(b.dataset.opt)));
  }));
  const form = $("#blankForm", el);
  if (form) {
    form.answer.focus({ preventScroll: true });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!form.answer.value.trim()) return;
      $(".send", form).classList.add("go");
      guarded($(".send", form), () => submit(form.answer.value));
    });
  }
}

function bindLeave(el) {
  const btn = $("#leaveBtn", el);
  btn.addEventListener("click", () => {
    const host = game.state.role === "host";
    if (!confirm(host ? "End this game for everyone? All material and cards are deleted." : "Leave this game? Your uploaded material leaves with you.")) return;
    guarded(btn, async () => {
      await api(`/api/sessions/${game.code}/${host ? "end" : "leave"}`, { method: "POST" });
      location.hash = "#/";
    });
  });
}

route();
