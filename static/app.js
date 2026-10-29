"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => Number(x) || 0;
const usd = (x) => "$" + num(x).toFixed(num(x) >= 0.1 ? 3 : 4);
const TIERS = ["nano", "super", "ultra"];
const tierOf = (t) => (TIERS.includes(t) ? t : "nano");   // safe for class names / CSS vars
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let CONFIG = null, EXAMPLES = [], policy = "ladder-wide", running = null;

// ------------------------------------------------------------------ tabs
function showView(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "bench") loadBench();
  if (name === "gallery") loadGallery();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-goto]");
  if (a) { e.preventDefault(); showView(a.dataset.goto); }
});

function selectPolicy(p) {
  policy = p;
  $$("#policy button").forEach((x) => x.classList.toggle("on", x.dataset.p === p));
}
$$("#policy button").forEach((b) => b.addEventListener("click", () => { selectPolicy(b.dataset.p); resetLadder(); }));

// ------------------------------------------------------------------ init
async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  if (!CONFIG.policies?.[policy]) selectPolicy("ladder");
  for (const [t, info] of Object.entries(CONFIG.tiers)) {
    const r = $(`.rung[data-tier="${tierOf(t)}"] .rmodel`);
    if (r) r.textContent = `${info.model}  ·  $${info.price_in}/$${info.price_out} per 1M`;
  }
  const key = store.get("nebius_key");
  $("#apikey").value = key;
  $("#apikey").addEventListener("input", (e) => { store.set("nebius_key", e.target.value.trim()); keyState(); });
  keyState();
  if (!CONFIG.server_key && !key) $("#keybox").open = true;

  EXAMPLES = await fetch("/api/examples").then((r) => r.json()).catch(() => []);
  const sel = $("#example");
  for (const ex of EXAMPLES) sel.insertAdjacentHTML("beforeend", `<option value="${esc(ex.id)}">[${esc(ex.difficulty)}] ${esc(ex.title)}</option>`);
  sel.addEventListener("change", () => {
    const ex = EXAMPLES.find((e) => e.id === sel.value);
    if (!ex) return;
    $("#spec").value = ex.spec;
    $("#starter").value = ex.starter || "";
    $("#tests").value = "";
  });
  resetLadder();
  fromHash();
}
function keyState() {
  const own = $("#apikey").value.trim();
  $("#keystate").textContent = own ? "· using your key" : CONFIG?.server_key ? "· server key active" : "· required";
}

// ------------------------------------------------------------------ ladder visual
function resetLadder(pol = policy) {
  const p = CONFIG?.policies?.[pol];
  $$(".rung").forEach((r) => {
    r.classList.remove("active");
    r.classList.toggle("disabled", p ? !p.ladder.includes(r.dataset.tier) : false);
    $(".dots", r).innerHTML = "";
  });
}
function setActive(tier) { $$(".rung").forEach((r) => r.classList.toggle("active", r.dataset.tier === tier)); }
function busyDot(tier) {
  const d = document.createElement("span");
  d.className = "dot busy";
  $(`.rung[data-tier="${tierOf(tier)}"] .dots`)?.appendChild(d);
  return d;
}

// ------------------------------------------------------------------ per-run view state
// cards: sid -> live call card; dots: sid -> ladder dot; lanes: sid -> lane element; rounds: first attempt -> round grid
let S, totals, t0, timer;

function beginRun(pol) {
  resetLadder(pol);
  $("#timeline").innerHTML = "";
  $("#final").classList.add("hidden");
  $("#share").classList.add("hidden");
  S = { cards: {}, dots: {}, lanes: {}, rounds: {}, runId: null };
  totals = { cost: 0, ultra: 0, tok: 0 };
  renderStats();
  $("#s-time").textContent = "0.0s";
}
function settle() {
  // Nothing is in flight any more: stop spinners and pulsing dots left by interrupted calls.
  $$("#timeline .spinner").forEach((s) => s.remove());
  for (const c of Object.values(S?.cards || {})) interrupt(c);
  for (const d of Object.values(S?.dots || {})) if (d.classList.contains("busy")) d.className = "dot";
}
// A call that ended without an 'llm' event: freeze its card instead of leaving it "thinking".
function interrupt(c) {
  if (!c || c.el.classList.contains("finished")) return;
  $(".spinner", c.el)?.remove();
  for (const p of [c.think, c.out]) {
    if (p.d.classList.contains("hidden")) continue;
    p.d.open = false;
    p.sum.textContent = `Interrupted (${p.n.toLocaleString()} chars)`;
  }
  c.el.classList.add("finished");
}

// ------------------------------------------------------------------ live run
$("#run").addEventListener("click", run);
async function run() {
  stopReplay(true);
  const spec = $("#spec").value.trim();
  if (spec.length < 10) { $("#spec").focus(); return; }
  if (running) running.abort();
  running = new AbortController();
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  const btn = $("#run");
  btn.disabled = true; btn.textContent = "Agent running…";
  beginRun(policy);
  t0 = performance.now();
  timer = setInterval(() => { $("#s-time").textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"; }, 100);

  const headers = { "Content-Type": "application/json" };
  const key = $("#apikey").value.trim();
  if (key) headers["X-Nebius-Key"] = key;
  let finished = false;
  try {
    const res = await fetch("/api/solve", {
      method: "POST", headers, signal: running.signal,
      body: JSON.stringify({ spec, tests: $("#tests").value, starter: $("#starter").value, policy }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail ? (typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)) : res.statusText);
    }
    const runId = res.headers.get("X-Ladder-Run");
    S.runId = /^[0-9a-f]{12}$/.test(runId || "") ? runId : null;
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "done") finished = true;
        handle(ev);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") errorCard(e.message);
  } finally {
    clearInterval(timer);
    settle();
    btn.disabled = false; btn.textContent = "Run agent";
    running = null;
  }
  if (finished && S.runId) $("#share").classList.remove("hidden");
}

// ------------------------------------------------------------------ cards
function addCard(cls, html, parent = $("#timeline")) {
  const el = document.createElement("div");
  el.className = "ev " + cls;
  el.innerHTML = html;
  parent.appendChild(el);
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return el;
}
function errorCard(msg, parent) {
  return addCard("error", `<div class="ev-head"><span class="badge fail">error</span><span class="ev-title">${esc(msg)}</span></div>`, parent);
}
function renderStats() {
  $("#s-cost").textContent = usd(totals.cost);
  $("#s-ultra").textContent = usd(totals.ultra);
  $("#s-tok").textContent = totals.tok.toLocaleString();
}
const ROLE = { triage: "Triaging difficulty", tests: "Writing unit tests", code: "Writing solution" };
const roleName = (r) => ROLE[r] || esc(r);

function callTitle(ev) {
  let what = roleName(ev.role);
  if (ev.role === "code" && ev.attempt) {
    what += num(ev.width) > 1 ? ` · draft ${num(ev.candidate) + 1}/${num(ev.width)}` : ` · attempt ${num(ev.attempt)}`;
  }
  return what;
}

// A round of width k: k lanes side by side, each holding its draft's call card then its run card.
function roundFor(ev) {
  const width = num(ev.width), first = num(ev.attempt) - num(ev.candidate);
  if (S.rounds[first]) return S.rounds[first];
  const t = tierOf(ev.tier);
  const el = addCard("round", `<div class="ev-head"><span class="badge ${t}">${t}</span>
    <span class="ev-title">${width} parallel drafts · attempts ${first}–${first + width - 1}</span>
    <span class="ev-meta round-meta">racing…</span></div><div class="lanes" style="--n:${width}"></div>`);
  const lanes = [];
  for (let c = 0; c < width; c++) {
    const lane = document.createElement("div");
    lane.className = "lane";
    lane.innerHTML = `<div class="lane-label">Draft ${c + 1}</div>`;
    $(".lanes", el).appendChild(lane);
    lanes.push(lane);
  }
  return (S.rounds[first] = { el, lanes });
}
function parentFor(ev) { return S.lanes[ev.sid] || $("#timeline"); }

const LIVE_CAP = 20000;
const toScroll = new Set();
let scrollQueued = false;
function liveScroll(pre) {
  toScroll.add(pre);
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    for (const p of toScroll) if (p._stick !== false) p.scrollTop = p.scrollHeight;
    toScroll.clear(); scrollQueued = false;
  });
}
function livePart(card, cls) {
  const d = $("details." + cls, card);
  const pre = $("pre", d);
  pre.addEventListener("scroll", () => { pre._stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30; }, { passive: true });
  return { d, pre, sum: $("summary", d), n: 0 };
}
function callCard(ev) {
  const sid = String(ev.sid ?? "");
  if (ev.role === "code" && num(ev.width) > 1) {
    const round = roundFor(ev);
    const lane = round.lanes[num(ev.candidate)];
    if (lane) S.lanes[sid] = lane;
  }
  const t = tierOf(ev.tier);
  const el = addCard("call", `<div class="ev-head"><span class="badge ${t}">${t}</span>
      <span class="ev-title">${callTitle(ev)}</span><span class="spinner" style="--c:var(--${t})"></span>
      <span class="ev-meta">${esc(ev.model)}</span></div>
    <details class="think hidden" open><summary>Nemotron is thinking…</summary><pre class="live dim"></pre></details>
    <details class="out hidden" open><summary>${ev.role === "code" ? "Writing code…" : "Writing…"}</summary><pre class="live"></pre></details>`,
  parentFor({ sid }));
  const card = { el, role: ev.role, think: livePart(el, "think"), out: livePart(el, "out") };
  S.cards[sid] = card;
  return card;
}
function appendLive(part, text) {
  if (!text) return;
  part.d.classList.remove("hidden");
  part.pre.appendChild(document.createTextNode(text));
  part.n += text.length;
  if (part.n > LIVE_CAP + 4000) {   // keep only the tail; amortised so most deltas are a plain append
    const tail = part.pre.textContent.slice(-LIVE_CAP);
    part.pre.textContent = tail;
    part.n = tail.length;
  }
  liveScroll(part.pre);
}
function finishPart(part, finalText, label) {
  if (!part.n && finalText) { part.pre.textContent = String(finalText).slice(-LIVE_CAP); part.n = part.pre.textContent.length; }
  if (!part.n) return;
  part.d.classList.remove("hidden");
  part.d.open = false;
  part.sum.textContent = `${label} (${Math.max(part.n, String(finalText || "").length).toLocaleString()} chars)`;
}

// ------------------------------------------------------------------ event handler (live runs and replays)
function handle(ev) {
  switch (ev.type) {
    case "start": {
      const names = Object.entries(ev.models || {}).map(([t, m]) => `<span class="badge ${tierOf(t)}">${esc(t)}</span> ${esc(m)}`).join(" &nbsp; ");
      const wide = Object.entries(ev.policy?.width || {}).filter(([, k]) => num(k) > 1)
        .map(([t, k]) => ` · ${num(k)} parallel ${esc(t)} drafts`).join("");
      addCard("", `<div class="ev-head"><span class="ev-title">Policy <b>${esc(ev.policy?.name)}</b>${wide}</span><span class="ev-meta">${names}</span></div>`);
      break;
    }
    case "call": {
      setActive(tierOf(ev.tier));
      callCard(ev);
      if (ev.role === "code") S.dots[ev.sid] = busyDot(ev.tier);
      break;
    }
    case "delta": {
      const c = S.cards[ev.sid];
      if (!c) break;
      if (ev.kind === "reset") {
        for (const p of [c.think, c.out]) { p.pre.textContent = ""; p.n = 0; }
      } else appendLive(ev.kind === "reasoning" ? c.think : c.out, ev.text);
      break;
    }
    case "llm": {
      totals.cost += num(ev.cost); totals.tok += num(ev.tokens_in) + num(ev.tokens_out);
      const u = CONFIG.tiers.ultra; totals.ultra += (num(ev.tokens_in) * u.price_in + num(ev.tokens_out) * u.price_out) / 1e6;
      renderStats();
      const c = S.cards[ev.sid] || callCard(ev);
      const t = tierOf(ev.tier);
      const meta = `${num(ev.tokens_in).toLocaleString()} in · ${num(ev.tokens_out).toLocaleString()} out · ${usd(ev.cost)} · ${num(ev.latency_s)}s`;
      $(".ev-head", c.el).innerHTML = `<span class="badge ${t}">${t}</span>
        <span class="ev-title">${callTitle(ev)}</span><span class="ev-meta">${meta}</span>`;
      finishPart(c.think, ev.reasoning, "Nemotron reasoning");
      finishPart(c.out, ev.content, ev.role === "code" ? "Model output" : "Output");
      c.el.classList.add("finished");
      break;
    }
    case "triage":
      addCard("", `<div class="ev-head"><span class="badge warn">triage</span><span class="ev-title">Difficulty: ${esc(ev.difficulty)} → start on <b>${esc(ev.start)}</b></span><span class="ev-meta">${esc(ev.why)}</span></div>`);
      break;
    case "tests": {
      const n = (String(ev.tests || "").match(/def test/g) || []).length;
      addCard("", `<div class="ev-head"><span class="badge ok">tests</span><span class="ev-title">${n} tests (${esc(ev.source)})</span>
        ${ev.why ? `<span class="ev-meta">${esc(ev.why)}</span>` : ""}</div><details><summary>Show tests</summary><pre>${esc(ev.tests)}</pre></details>`);
      break;
    }
    case "run": {
      const r = ev.report || { tests: [] };
      const dot = S.dots[ev.sid];
      if (dot) dot.className = "dot " + (ev.ok ? "ok" : "fail");
      const chips = (r.tests || []).map((t) => `<span class="chip ${t.ok ? "" : "fail"}" title="${esc(t.error)}">${esc(t.name)}</span>`).join("");
      const head = r.fatal ? "Crashed" : `${num(r.passed)}/${num(r.passed) + num(r.failed)} tests passed`;
      const failText = ev.ok ? "" : `<pre class="fail-text">${esc(ev.summary)}</pre>`;
      addCard("", `<div class="ev-head"><span class="badge ${ev.ok ? "ok" : "fail"}">sandbox</span>
        <span class="ev-title">${head}</span><span class="ev-meta">attempt ${num(ev.attempt)} · ${num(r.duration_s)}s</span></div>
        <div class="chips">${chips}</div>${failText}
        <details><summary>Code from this attempt</summary><pre>${esc(ev.code)}</pre></details>`, parentFor(ev));
      break;
    }
    case "pick": {
      const round = S.rounds[num(ev.attempt) - num(ev.candidate)];
      if (!round) break;
      round.lanes.forEach((lane, c) => {
        const win = c === num(ev.candidate);
        lane.classList.toggle("picked", win);
        lane.classList.toggle("dimmed", !win);
        if (win) $(".lane-label", lane).insertAdjacentHTML("beforeend", ` <span class="badge ${ev.ok ? "ok" : "warn"}">picked</span>`);
      });
      const scores = (ev.scores || []).map((s) => `draft ${num(s.candidate) + 1}: ${s.fatal ? "crashed" : `${num(s.passed)}/${num(s.total)}`}`).join(" · ");
      $(".round-meta", round.el).textContent = scores;
      break;
    }
    case "escalate":
      addCard("escalate", `<div class="ev-head"><span class="badge warn">escalate</span>
        <span class="ev-title">${esc(ev.from)} → ${esc(ev.to)}</span><span class="ev-meta">${esc(ev.reason)}</span></div>`);
      break;
    case "error": {
      if (ev.sid != null) interrupt(S.cards[ev.sid]);
      const dot = S.dots[ev.sid];
      if (dot) dot.className = "dot fail";
      const el = errorCard(ev.message, ev.sid != null ? parentFor(ev) : undefined);
      if (ev.recoverable) el.classList.add("recoverable");
      break;
    }
    case "done": done(ev); break;
  }
}

function done(ev) {
  settle();
  totals.cost = num(ev.cost); totals.ultra = num(ev.ultra_equivalent_cost); totals.tok = num(ev.tokens_in) + num(ev.tokens_out);
  renderStats();
  const saved = totals.ultra > 0 ? 1 - totals.cost / totals.ultra : 0;
  if (saved > 0.01) $("#s-cost").innerHTML = `${usd(ev.cost)}<small>−${Math.round(saved * 100)}%</small>`;
  $("#s-time").textContent = num(ev.wall_seconds).toFixed(1) + "s";
  const fin = $("#final");
  fin.classList.remove("hidden");
  fin.classList.toggle("failed", ev.status !== "solved");
  const n = num(ev.attempts);
  $("#final-title").textContent = ev.status === "solved"
    ? `✓ Solved by Nemotron ${ev.solved_by} in ${n} attempt${n > 1 ? "s" : ""}`
    : ev.status === "error" ? "Stopped by an error" : `✗ Not solved after ${n} attempts (last code below)`;
  const code = $("#final-code");
  code.textContent = ev.code || "";
  code.removeAttribute("data-highlighted");
  code.className = "language-python";
  if (window.hljs && ev.code) hljs.highlightElement(code);
  if (ev.status === "solved") setActive(tierOf(ev.solved_by));
}

function flash(btn, text, back) { btn.textContent = text; setTimeout(() => (btn.textContent = back), 1200); }
$("#copy").addEventListener("click", () => {
  navigator.clipboard?.writeText($("#final-code").textContent);
  flash($("#copy"), "Copied", "Copy code");
});
$("#share").addEventListener("click", () => {
  if (!S?.runId) return;
  navigator.clipboard?.writeText(`${location.origin}${location.pathname}#run=${S.runId}`);
  flash($("#share"), "Link copied", "Copy replay link");
});

// ------------------------------------------------------------------ replays
// A stored run replays through handle(): gaps between events are capped, and each code/tests call
// re-streams its final reasoning + content as synthetic deltas between its 'call' and 'llm' events.
const GAP_MAX = 1.2, STREAM_MIN = 1.0, STREAM_MAX = 2.0;
const RUN_ID = /^([0-9a-f]{12}|[a-z0-9-]{1,64})$/;
let replay = null, replaySeq = 0;   // replaySeq invalidates replays still loading

function plan(events) {
  const out = [], callAt = {}, streams = [];
  let at = 0, prevT = 0;
  for (const ev of events) {
    if (!ev || typeof ev !== "object" || ev.type === "delta") continue;
    const t = Math.max(num(ev.t), prevT);
    at += Math.min(t - prevT, GAP_MAX); prevT = t;
    if (ev.type === "call") callAt[ev.sid] = at;
    if (ev.type === "llm" && (ev.role === "code" || ev.role === "tests") && callAt[ev.sid] != null) {
      at = Math.max(at, callAt[ev.sid] + STREAM_MIN);
      const r = String(ev.reasoning || ""), c = String(ev.content || "");
      if (r || c) streams.push({ sid: ev.sid, from: Math.max(callAt[ev.sid], at - STREAM_MAX), to: at, r, c, sent: 0 });
    }
    out.push({ ev, at });
  }
  return { out, streams };
}
function feed(s, frac) {
  const total = s.r.length + s.c.length, upto = Math.min(total, Math.floor(total * frac));
  if (upto <= s.sent) return;
  const rEnd = s.r.length;
  if (s.sent < rEnd) handle({ type: "delta", sid: s.sid, kind: "reasoning", text: s.r.slice(s.sent, Math.min(upto, rEnd)) });
  if (upto > rEnd) handle({ type: "delta", sid: s.sid, kind: "content", text: s.c.slice(Math.max(s.sent, rEnd) - rEnd, upto - rEnd) });
  s.sent = upto;
}

async function startReplay(id) {
  if (!RUN_ID.test(id)) return;
  stopReplay(true);
  const seq = ++replaySeq;
  if (running) running.abort();
  showView("solve");
  let data;
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(res.status === 404 ? "This recorded run no longer exists." : res.statusText);
    data = await res.json();
  } catch (e) {
    if (seq !== replaySeq) return;
    beginRun(policy);
    errorCard(`Could not load replay: ${e.message}`);
    return;
  }
  if (seq !== replaySeq) return;   // another replay or a live run took over meanwhile
  const events = Array.isArray(data.events) ? data.events : [];
  const start = events.find((e) => e?.type === "start");
  beginRun(start?.policy?.name || data.policy);
  const when = data.created ? new Date(data.created) : null;
  $("#replay-text").textContent = `Replay of a recorded run · ${when && !isNaN(when) ? when.toLocaleString() : "unknown date"} · ${data.policy || "?"}`;
  $("#replay-spec").textContent = data.spec || "";
  if (!$("#spec").value.trim() && data.spec) $("#spec").value = data.spec;   // so "Run agent" can re-run it live
  $("#replay-bar").classList.remove("hidden");
  $("#replay-btn").textContent = "Stop replay";
  if (location.hash !== "#run=" + id) history.replaceState(null, "", "#run=" + id);

  const { out, streams } = plan(events);
  const R = (replay = { id, stopped: false });
  const t0r = performance.now(), started = new Set();
  let i = 0;
  while (!R.stopped) {
    const now = (performance.now() - t0r) / 1000;
    for (; i < out.length && out[i].at <= now; i++) {
      const { ev } = out[i];
      if (ev.type === "llm") { const s = streams.find((x) => x.sid === ev.sid); if (s) feed(s, 1); }
      if (ev.type === "call") started.add(ev.sid);
      handle(ev);
      if (ev.type !== "done") $("#s-time").textContent = num(ev.t).toFixed(1) + "s";
    }
    for (const s of streams) {
      if (started.has(s.sid) && s.sent < s.r.length + s.c.length && now >= s.from) feed(s, (now - s.from) / Math.max(s.to - s.from, 0.01));
    }
    if (i >= out.length) break;
    await sleep(40);
  }
  if (replay !== R) return;   // superseded by another replay or a live run
  settle();
  $("#replay-text").textContent = $("#replay-text").textContent.replace(/^Replay of/, R.stopped ? "Stopped replay of" : "Replay of");
  $("#replay-btn").textContent = "Replay again";
  replay = null;
}
// Stop the running replay; `hide` also removes the banner (when something else takes over the view).
function stopReplay(hide = false) {
  if (replay) replay.stopped = true;
  if (hide) { replay = null; replaySeq++; $("#replay-bar").classList.add("hidden"); }
}
$("#replay-btn").addEventListener("click", () => {
  if (replay) { stopReplay(); return; }
  const m = location.hash.match(/^#run=(.+)$/);
  if (m) startReplay(decodeURIComponent(m[1]));
});
function fromHash() {
  const m = location.hash.match(/^#run=(.+)$/);
  if (m) startReplay(decodeURIComponent(m[1]));
}
window.addEventListener("hashchange", fromHash);

// ------------------------------------------------------------------ gallery
async function loadGallery() {
  const data = await fetch("/api/runs").then((r) => r.json()).catch(() => null);
  if (!data) return;
  const card = (s) => {
    const ok = s.status === "solved";
    const when = s.created ? new Date(s.created) : null;
    return `<button class="gcard" data-run="${esc(s.id)}">
      <span class="row between"><span class="badge ${ok ? "ok" : "fail"}">${esc(s.status || "?")}</span>
        <span class="muted small">${when && !isNaN(when) ? esc(when.toLocaleString()) : ""}</span></span>
      <span class="gtitle">${esc(s.title || "(no spec)")}</span>
      <span class="gmeta"><span>${esc(s.policy)}</span>${s.solved_by ? `<span class="badge ${tierOf(s.solved_by)}">${esc(s.solved_by)}</span>` : ""}
        <span>${usd(s.cost)}</span><span>${num(s.wall_seconds).toFixed(0)}s</span></span></button>`;
  };
  $("#g-featured").innerHTML = data.featured?.length ? data.featured.map(card).join("") : `<div class="empty">No featured runs yet.</div>`;
  $("#g-recent").innerHTML = data.recent?.length ? data.recent.map(card).join("")
    : `<div class="empty">No recorded runs yet. Finished runs from the Solve tab appear here.</div>`;
}
$("#view-gallery").addEventListener("click", (e) => {
  const b = e.target.closest("[data-run]");
  if (b) startReplay(b.dataset.run);
});

// ------------------------------------------------------------------ benchmark
const POLICY_COLOR = { ladder: "var(--green)", "ladder-wide": "#2dd4bf", "ladder-auto": "#a3e635",
  "nano-only": "var(--nano)", "super-only": "var(--super)", "ultra-only": "var(--ultra)" };
let BENCH = null, benchDiff = "";

async function loadBench() {
  const data = await fetch("/api/bench").then((r) => r.json()).catch(() => ({}));
  if (!data.available) return;
  BENCH = data;
  $("#bench-meta").innerHTML = `${num(data.n_tasks)} tasks (${Object.entries(data.difficulty_counts || {}).map(([k, v]) => `${num(v)} ${esc(k)}`).join(", ")}).
    Correctness is scored on <b>hidden tests</b> the agent never sees. Run on ${esc(data.generated_at)}.`;
  const P = data.policies || [];
  const maxCost = Math.max(...P.map((p) => num(p.cost)), 1e-9);
  const ultra = P.find((p) => p.name === "ultra-only");
  const ladder = P.find((p) => p.name === "ladder");
  let callouts = "";
  if (ultra && ladder) {
    const pct = ultra.cost > 0 ? Math.round((1 - ladder.cost / ultra.cost) * 100) : 0;
    callouts = `<div class="callouts">
      <div class="callout"><div class="big">${pct}%</div>cheaper than Ultra-only</div>
      <div class="callout"><div class="big">${Math.round(num(ladder.pass_rate) * 100)}% vs ${Math.round(num(ultra.pass_rate) * 100)}%</div>hidden-test pass rate, Ladder vs Ultra-only</div>
      <div class="callout"><div class="big">${num(ladder.solved_by?.nano)}/${num(ladder.solved)}</div>Ladder solves finished on Nano without escalating</div>
    </div>`;
  }
  const diffs = Object.keys(data.difficulty_counts || {});
  const rows = P.map((p) => `<tr class="${p.name === "ladder" ? "hl" : ""}">
      <td><b>${esc(p.name)}</b></td>
      <td>${num(p.solved)}/${num(p.n)} <span class="muted">(${Math.round(num(p.pass_rate) * 100)}%)</span></td>
      ${diffs.map((d) => `<td>${num(p.by_difficulty?.[d]?.solved)}/${num(p.by_difficulty?.[d]?.n)}</td>`).join("")}
      <td>${usd(p.cost)} <span class="bar" style="width:${Math.max(2, (num(p.cost) / maxCost) * 90)}px"></span></td>
      <td>${usd(p.cost_per_solve)}</td>
      <td>${num(p.avg_wall).toFixed(1)}s</td>
      <td class="muted">${Object.entries(p.solved_by || {}).map(([k, v]) => `${esc(k)}:${num(v)}`).join(" ")}</td></tr>`).join("");
  const filter = Array.isArray(data.records) && data.records.length ? `<div class="row between drill-head">
      <h3 class="sub">Per-task drilldown</h3>
      <select id="drill-diff" aria-label="Filter by difficulty"><option value="">All difficulties</option>
        ${diffs.map((d) => `<option value="${esc(d)}"${d === benchDiff ? " selected" : ""}>${esc(d)}</option>`).join("")}</select>
    </div><div id="drill" class="table-wrap"></div>` : "";
  $("#bench-body").innerHTML = `${callouts}<div class="table-wrap"><table>
    <thead><tr><th>Policy</th><th>Solved</th>${diffs.map((d) => `<th>${esc(d)}</th>`).join("")}<th>Total cost</th><th>$ / solve</th><th>Avg time</th><th>Solved on</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${scatter(P)}${filter}`;
  $("#drill-diff")?.addEventListener("change", (e) => { benchDiff = e.target.value; drilldown(); });
  drilldown();
}

function drilldown() {
  const box = $("#drill");
  if (!box || !BENCH) return;
  const pols = (BENCH.policies || []).map((p) => p.name);
  const tasks = new Map();
  for (const r of BENCH.records || []) {
    if (benchDiff && r.difficulty !== benchDiff) continue;
    if (!tasks.has(r.task)) tasks.set(r.task, { difficulty: r.difficulty, by: {} });
    tasks.get(r.task).by[r.policy] = r;
  }
  const cell = (r) => {
    if (!r) return `<td class="muted">–</td>`;
    const tip = `hidden tests ${r.hidden ?? "-"} · ${num(r.wall_s).toFixed(1)}s · ${num(r.escalations)} escalation${num(r.escalations) === 1 ? "" : "s"}`;
    const rung = r.solved_by ? `<span class="badge ${tierOf(r.solved_by)}">${esc(r.solved_by)}</span>` : "";
    return `<td class="cell ${r.solved ? "yes" : "no"}" title="${esc(tip)}"><b>${r.solved ? "✓" : "✗"}</b> ${rung} <span class="muted">${usd(r.cost)}</span></td>`;
  };
  const rows = [...tasks.entries()].map(([id, t]) => `<tr><td><b>${esc(id)}</b> <span class="muted small">${esc(t.difficulty)}</span></td>
    ${pols.map((p) => cell(t.by[p])).join("")}</tr>`).join("");
  box.innerHTML = rows ? `<table class="drill"><thead><tr><th>Task</th>${pols.map((p) => `<th>${esc(p)}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table>` : `<div class="empty">No tasks at this difficulty.</div>`;
}

function scatter(P) {
  const W = 640, H = 300, m = 48;
  const maxC = Math.max(...P.map((p) => num(p.cost))) * 1.15 || 1;
  const x = (c) => m + (num(c) / maxC) * (W - 2 * m);
  const y = (r) => H - m - num(r) * (H - 2 * m);
  const pts = P.map((p) => `<circle cx="${x(p.cost)}" cy="${y(p.pass_rate)}" r="7" fill="${POLICY_COLOR[p.name] || "#999"}"/>
    <text x="${x(p.cost) + 11}" y="${y(p.pass_rate) + 4}" fill="var(--text)" font-size="12">${esc(p.name)}</text>`).join("");
  const grid = [0, .25, .5, .75, 1].map((r) => `<line x1="${m}" x2="${W - m}" y1="${y(r)}" y2="${y(r)}" stroke="var(--line)"/>
    <text x="${m - 8}" y="${y(r) + 4}" text-anchor="end" fill="var(--muted)" font-size="11">${r * 100}%</text>`).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Pass rate versus total cost per policy">${grid}
    <text x="${W / 2}" y="${H - 10}" text-anchor="middle" fill="var(--muted)" font-size="12">total cost (USD) →  · up-and-left is better</text>
    <text x="${m}" y="${H - m + 18}" fill="var(--muted)" font-size="11">$0</text>
    <text x="${W - m}" y="${H - m + 18}" text-anchor="end" fill="var(--muted)" font-size="11">${usd(maxC)}</text>${pts}</svg></div>`;
}

init();
