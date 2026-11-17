/* Study Deck front end. No framework; every piece of server text goes through esc(). */
"use strict";

const main = document.getElementById("main");
const topRight = document.getElementById("topRight");
const toastEl = document.getElementById("toast");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ICON = {
  plus: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  chev: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
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
  topRight.innerHTML = `${bs.available ? `<span class="pill"><span class="dot ${bs.connected ? "on" : ""}"></span>${bs.connected ? `Brightspace · ${bs.courses} course${bs.courses === 1 ? "" : "s"}` : "Brightspace not connected"}</span>` : ""}
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
  if (hash === "#signin-unavailable") { history.replaceState(null, "", "#/"); toast("Brightspace isn't set up on this server. Add your own notes and files instead.", true); }
  if (hash === "#signin-join-first") { history.replaceState(null, "", "#/"); toast("Join a game first, then connect Brightspace from the game page.", true); }
  if (hash === "#signin-busy") { history.replaceState(null, "", "#/"); toast("Too many people are signing in right now. Try again in a minute.", true); }
  if (hash === "#signin-local-only") { history.replaceState(null, "", "#/"); toast("Brightspace can only be connected on the computer running Study Deck.", true); }
  try {
    await loadMe();
    watchSignIn();
    const parts = (location.hash || "#/").slice(2).split("/");
    if (parts[0] === "host") await hostView();
    else if (parts[0] === "settings") settingsView();
    else if (parts[0] === "game" && parts[1]) await gameView(parts[1].toUpperCase());
    else homeView(parts[0] === "join" ? (parts[1] || "") : "");
  } catch (e) {
    main.innerHTML = `<section class="card"><h2>Something went wrong</h2><p class="muted">${esc(e.message)}</p><div class="btn-row"><a class="btn dark" href="#/">Back to start</a></div></section>`;
  }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

/* Personal sign-in happens in a browser window the app opens itself (automated, so
   navigator.webdriver is true there). Your own browser tabs work too, once signed in. */
const IN_APP_WINDOW = navigator.webdriver === true;

/* Personal sign-in: a browser window on this computer is open on Purdue's page. */
let signInTimer = null;
function watchSignIn() {
  if (!me.brightspace.signing_in || signInTimer) return;
  toast(me.brightspace.signin_on_host
    ? "A Purdue sign-in window with your name opened on the computer running Study Deck. Sign in there; this page connects by itself."
    : IN_APP_WINDOW ? "Finish signing in to Purdue in the new tab. It closes by itself when you're done."
    : "Sign in to Purdue in the Study Deck window that opened (it may be behind this one). This page connects by itself.");
  signInTimer = setInterval(async () => {
    try { await loadMe(); } catch { return; }
    if (me.brightspace.signing_in) return;
    clearInterval(signInTimer); signInTimer = null;
    try { window.focus(); } catch {}
    if (me.brightspace.connected) toast(`Brightspace connected · ${me.brightspace.courses} courses.`);
    else toast(me.brightspace.error || "Brightspace sign-in didn't finish. Try again.", true);
    route();
  }, 2000);
}

/* -------------------------------------------------------------------- home */

/* Small Brightspace status bar on the home page; the details live in Settings. */
function connectCard() {
  const bs = me.brightspace;
  if (!bs.available) return "";
  const bar = (dot, title, sub, action) => `<section class="card tight bs-card">
    <div class="bs-line"><span class="dot ${dot}" aria-hidden="true"></span>
      <div class="bs-text"><b>${title}</b>${sub ? `<span class="hint">${sub}</span>` : ""}</div>${action}</div>
  </section>`;
  if (bs.connected) {
    return bar("on", "Brightspace connected", `${esc(bs.courses)} course${bs.courses === 1 ? "" : "s"}`,
      '<button class="btn line sm" id="disconnect" type="button">Disconnect</button>');
  }
  if (bs.signing_in) {
    return bar("wait", "Signing in to Brightspace…", bs.signin_on_host ? "Finish on the computer running Study Deck"
      : IN_APP_WINDOW ? "Finish in the Purdue tab" : "Finish in the Study Deck window", "");
  }
  if (bs.signin_on_host) {
    return bar("", "Brightspace not connected", "Join a game first, then connect from the game page", "");
  }
  if (!bs.can_connect) return bar("", "Brightspace not connected", "It can only be connected on the computer running Study Deck", "");
  return bar("", "Brightspace not connected", "",
    '<a class="btn dark sm" href="/auth/brightspace/start">Connect</a>');
}

/* On the computer running Study Deck, say where everyone else should go. */
function inviteNotice() {
  if (!me.on_network || me.join_base === location.origin) return "";
  return `<div class="notice invite">${ICON.info}<p class="small">Players on the same Wi-Fi open <b class="url">${esc(me.join_base)}</b> on their phone or laptop, or scan the QR code once you start a game.</p></div>`;
}

function homeView(prefill) {
  main.innerHTML = `
    <section>
      <p class="eyebrow">Board game companion</p>
      <h1>Study<br>Deck</h1>
      <p class="lede">Pick what you're studying, pool course material and notes with your group, and play with a deck of questions that keeps refilling itself.</p>
    </section>
    ${inviteNotice()}
    <div class="grid g2">
      <div class="grid">
        <section class="card lead">
          <p class="eyebrow">Host</p><h2>Start a game</h2>
          <p class="muted">Choose courses, modules or topics, pick the question style, and share the join code with your table.</p>
          <a class="btn dark big wide" href="#/host">Host a game</a>
        </section>
        ${connectCard()}
      </div>
      <section class="card">
        <p class="eyebrow">Player</p><h2>Join a game</h2>
        <form id="joinForm" class="grid" novalidate>
          <label class="f">Join code<input class="input code-input" name="code" maxlength="6" autocomplete="off" autocapitalize="characters" spellcheck="false" value="${esc(prefill.toUpperCase())}" required></label>
          <label class="f">Your first name<input class="input" name="name" maxlength="24" autocomplete="given-name" value="${esc(me.name)}" required></label>
          <button class="btn gold wide" type="submit">Join</button>
        </form>
      </section>
    </div>`;
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
  // Arrived from a join link or QR code: the code is filled in, so go straight to the name.
  if (prefill) { const n = $("#joinForm").name; if (!n.value) n.focus(); }
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

const setup = { entries: [], types: "both", size: 20, boilerexams: true };

// Boilerexams courses, so matching courses can be marked; fetched once, in the background.
let boilerexams = null;
function loadBoilerexams() {
  if (boilerexams && !boilerexams.unreachable) return;
  boilerexams = { enabled: false, courses: [], loading: true };
  api("/api/boilerexams").then((r) => { boilerexams = r; renderBlocks(); }).catch(() => {
    boilerexams = { enabled: true, courses: [], unreachable: true };
    renderStyleOption();
  });
}
// Same rule as the server: "MA 16100" or "MA 161" -> "MA16100".
function courseCodes(name) {
  return [...name.toUpperCase().matchAll(/\b([A-Z]{2,5})\s?(\d{3,5})\b/g)]
    .map(([, s, n]) => (n.length === 3 ? `${s}${n}00` : n.length === 5 ? `${s}${n}` : null)).filter(Boolean);
}
const spacedCode = (code) => code.replace(/^([A-Z]+)/, "$1 ");
function onBoilerexams(name) {
  return boilerexams && boilerexams.courses.length ? courseCodes(name).find((c) => boilerexams.courses.includes(c)) || null : null;
}
function renderStyleOption() {
  const el = $("#styleOption");
  if (!el) return;
  if (setup.entries.some((e) => courseCodes(e.name).length)) {
    if (!boilerexams || boilerexams.loading) {
      el.innerHTML = '<p class="hint" role="status">Checking for past exam questions on Boilerexams…</p>';
      return;
    }
    if (boilerexams.unreachable) {
      el.innerHTML = '<p class="hint" role="status">Could not check Boilerexams right now. Your course is still added. <button type="button" class="linkbtn" id="retryBoilerexams">Try again</button></p>';
      $("#retryBoilerexams", el).addEventListener("click", () => { loadBoilerexams(); renderStyleOption(); });
      return;
    }
  }
  // Only offered when a chosen course is on Boilerexams; otherwise nothing is said about it.
  const matched = boilerexams && boilerexams.enabled
    ? [...new Set(setup.entries.map((e) => onBoilerexams(e.name)).filter(Boolean))] : [];
  if (!matched.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<label class="check"><input type="checkbox" id="bxIn" ${setup.boilerexams ? "checked" : ""}>
    <span><b>Use past exam questions</b> · ${esc(matched.map(spacedCode).join(", "))} ${matched.length === 1 ? "is" : "are"} on Boilerexams. Multiple-choice decks include real past exam questions. The AI uses your material when available; otherwise it creates new practice from matching past exam questions and answers. Text-only questions.</span></label>`;
  $("#bxIn", el).addEventListener("change", (ev) => { setup.boilerexams = ev.target.checked; renderBlocks(); });
}

async function hostView() {
  Object.assign(setup, { entries: [], types: "both", size: 20, boilerexams: true });
  const { courses } = await api("/api/courses");
  setup.courses = courses;
  loadBoilerexams();
  main.innerHTML = `
    <section>
      <p class="eyebrow">Host a game</p>
      <h1>What are you<br>studying?</h1>
      <p class="lede">Pick a Brightspace course or add a course below. If it is on Boilerexams, past exam questions can supply the material without notes or uploads. Study a whole course or focus on topics.</p>
    </section>
    <section class="card step">
      <h2><span class="step-num">1</span>Courses</h2>
      ${courses.length ? `<div class="chips" id="courseChips">${courses.map((c) => `<button type="button" class="chip" data-key="${esc(c.key)}" aria-pressed="${setup.entries.some((e) => e.key === c.key)}"><span class="tick">${ICON.check}</span>${esc(c.name)}</button>`).join("")}</div>`
        : me.brightspace.can_connect
          ? `<div class="notice">${ICON.info}<p class="small">No Brightspace courses connected. Add a course below to use available past exams or your own material, or <a href="/auth/brightspace/start">connect Brightspace</a>.</p></div>`
          : `<p class="muted small">Add your course code. Available Boilerexams questions can supply the material, and everyone can add notes and files.</p>`}
      <form id="manualForm" class="row grow-first" novalidate>
        <input class="input" name="manual" maxlength="60" placeholder="Course name or code, e.g. PHYS 17200" aria-label="Course name or code">
        <button class="btn line" type="submit">Add course</button>
      </form>
      <div id="blocks" class="grid"></div>
    </section>
    <section class="card step">
      <h2><span class="step-num">2</span>Cards</h2>
      <div class="spread">
        <div class="grid"><b>Question style</b>${segHTML("types", [["mcq", "Multiple choice"], ["fill_blank", "Fill in the blank"], ["both", "Mix"]], setup.types, "Question style")}</div>
        <div class="grid"><label for="sizeIn"><b>Starting deck</b></label><div class="stepper"><button type="button" data-step="-1" aria-label="Fewer cards">−</button><input id="sizeIn" type="number" inputmode="numeric" min="1" max="${esc(me.max_deck_size)}" step="1" value="${esc(setup.size)}" aria-describedby="sizeHint"><button type="button" data-step="1" aria-label="More cards">+</button></div><span class="hint" id="sizeHint">Type any number of cards, 1 to ${esc(me.max_deck_size)}.</span></div>
      </div>
      <p class="hint">When the deck runs low, new questions are written automatically from the same sections.</p>
      <div id="styleOption"></div>
    </section>
    <section class="card step">
      <h2><span class="step-num">3</span>Start</h2>
      <label class="f">Your first name<input class="input" id="hostName" maxlength="24" autocomplete="given-name" value="${esc(me.name)}"></label>
      ${me.genai.configured
        ? `<div class="notice good">${ICON.info}<p class="small">Cards will be written by <b>Purdue GenAI Studio</b> using <b>${esc(me.genai.model)}</b>. <a href="#/settings">Change in Settings</a></p></div>`
        : `<div class="notice">${ICON.alert}<p class="small">Cards are written by Purdue GenAI Studio. <a href="#/settings">Add your API key in Settings</a> before you create the game.</p></div>`}
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
    setup.entries.push({ key: null, name, kind: "course", picked: new Set(), topics: "" });
    e.currentTarget.manual.value = "";
    loadBoilerexams();
    renderBlocks();
  });
  initSeg($('[data-seg="types"]'), (v) => { setup.types = v; });
  const sizeIn = $("#sizeIn");
  const setSize = (n) => {
    setup.size = Math.max(1, Math.min(me.max_deck_size, Math.round(Number(n)) || 1));
    sizeIn.value = setup.size;
  };
  $$(".stepper button").forEach((b) => b.addEventListener("click", () => setSize(setup.size + Number(b.dataset.step))));
  sizeIn.addEventListener("input", () => { const n = parseInt(sizeIn.value, 10); if (n >= 1 && n <= me.max_deck_size) setup.size = n; });
  sizeIn.addEventListener("change", () => setSize(sizeIn.value));
  $("#createBtn").addEventListener("click", (e) => guarded(e.currentTarget, createGame));
  renderBlocks();
}

async function renderBlocks() {
  const blocks = $("#blocks");
  if (!blocks) return;
  renderStyleOption();
  if (!setup.entries.length) { blocks.innerHTML = `<p class="muted small">${setup.courses && setup.courses.length ? "Pick" : "Add"} a course above to choose what to study in it.</p>`; renderPermissions(); return; }
  blocks.innerHTML = setup.entries.map((e, i) => `
    <div class="course-block" data-i="${i}">
      <header><div><span class="tag">${e.key ? "Brightspace course" : setup.boilerexams && onBoilerexams(e.name) ? "Boilerexams + optional uploads" : "Your material only"}</span>${onBoilerexams(e.name) ? ' <span class="tag bx">On Boilerexams</span>' : ""}<h3>${esc(e.name)}</h3></div>
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
    body.innerHTML = `<p class="muted small">${e.key ? "Files from every folder, at any depth, plus anything players add. If the course has more files than a deck reads at once, the AI picks a spread across its folders." : setup.boilerexams && onBoilerexams(e.name) ? "Build from past exam questions and new AI practice based on their concepts and correct answers. Notes and files are optional; when added, they supply the AI's material instead." : "Whatever notes and files players add for this course."}</p>`;
  } else if (e.kind === "topic") {
    body.innerHTML = `<label class="f">Topics<input class="input" maxlength="200" placeholder="e.g. glycolysis, electron transport chain" value="${esc(e.topics)}"><span class="hint">Separate topics with commas. Each one becomes its own section of the deck.${e.key ? " The AI looks through every folder, however deep, and picks the files that match your topics. It only sees folder and file titles to do that." : ""}</span></label>`;
    $("input", body).addEventListener("input", (ev) => { e.topics = ev.target.value; });
  } else {
    body.innerHTML = '<p class="muted small">Loading modules…</p>';
    try {
      if (!e.modules) e.modules = (await api(`/api/courses/${encodeURIComponent(e.key)}/modules`)).modules;
      renderTree(e, body);
    } catch (err) { body.innerHTML = `<p class="muted small">${esc(err.message)}</p>`; }
  }
}

/* Folder picker. A picked folder includes every folder inside it, so those show as
   ticked ("included") and can't be picked separately, which would only repeat material. */
function renderTree(e, body, focusSel) {
  if (!e.modules.length) {
    body.innerHTML = '<p class="muted small">This course has no folders with readable files. Try the whole course or a topic.</p>';
    return;
  }
  const byId = new Map(e.modules.map((m) => [m.id, m]));
  const kids = new Map();
  e.modules.forEach((m) => {
    const p = m.parent && byId.has(m.parent) ? m.parent : "";
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(m);
  });
  const nested = e.modules.some((m) => kids.has(m.id));
  // Each top folder is a card with its folders folded away, so the list stays short.
  // Folders holding a pick start open so the pick stays in view.
  if (!e.open) {
    e.open = new Set();
    e.picked.forEach((id) => { for (let p = byId.get(byId.get(id)?.parent); p; p = byId.get(p.parent)) e.open.add(p.id); });
  }
  const pickedAncestor = (m) => {
    for (let p = byId.get(m.parent); p; p = byId.get(p.parent)) if (e.picked.has(p.id)) return p;
    return null;
  };
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const stateOf = (m) => e.picked.has(m.id) ? "on" : pickedAncestor(m) ? "included" : "off";
  const pick = (m, state, folderCount) => `
    <button type="button" class="fpick" data-mod="${esc(m.id)}" aria-pressed="${state !== "off"}"${state === "included" ? ' aria-disabled="true"' : ""} title="${esc(m.label || m.title)}">
      <span class="tick">${ICON.check}</span>
      <span class="ftext"><span class="fname">${esc(m.title)}</span>
        <span class="fmeta">${state === "included" ? "<b>Included</b> · " : ""}${plural(m.files, "file")}${folderCount ? ` · ${plural(folderCount, "folder")}` : ""}</span></span>
    </button>`;
  const sub = (m) => {
    const children = kids.get(m.id) || [];
    const open = e.open.has(m.id);
    const state = stateOf(m);
    return `<li class="fsub" data-state="${state}">
      <div class="fsub-row">${pick(m, state, children.length)}
        ${children.length ? `<button type="button" class="fmini" data-twist="${esc(m.id)}" aria-expanded="${open}" aria-label="${open ? "Hide" : "Show"} the folders in ${esc(m.title)}">${ICON.chev}</button>` : ""}
      </div>
      ${children.length && open ? `<ul class="fsubs">${children.map(sub).join("")}</ul>` : ""}
    </li>`;
  };
  const card = (m) => {
    const children = kids.get(m.id) || [];
    const open = e.open.has(m.id);
    const state = stateOf(m);
    const count = plural(children.length, "folder");
    return `<li class="fcard" data-state="${state}">
      <div class="fcard-head">${pick(m, state, 0)}
        ${children.length ? `<button type="button" class="fmore" data-twist="${esc(m.id)}" aria-expanded="${open}" aria-label="${open ? "Hide the folders" : `Show ${count}`} in ${esc(m.title)}"><span>${open ? "Hide" : count}</span>${ICON.chev}</button>` : ""}
      </div>
      ${children.length && open ? `<ul class="fsubs">${children.map(sub).join("")}</ul>` : ""}
    </li>`;
  };
  const n = e.picked.size;
  body.innerHTML = `<ul class="fcards" aria-label="Folders in ${esc(e.name)}">${(kids.get("") || []).map(card).join("")}</ul>
    <p class="hint">${n ? `<b>${plural(n, "folder")} picked</b>, so ${plural(n, "section")} in the deck. ` : ""}${nested ? "Picking a folder includes every folder inside it; to choose inside it instead, open its folders and pick those. " : ""}If your picks hold more files than a deck reads at once, the AI chooses the most useful ones.</p>`;
  $$("[data-twist]", body).forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.twist;
    if (e.open.has(id)) e.open.delete(id); else e.open.add(id);
    renderTree(e, body, `[data-twist="${CSS.escape(id)}"]`);
  }));
  $$("[data-mod]", body).forEach((b) => b.addEventListener("click", () => {
    const m = byId.get(b.dataset.mod);
    const holder = pickedAncestor(m);
    if (holder) {
      toast(`Already included in “${holder.title}”. Untick it to pick the folders inside it one by one.`);
      return;
    }
    if (e.picked.has(m.id)) e.picked.delete(m.id);
    else {
      e.picked.add(m.id);
      // Folders inside it are now included, so they aren't separate sections any more.
      const inside = (id) => (kids.get(id) || []).forEach((c) => { e.picked.delete(c.id); inside(c.id); });
      inside(m.id);
    }
    renderTree(e, body, `[data-mod="${CSS.escape(m.id)}"]`);
  }));
  if (focusSel) { const el = $(focusSel, body); if (el) el.focus(); }
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
      boilerexams: setup.boilerexams,
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
      const removed = /removed you/.test(e.message); // the host removed them: rejoining isn't possible
      main.innerHTML = `<section class="card"><h2>${removed ? "You were removed" : "Game unavailable"}</h2><p class="muted">${esc(e.message)}</p>
        <div class="btn-row">${removed ? "" : `<a class="btn dark" href="#/join/${esc(code)}">Join this game</a>`}<a class="btn ${removed ? "dark" : "line"}" href="#/">Back to start</a></div></section>`;
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
    // Two columns, balanced so neither runs much longer than the other.
    main.innerHTML = phase === "play"
      ? `<div id="r-head"></div><div class="grid g-side eq">
          <div class="col"><div id="r-card"></div><div id="r-notices"></div><div id="r-picked"></div>
            <details class="more" id="r-more"><summary class="btn line wide more-btn">${ICON.plus}<span>Add more material</span></summary><div class="card tight" id="r-upload"></div></details></div>
          <div class="col"><div id="r-host"></div><div id="r-players"></div><div id="r-leave"></div></div></div>`
      : `<div id="r-head"></div><div id="r-notices"></div><div class="grid g-side eq">
          <div class="col"><div id="r-upload"></div></div>
          <div class="col"><div id="r-host"></div><div id="r-sections"></div><div id="r-players"></div><div id="r-leave"></div></div></div>`;
  }
  patch("r-head", headHTML(s));
  patch("r-notices", noticesHTML(s));
  patch("r-sections", sectionsHTML(s));
  patch("r-players", playersHTML(s), bindPlayers);
  patch("r-picked", (s.picked && s.picked.length ? `<section class="card tight"><p class="eyebrow">Brightspace files in this deck</p>${s.picked.map(pickedHTML).join("")}</section>` : "") + styleHTML(s));
  patch("r-host", hostHTML(s), bindHost);
  patch("r-upload", uploadHTML(s), bindUpload);
  patch("r-card", cardHTML(s), bindCard);
  patch("r-leave", `<button class="btn line wide" type="button" id="leaveBtn">${s.role === "host" ? "End game for everyone" : "Leave game"}</button>`, bindLeave);
}

function headHTML(s) {
  const status = { lobby: "Gathering material", building: "Building the deck", ready: "In play" }[s.status] || s.status;
  const site = me.join_base.replace(/^https?:\/\//, "");
  const lobby = s.status !== "ready";
  const info = `<section class="card lead head-main">
    <div class="grid">
      <div><p class="eyebrow">Join code</p><div class="code" aria-label="Join code ${esc(s.code.split("").join(" "))}">${esc(s.code)}</div></div>
      <div class="grid start-items">
        <span class="pill ink">${esc(status)}</span>
        <span class="small muted">${esc(s.players.length)} player${s.players.length === 1 ? "" : "s"} · ${esc(s.types.map((t) => (t === "mcq" ? "multiple choice" : "fill in the blank")).join(" + "))}</span>
      </div>
    </div>
    <p class="muted small">${lobby ? "Scan the QR code with a phone camera, or open" : "Players join at"} <b class="url">${esc(site)}</b> and type the code.</p>
  </section>`;
  if (!lobby) return info;
  // Same two columns as the lobby below, so the QR code is exactly as wide as the Build deck card.
  return `<div class="grid g-side head-split">${info}
    <img class="qr" src="/api/sessions/${encodeURIComponent(s.code)}/qr.svg" width="400" height="400" alt="QR code that opens this game's join page"></div>`;
}

function noticesHTML(s) {
  const held = s.held_back.map((h) => `<div class="notice">${ICON.alert}<p class="small"><b>${esc(h.course)}</b> Brightspace material is held back until ${esc(h.waiting_on.join(", "))} ${h.waiting_on.length === 1 ? "connects" : "connect"} Brightspace and ${h.waiting_on.length === 1 ? "is" : "are"} confirmed as enrolled. Uploaded notes still count.</p></div>`);
  const notes = s.notices.map((n) => `<div class="notice">${ICON.info}<p class="small">${esc(n)}</p></div>`);
  return held.concat(notes).join("") ? `<div class="grid">${held.concat(notes).join("")}</div>` : "";
}

// How the deck used Boilerexams past exam questions; nothing at all when it didn't.
function styleHTML(s) {
  const by = new Map();
  for (const st of s.strata) {
    const bx = st.boilerexams;
    if (!bx) continue;
    const t = by.get(bx.course) || { cards: 0, examples: 0, material: false };
    t.cards += bx.cards; t.examples += bx.examples;
    t.material = t.material || bx.material;
    by.set(bx.course, t);
  }
  if (!by.size) return "";
  const parts = [...by].map(([code, t]) => {
    const what = [t.cards ? `${t.cards} card${t.cards === 1 ? " is a real past exam question" : "s are real past exam questions"}` : "",
      t.material ? "the AI creates new practice from their questions and correct answers" : t.examples ? "the AI writes in their style" : ""].filter(Boolean).join(", and ");
    return `<b>${esc(spacedCode(code))}</b>: ${what}`;
  });
  return `<div class="notice good">${ICON.info}<p class="small">From <a href="https://boilerexams.com" target="_blank" rel="noopener">Boilerexams</a> · ${parts.join("; ")}.</p></div>`;
}

function sectionsHTML(s) {
  return `<section class="card">
    <div class="card-head"><div><p class="eyebrow">The deck is dealt evenly across</p><h2>${s.strata.length} section${s.strata.length === 1 ? "" : "s"}</h2></div></div>
    <ul class="sections">${s.strata.map((st) => {
      const c = st.counts;
      const tag = (n, word) => `<span class="${n ? "" : "zero"}">${n} ${word}</span>`;
      const bx = st.boilerexams && st.boilerexams.cards ? `<span class="bx">${st.boilerexams.cards} from past exams</span>` : "";
      return `<li><b>${esc(st.label)}</b><div class="counts">${s.has_brightspace ? tag(c.brightspace, "from Brightspace") : ""}${tag(c.upload, "from files")}${tag(c.notes, "from notes")}${bx}</div></li>`;
    }).join("")}</ul>
    <p class="hint">Counts are passages of material.${s.has_brightspace ? " Brightspace material is read when the host builds the deck." : " Every player can add notes and files."}</p>
    ${(s.picked || []).map(pickedHTML).join("")}
  </section>`;
}

function pickedHTML(p) {
  const how = p.how === "ai" ? `The AI picked ${p.files.length} of ${p.of} files` : p.how === "titles" ? `Picked ${p.files.length} of ${p.of} files whose titles match your topics` : `Read ${p.files.length} of ${p.of} files`;
  return `<details class="picked"><summary><b>${esc(p.course)}</b>: ${how}</summary>
    ${p.folders && p.folders.length ? `<p class="small muted">Opened whole: ${p.folders.map(esc).join("; ")}</p>` : ""}
    <ul>${p.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></details>`;
}

function playersHTML(s) {
  const play = s.status === "ready";
  return `<section class="card">
    <p class="eyebrow">Players</p>
    <ul class="players">${s.players.map((p) => {
      let note = p.host ? "Host" : "Player";
      if (!play && s.has_brightspace) note += p.missing.length ? ` · not verified for ${p.missing.join(", ")}` : " · verified for every course";
      if (!play && p.uploads) note += ` · ${p.uploads} file${p.uploads === 1 ? "" : "s"}`;
      const kick = s.role === "host" && !p.host && s.status !== "building"
        ? `<button type="button" class="x-btn" data-kick="${esc(p.key)}" data-name="${esc(p.name)}" aria-label="Remove ${esc(p.name)} from the game" title="Remove from game">×</button>`
        : s.role === "host" && s.status !== "building" ? '<span class="x-space" aria-hidden="true"></span>' : ""; // keeps the scores lined up
      return `<li class="${p.me ? "me" : ""}"><span class="avatar" aria-hidden="true">${esc(p.name.slice(0, 1).toUpperCase())}</span>
        <span><b>${esc(p.name)}${p.me ? " (you)" : ""}</b><small>${esc(note)}</small></span>
        ${play || !s.has_brightspace ? "" : p.missing.length ? '<span class="dot" title="Not verified"></span>' : '<span class="dot on" title="Verified"></span>'}${kick}</li>`;
    }).join("")}</ul>
  </section>`;
}

// The host removes a player, after confirming in place (window.confirm is unreliable in some browsers).
function bindPlayers(el) {
  $$("[data-kick]", el).forEach((b) => b.addEventListener("click", () => {
    const li = b.closest("li"), original = li.innerHTML;
    li.innerHTML = `<div class="confirm-box kick-box" role="alertdialog" aria-label="Remove ${esc(b.dataset.name)}">
      <p class="small"><b>Remove ${esc(b.dataset.name)}?</b> They can't rejoin this game, and their uploaded material is removed.</p>
      <div class="btn-row"><button class="btn danger sm" type="button" data-yes>Remove</button><button class="btn line sm" type="button" data-no>Cancel</button></div></div>`;
    const yes = $("[data-yes]", li);
    yes.focus();
    yes.addEventListener("click", () => guarded(yes, async () => {
      render(await api(`/api/sessions/${game.code}/kick`, { method: "POST", json: { player: b.dataset.kick } }));
      toast(`${b.dataset.name} was removed from the game.`);
    }));
    $("[data-no]", li).addEventListener("click", () => { li.innerHTML = original; bindPlayers(li); });
  }));
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
      ${s.role === "host"
        ? (s.card && !s.outcome
          ? `<button class="btn dark big wide" type="button" id="drawBtn" disabled aria-describedby="drawWait">Draw next card</button><p class="small muted" id="drawWait">Enter ${esc(s.answering ? `${s.answering.name}'s` : "the")} answer on the card first (or press Didn't know), then you can draw the next one.</p>`
          : `<button class="btn dark big wide" type="button" id="drawBtn">${s.card ? "Draw next card" : "Draw first card"}</button>`)
        : '<p class="small muted">The host draws each card.</p>'}
    </section>`;
  }
  const mine = s.players.find((p) => p.me);
  let share = "";
  const bs = me.brightspace;
  if (s.has_brightspace && bs.signing_in) {
    share = `<div class="notice">${ICON.info}<p class="small"><b>Signing in to Brightspace…</b> ${bs.signin_on_host
      ? "Go to the computer running Study Deck: a Purdue sign-in window with your name is open there. Sign in with your own account."
      : "Finish in the Purdue tab."} This page updates by itself.</p></div>`;
  } else if (s.has_brightspace && !bs.connected) {
    share = `<div class="grid"><p class="small muted">Connect Brightspace so the game can confirm you're enrolled. Until everyone is, that course's Brightspace material stays out of the deck.</p>
      ${bs.can_connect ? `<a class="btn line" href="/auth/brightspace/start?next=${encodeURIComponent(`/#/game/${s.code}`)}">Connect Brightspace</a>
      ${bs.signin_on_host ? '<p class="hint">Opens a Purdue sign-in window for you on the computer running Study Deck. You sign in there with your own account and Authenticator.</p>' : ""}`
        : '<p class="hint">Brightspace can only be connected on the computer running Study Deck.</p>'}</div>`;
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

const UPLOAD_TYPES = [".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".htm"];
const MAX_FILE_BYTES = 15 * 1048576;
const BATCH_FILES = 8, BATCH_BYTES = 40 * 1048576; // the server takes up to 60 MB per request
// Folder picking works in desktop browsers; phones only pick files.
const CAN_PICK_FOLDER = "webkitdirectory" in document.createElement("input") && !matchMedia("(pointer: coarse)").matches;

function uploadHTML(s) {
  if (s.status === "building") return "";
  const inPlay = s.status === "ready";
  return `${inPlay ? "" : '<section class="card"><p class="eyebrow">Add material</p><h2>Your notes &amp; files</h2>'}
    <form id="uploadForm" class="grid" novalidate>
      <label class="f">Section<select class="input" name="stratum">${s.strata.map((st) => `<option value="${esc(st.id)}">${esc(st.label)}</option>`).join("")}</select></label>
      <div class="grid tight-gap">
        <label class="drop" id="drop">${ICON.file}<b>Drop files or folders here</b><span class="hint">or click to browse. PDF, Word, PowerPoint, text. Up to 15 MB each.</span>
          <input type="file" id="pickFiles" multiple accept="${UPLOAD_TYPES.join(",")}"></label>
        ${CAN_PICK_FOLDER ? '<label class="btn line wide folder-btn">Choose a whole folder<input type="file" id="pickFolder" webkitdirectory multiple></label>' : ""}
      </div>
      <div id="fileList" aria-live="polite"></div>
      <label class="f notes-f">Or paste notes<textarea class="input" name="notes" maxlength="60000" placeholder="Paste your own study notes"></textarea></label>
      <label class="check"><input type="checkbox" name="attest" value="yes"><span>This is my own work or course material I'm allowed to share with these players. No exams, quizzes, answer keys or other students' personal information.</span></label>
      <button class="btn gold wide" type="submit">Add to the deck</button>
    </form>${inPlay ? "" : "</section>"}`;
}

/* Files waiting to be added. Each pick or drop adds to this list (it never replaces
   it), and it outlives re-renders of the upload box. Cleared when they're sent. */
let picks = { code: null, files: [] };
function pickedFiles() {
  if (picks.code !== game.code) picks = { code: game.code, files: [] };
  return picks.files;
}

// Every file in a drop, opening dropped folders at any depth. The browser only lets
// a drop be read during the event, so the entries are taken before the first await.
async function droppedFiles(dt) {
  const entries = [...(dt.items || [])].map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null)).filter(Boolean);
  const plain = [...dt.files].map((file) => ({ file, path: file.name }));
  if (!entries.length) return plain;
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      out.push({ file: await new Promise((ok, bad) => entry.file(ok, bad)), path: entry.fullPath.replace(/^\//, "") });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) { // a folder is read in pages until an empty one comes back
        const page = await new Promise((ok, bad) => reader.readEntries(ok, bad));
        if (!page.length) break;
        for (const e of page) await walk(e);
      }
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

function addPicks(items) {
  const list = pickedFiles();
  const seen = new Set(list.map((p) => `${p.path}|${p.file.size}`));
  let wrongType = 0;
  const tooBig = [];
  for (const { file, path } of items) {
    const name = file.name;
    if (!file.size || name.startsWith(".") || name.startsWith("~$")) continue; // empty, hidden and Office lock files
    if (!UPLOAD_TYPES.includes(name.slice(name.lastIndexOf(".")).toLowerCase())) { wrongType++; continue; }
    if (file.size > MAX_FILE_BYTES) { tooBig.push(name); continue; }
    const key = `${path}|${file.size}`;
    if (seen.has(key)) continue; // the same file picked twice
    seen.add(key);
    list.push({ file, path });
  }
  const left = [];
  if (wrongType) left.push(`${wrongType} file${wrongType === 1 ? " that isn't" : "s that aren't"} PDF, Word, PowerPoint or text`);
  if (tooBig.length) left.push(`${tooBig.join(", ")} (over 15 MB)`);
  if (left.length) toast(`Left out ${left.join(" and ")}.`, true);
}

function uploadRoom() {
  return game.state ? Math.max(0, game.state.me.max_uploads - game.state.me.uploads) : Infinity;
}

const fileSize = (bytes) => (bytes < 1048576 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1048576).toFixed(1)} MB`);

function fileListHTML() {
  const list = pickedFiles();
  if (!list.length) return "";
  const total = list.reduce((n, p) => n + p.file.size, 0);
  const room = uploadRoom();
  const over = list.length > room
    ? `<p class="small err-text">You can add ${room} more file${room === 1 ? "" : "s"} in this game. Remove ${list.length - room} to continue.</p>` : "";
  return `<div class="picked-files">
    <div class="picked-head"><b>${list.length} file${list.length === 1 ? "" : "s"} ready</b><span class="muted small">${fileSize(total)}</span>
      <button type="button" class="link-btn" data-clear>Clear all</button></div>${over}
    <ul class="files">${list.map((p, i) => `<li><span class="fname" title="${esc(p.path)}">${esc(p.path)}</span>
      <span class="muted">${fileSize(p.file.size)}</span>
      <button type="button" class="x-btn" data-remove="${i}" aria-label="Remove ${esc(p.path)}">×</button></li>`).join("")}</ul></div>`;
}

function bindUpload(el) {
  const form = $("#uploadForm", el);
  if (!form) return;
  const drop = $("#drop", form);
  const list = $("#fileList", form);
  const show = () => { list.innerHTML = fileListHTML(); };
  show();
  for (const input of $$("input[type=file]", form)) {
    input.addEventListener("change", () => {
      addPicks([...input.files].map((file) => ({ file, path: file.webkitRelativePath || file.name })));
      input.value = ""; // so the same file can be picked again after removing it
      show();
    });
  }
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => {
    const reading = droppedFiles(e.dataTransfer);
    guarded(null, async () => { addPicks(await reading); show(); });
  });
  list.addEventListener("click", (e) => {
    const rm = e.target.closest("[data-remove]");
    if (rm) pickedFiles().splice(Number(rm.dataset.remove), 1);
    else if (e.target.closest("[data-clear]")) pickedFiles().length = 0;
    else return;
    show();
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const button = $("button[type=submit]", form);
    const label = button.textContent;
    guarded(button, async () => {
      if (!form.attest.checked) throw new Error("Confirm you're allowed to share this material.");
      const files = pickedFiles().slice();
      const notes = form.notes.value.trim();
      if (!files.length && !notes) throw new Error("Add a file or paste some notes.");
      if (files.length > uploadRoom()) throw new Error(`You can add ${uploadRoom()} more files in this game. Remove some first.`);
      // Sent a few at a time, so a whole folder never makes one huge request.
      const batches = [[]];
      let size = 0;
      for (const p of files) {
        const cur = batches[batches.length - 1];
        if (cur.length && (cur.length >= BATCH_FILES || size + p.file.size > BATCH_BYTES)) { batches.push([]); size = 0; }
        batches[batches.length - 1].push(p);
        size += p.file.size;
      }
      let added = 0, writing = false, state = null;
      const skipped = [];
      try {
        for (const [i, batch] of batches.entries()) {
          if (files.length > 1) {
            const first = files.indexOf(batch[0]) + 1, last = first + batch.length - 1;
            button.textContent = `Adding ${first === last ? first : `${first}–${last}`} of ${files.length}…`;
          }
          const fd = new FormData();
          fd.append("stratum", form.stratum.value);
          fd.append("attest", "yes");
          if (i === 0 && notes) fd.append("notes", notes);
          for (const p of batch) fd.append("files", p.file, p.file.name);
          const res = await api(`/api/sessions/${game.code}/upload`, { method: "POST", form: fd });
          added += res.added; writing = writing || res.writing; state = res.state;
          skipped.push(...res.skipped);
          // Only what's been sent leaves the list, so if something fails the rest stays.
          picks.files = pickedFiles().filter((p) => !batch.includes(p));
          if (i === 0) form.notes.value = "";
        }
      } finally {
        button.textContent = label;
        show();
        if (state) render(state);
      }
      form.attest.checked = false;
      const what = `Added ${added} passage${added === 1 ? "" : "s"} of material.${writing ? " Cards from it are being written and will be mixed into the cards still to come." : ""}`;
      toast(skipped.length ? `${what} Skipped: ${skipped.join("; ")}` : what, skipped.length > 0);
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
  // The physical board determines turns; only the host's screen checks the answer.
  const c = s.card, out = s.outcome, host = s.role === "host";
  let body;
  if (c.type === "mcq") {
    body = `<div class="options">${c.options.map((o, i) => {
      let cls = "";
      if (out) { if (o === out.expected) cls = "right"; else if (o === out.given) cls = "wrong"; }
      const inner = `<span class="letter">${"ABCDE"[i]}</span><span>${esc(o)}</span>`;
      return host && !out
        ? `<button type="button" class="opt" data-opt="${i}">${inner}</button>`
        : `<div class="opt ${cls} static">${inner}</div>`;
    }).join("")}</div>${host && !out ? '<div class="idk-row"><button class="btn line sm" id="idkBtn" type="button">Didn\'t know</button></div>' : ""}`;
  } else {
    body = host && !out ? `<form class="dock" id="blankForm" novalidate><input name="answer" maxlength="80" autocomplete="off" spellcheck="false" placeholder="Type the answer they gave" aria-label="Answer they gave">
      <button class="send" type="submit" aria-label="Check answer">${ICON.up}</button></form>
      <div class="idk-row"><button class="btn line sm" id="idkBtn" type="button">Didn't know</button></div>` : "";
  }
  const result = out ? `<div class="result ${out.correct ? "ok" : "no"}" role="status"><b>${out.correct ? "Correct" : out.skipped ? "Didn't know" : "Not quite"}</b>
      ${out.correct ? "" : `<p>Answer: <b>${esc(out.expected)}</b>${c.type === "fill_blank" && !out.skipped ? ` · answered “${esc(out.given)}”` : ""}</p>`}
      ${out.explanation ? `<p class="small">${esc(out.explanation)}</p>` : ""}</div>` : "";
  return `<article class="qcard" aria-label="Question card ${esc(c.number)}">
    <div class="strip"><b>${esc(c.label)}</b><span class="small">Card ${esc(c.number)} · ${c.source ? `<span class="past">${esc(c.source)}</span>` : c.type === "mcq" ? "Multiple choice" : "Fill in the blank"}</span></div>
    <div class="body"><p class="prompt">${promptHTML(c.prompt)}</p>${body}${result}</div>
  </article>`;
}

function bindCard(el) {
  const s = game.state;
  if (!s.card || s.role !== "host" || s.outcome) return;
  const submit = (answer, skip = false) => api(`/api/sessions/${game.code}/answer`, {
    method: "POST", json: { question_id: s.card.id, answer, skip },
  }).then(render);
  $$("[data-opt]", el).forEach((b) => b.addEventListener("click", () => {
    b.classList.add("picked");
    guarded(null, () => submit(Number(b.dataset.opt)));
  }));
  const form = $("#blankForm", el);
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!form.answer.value.trim()) return;
      $(".send", form).classList.add("go");
      guarded($(".send", form), () => submit(form.answer.value));
    });
  }
  const idk = $("#idkBtn", el);
  if (idk) idk.addEventListener("click", () => guarded(idk, () => submit("", true)));
}

/* Confirmation is shown in the page: window.confirm() is blocked in some
   embedded and mobile browsers, where it returns "cancel" without showing anything. */
function bindLeave(el) {
  const original = el.innerHTML;
  const btn = $("#leaveBtn", el);
  btn.addEventListener("click", () => {
    const host = game.state.role === "host";
    el.innerHTML = `<div class="confirm-box" role="alertdialog" aria-labelledby="leaveQ">
      <p id="leaveQ" class="small"><b>${host ? "End this game for everyone?" : "Leave this game?"}</b> ${host ? "All material and cards are deleted." : "Your uploaded material leaves with you."}</p>
      <div class="btn-row"><button class="btn danger sm" type="button" id="leaveYes">${host ? "Yes, end game" : "Yes, leave"}</button>
      <button class="btn line sm" type="button" id="leaveNo">Cancel</button></div></div>`;
    const yes = $("#leaveYes", el);
    yes.focus();
    yes.addEventListener("click", () => guarded(yes, async () => {
      stopPolling();
      await api(`/api/sessions/${game.code}/${host ? "end" : "leave"}`, { method: "POST" });
      game = { code: null };
      location.hash = "#/";
      toast(host ? "Game ended. Everything was deleted." : "You left the game.");
    }));
    $("#leaveNo", el).addEventListener("click", () => { el.innerHTML = original; bindLeave(el); $("#leaveBtn", el).focus(); });
  });
}

route();
