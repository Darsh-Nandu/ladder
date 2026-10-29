"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const usd = (x) => "$" + (x || 0).toFixed(x >= 0.1 ? 3 : 4);
const store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let CONFIG = null, EXAMPLES = [], policy = "ladder", running = null;

// ------------------------------------------------------------------ tabs
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + b.dataset.view));
  if (b.dataset.view === "bench") loadBench();
}));

$$("#policy button").forEach((b) => b.addEventListener("click", () => {
  policy = b.dataset.p;
  $$("#policy button").forEach((x) => x.classList.toggle("on", x === b));
  resetLadder();
}));

// ------------------------------------------------------------------ init
async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  for (const [t, info] of Object.entries(CONFIG.tiers)) {
    const r = $(`.rung[data-tier="${t}"] .rmodel`);
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
}
function keyState() {
  const own = $("#apikey").value.trim();
  $("#keystate").textContent = own ? "· using your key" : CONFIG?.server_key ? "· server key active" : "· required";
}

// ------------------------------------------------------------------ ladder visual
function resetLadder() {
  const p = CONFIG?.policies?.[policy];
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
  $(`.rung[data-tier="${tier}"] .dots`)?.appendChild(d);
  return d;
}

// ------------------------------------------------------------------ run
let totals, t0, timer, pendingDot, pendingCard;

$("#run").addEventListener("click", run);
async function run() {
  const spec = $("#spec").value.trim();
  if (spec.length < 10) { $("#spec").focus(); return; }
  if (running) running.abort();
  running = new AbortController();
  const btn = $("#run");
  btn.disabled = true; btn.textContent = "Agent running…";
  resetLadder();
  $("#timeline").innerHTML = "";
  $("#final").classList.add("hidden");
  totals = { cost: 0, ultra: 0, tok: 0 };
  renderStats();
  t0 = performance.now();
  timer = setInterval(() => { $("#s-time").textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"; }, 100);

  const headers = { "Content-Type": "application/json" };
  const key = $("#apikey").value.trim();
  if (key) headers["X-Nebius-Key"] = key;
  try {
    const res = await fetch("/api/solve", {
      method: "POST", headers, signal: running.signal,
      body: JSON.stringify({ spec, tests: $("#tests").value, starter: $("#starter").value, policy }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail ? (typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)) : res.statusText);
    }
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
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") addCard("error", `<div class="ev-head"><span class="badge fail">error</span><span class="ev-title">${esc(e.message)}</span></div>`);
  } finally {
    clearInterval(timer);
    btn.disabled = false; btn.textContent = "Run agent";
    running = null;
  }
}

function addCard(cls, html) {
  const el = document.createElement("div");
  el.className = "ev " + cls;
  el.innerHTML = html;
  $("#timeline").appendChild(el);
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return el;
}
function renderStats() {
  $("#s-cost").textContent = usd(totals.cost);
  $("#s-ultra").textContent = usd(totals.ultra);
  $("#s-tok").textContent = totals.tok.toLocaleString();
}
const ROLE = { triage: "Triaging difficulty", tests: "Writing unit tests", code: "Writing solution" };
const priceUltra = () => CONFIG.tiers.ultra;

function handle(ev) {
  switch (ev.type) {
    case "start": {
      const names = Object.entries(ev.models).map(([t, m]) => `<span class="badge ${t}">${t}</span> ${esc(m)}`).join(" &nbsp; ");
      addCard("", `<div class="ev-head"><span class="ev-title">Policy <b>${esc(ev.policy.name)}</b></span><span class="ev-meta">${names}</span></div>`);
      break;
    }
    case "call": {
      setActive(ev.tier);
      if (ev.role === "code") pendingDot = busyDot(ev.tier);
      const what = ROLE[ev.role] + (ev.attempt ? ` · attempt ${ev.attempt}` : "");
      pendingCard = addCard("", `<div class="ev-head"><span class="badge ${ev.tier}">${ev.tier}</span>
        <span class="ev-title">${what}</span><span class="spinner" style="--c:var(--${ev.tier})"></span>
        <span class="ev-meta">${esc(ev.model)}</span></div>`);
      break;
    }
    case "llm": {
      totals.cost += ev.cost; totals.tok += ev.tokens_in + ev.tokens_out;
      const u = priceUltra(); totals.ultra += (ev.tokens_in * u.price_in + ev.tokens_out * u.price_out) / 1e6;
      renderStats();
      const meta = `${ev.tokens_in.toLocaleString()} in · ${ev.tokens_out.toLocaleString()} out · ${usd(ev.cost)} · ${ev.latency_s}s`;
      const reasoning = ev.reasoning ? `<details><summary>Nemotron reasoning (${ev.reasoning.length.toLocaleString()} chars)</summary><pre>${esc(ev.reasoning)}</pre></details>` : "";
      const body = ev.role === "code" ? "" : `<details><summary>Output</summary><pre>${esc(ev.content)}</pre></details>`;
      const html = `<div class="ev-head"><span class="badge ${ev.tier}">${ev.tier}</span>
        <span class="ev-title">${ROLE[ev.role] || ev.role}</span><span class="ev-meta">${meta}</span></div>${reasoning}${body}`;
      if (pendingCard) { pendingCard.innerHTML = html; pendingCard = null; } else addCard("", html);
      break;
    }
    case "triage":
      addCard("", `<div class="ev-head"><span class="badge warn">triage</span><span class="ev-title">Difficulty: ${esc(ev.difficulty)} → start on <b>${esc(ev.start)}</b></span><span class="ev-meta">${esc(ev.why)}</span></div>`);
      break;
    case "tests": {
      const n = (ev.tests.match(/def test/g) || []).length;
      addCard("", `<div class="ev-head"><span class="badge ok">tests</span><span class="ev-title">${n} tests (${esc(ev.source)})</span>
        ${ev.why ? `<span class="ev-meta">${esc(ev.why)}</span>` : ""}</div><details><summary>Show tests</summary><pre>${esc(ev.tests)}</pre></details>`);
      break;
    }
    case "run": {
      const r = ev.report;
      if (pendingDot) { pendingDot.className = "dot " + (ev.ok ? "ok" : "fail"); pendingDot = null; }
      const chips = r.tests.map((t) => `<span class="chip ${t.ok ? "" : "fail"}" title="${esc(t.error)}">${esc(t.name)}</span>`).join("");
      const head = r.fatal ? "Crashed" : `${r.passed}/${r.passed + r.failed} tests passed`;
      const failText = ev.ok ? "" : `<pre class="fail-text">${esc(ev.summary)}</pre>`;
      addCard("", `<div class="ev-head"><span class="badge ${ev.ok ? "ok" : "fail"}">sandbox</span>
        <span class="ev-title">${head}</span><span class="ev-meta">attempt ${ev.attempt} · ${r.duration_s}s</span></div>
        <div class="chips">${chips}</div>${failText}
        <details><summary>Code from this attempt</summary><pre>${esc(ev.code)}</pre></details>`);
      break;
    }
    case "escalate":
      addCard("escalate", `<div class="ev-head"><span class="badge warn">escalate</span>
        <span class="ev-title">${esc(ev.from)} → ${esc(ev.to)}</span><span class="ev-meta">${esc(ev.reason)}</span></div>`);
      break;
    case "error":
      addCard("error", `<div class="ev-head"><span class="badge fail">error</span><span class="ev-title">${esc(ev.message)}</span></div>`);
      break;
    case "done": done(ev); break;
  }
}

function done(ev) {
  totals.cost = ev.cost; totals.ultra = ev.ultra_equivalent_cost; totals.tok = ev.tokens_in + ev.tokens_out;
  renderStats();
  const saved = ev.ultra_equivalent_cost > 0 ? 1 - ev.cost / ev.ultra_equivalent_cost : 0;
  if (saved > 0.01) $("#s-cost").innerHTML = `${usd(ev.cost)}<small>−${Math.round(saved * 100)}%</small>`;
  $("#s-time").textContent = ev.wall_seconds.toFixed(1) + "s";
  const fin = $("#final");
  fin.classList.remove("hidden");
  fin.classList.toggle("failed", ev.status !== "solved");
  $("#final-title").textContent = ev.status === "solved"
    ? `✓ Solved by Nemotron ${ev.solved_by} in ${ev.attempts} attempt${ev.attempts > 1 ? "s" : ""}`
    : ev.status === "error" ? "Stopped by an error" : `✗ Not solved after ${ev.attempts} attempts (last code below)`;
  const code = $("#final-code");
  code.textContent = ev.code || "";
  code.removeAttribute("data-highlighted");
  if (window.hljs && ev.code) hljs.highlightElement(code);
  if (ev.status === "solved") setActive(ev.solved_by);
}

$("#copy").addEventListener("click", () => {
  navigator.clipboard?.writeText($("#final-code").textContent);
  $("#copy").textContent = "Copied"; setTimeout(() => ($("#copy").textContent = "Copy code"), 1200);
});

// ------------------------------------------------------------------ benchmark
async function loadBench() {
  const data = await fetch("/api/bench").then((r) => r.json());
  if (!data.available) return;
  $("#bench-meta").innerHTML = `${data.n_tasks} tasks (${Object.entries(data.difficulty_counts || {}).map(([k, v]) => `${v} ${k}`).join(", ")}).
    Correctness is scored on <b>hidden tests</b> the agent never sees. Run on ${esc(data.generated_at)}.`;
  const P = data.policies;
  const maxCost = Math.max(...P.map((p) => p.cost));
  const ultra = P.find((p) => p.name === "ultra-only");
  const ladder = P.find((p) => p.name === "ladder");
  let callouts = "";
  if (ultra && ladder) {
    const pct = ultra.cost > 0 ? Math.round((1 - ladder.cost / ultra.cost) * 100) : 0;
    callouts = `<div class="callouts">
      <div class="callout"><div class="big">${pct}%</div>cheaper than Ultra-only</div>
      <div class="callout"><div class="big">${Math.round(ladder.pass_rate * 100)}% vs ${Math.round(ultra.pass_rate * 100)}%</div>hidden-test pass rate, Ladder vs Ultra-only</div>
      <div class="callout"><div class="big">${ladder.solved_by?.nano ?? 0}/${ladder.solved}</div>Ladder solves finished on Nano without escalating</div>
    </div>`;
  }
  const diffs = Object.keys(data.difficulty_counts || {});
  const rows = P.map((p) => `<tr class="${p.name === "ladder" ? "hl" : ""}">
      <td><b>${esc(p.name)}</b></td>
      <td>${p.solved}/${p.n} <span class="muted">(${Math.round(p.pass_rate * 100)}%)</span></td>
      ${diffs.map((d) => `<td>${p.by_difficulty?.[d]?.solved ?? 0}/${p.by_difficulty?.[d]?.n ?? 0}</td>`).join("")}
      <td>${usd(p.cost)} <span class="bar" style="width:${Math.max(2, (p.cost / maxCost) * 90)}px"></span></td>
      <td>${usd(p.cost_per_solve)}</td>
      <td>${p.avg_wall.toFixed(1)}s</td>
      <td class="muted">${Object.entries(p.solved_by || {}).map(([k, v]) => `${k}:${v}`).join(" ")}</td></tr>`).join("");
  $("#bench-body").innerHTML = `${callouts}<div class="table-wrap"><table>
    <thead><tr><th>Policy</th><th>Solved</th>${diffs.map((d) => `<th>${esc(d)}</th>`).join("")}<th>Total cost</th><th>$ / solve</th><th>Avg time</th><th>Solved on</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${scatter(P)}`;
}

function scatter(P) {
  const W = 640, H = 300, m = 48;
  const maxC = Math.max(...P.map((p) => p.cost)) * 1.15 || 1;
  const x = (c) => m + (c / maxC) * (W - 2 * m);
  const y = (r) => H - m - r * (H - 2 * m);
  const col = { ladder: "var(--green)", "ladder-auto": "#a3e635", "nano-only": "var(--nano)", "super-only": "var(--super)", "ultra-only": "var(--ultra)" };
  const pts = P.map((p) => `<circle cx="${x(p.cost)}" cy="${y(p.pass_rate)}" r="7" fill="${col[p.name] || "#999"}"/>
    <text x="${x(p.cost) + 11}" y="${y(p.pass_rate) + 4}" fill="var(--text)" font-size="12">${esc(p.name)}</text>`).join("");
  const grid = [0, .25, .5, .75, 1].map((r) => `<line x1="${m}" x2="${W - m}" y1="${y(r)}" y2="${y(r)}" stroke="var(--line)"/>
    <text x="${m - 8}" y="${y(r) + 4}" text-anchor="end" fill="var(--muted)" font-size="11">${r * 100}%</text>`).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Pass rate versus total cost per policy">${grid}
    <text x="${W / 2}" y="${H - 10}" text-anchor="middle" fill="var(--muted)" font-size="12">total cost (USD) →  · up-and-left is better</text>
    <text x="${m}" y="${H - m + 18}" fill="var(--muted)" font-size="11">$0</text>
    <text x="${W - m}" y="${H - m + 18}" text-anchor="end" fill="var(--muted)" font-size="11">${usd(maxC)}</text>${pts}</svg></div>`;
}

init();
