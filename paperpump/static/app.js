// Shared helpers for every page. Plain JavaScript, no framework.
const $ = (s) => document.querySelector(s);

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { showLoggedOut(); throw new Error(data.detail || "Not logged in"); }
  if (!r.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function usd(v) {
  if (v === null || v === undefined) return "–";
  for (const [d, s] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (Math.abs(v) >= d) return "$" + (v / d).toFixed(1) + s;
  return "$" + Math.round(v).toLocaleString();
}
const sol = (v, n = 3) => (v === null || v === undefined ? "–" : v.toFixed(n));
const signed = (v, n = 3) => (v >= 0 ? "+" : "") + v.toFixed(n);
const cls = (v) => (v >= 0 ? "g" : "r");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.innerHTML = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
}

function showLoggedOut() {
  const m = $("main");
  if (m && !m.dataset.out) {
    m.dataset.out = 1;
    m.innerHTML = `<div class="card pad full" style="text-align:center;padding:40px">
      <h2>Log in through Telegram</h2><p class="mu">Open the PaperPump bot, tap <b>🌐 Open website</b> and use the link it sends you.</p></div>`;
  }
}

function nav(active) {
  document.body.insertAdjacentHTML("afterbegin", `
  <div class="banner">🧪 Paper trading only. Fake SOL, real prices. Not financial advice.</div>
  <div class="nav"><a class="logo" href="/">🧪 PaperPump</a>
    <div class="links">${[["/", "Trade"], ["/discover", "Discover"], ["/portfolio", "Portfolio"], ["/history", "History", "Trades"], ["/leaderboard", "Leaderboard", "Ranks"]]
      .map(([h, n, s]) => `<a href="${h}" class="${n === active ? "on" : ""}"><span class="ln">${n}</span><span class="sn">${s || n}</span></a>`).join("")}</div>
    <div class="sp"></div><span class="live" id="live" title="Data refreshes automatically">Live <i>●</i> <span id="ago"></span></span>
    <div class="pill" id="bal">… SOL (paper)</div></div>
  <div class="toast" id="toast"></div>`);
  live(refreshPortfolio, 5000);
  setInterval(() => { $("#ago").textContent = lastUpdate ? `updated ${Math.round((Date.now() - lastUpdate) / 1000)}s ago` : ""; }, 1000);
}

// ---------- Live updates ----------
// live(fn, ms): run fn now and every ms while the tab is visible; paused when hidden.
const pollers = [];
let lastUpdate = 0;
function live(fn, ms) {
  const p = { fn, ms, timer: null, busy: false };
  const tick = async () => {
    clearTimeout(p.timer);
    if (document.hidden) return;
    if (!p.busy) {
      p.busy = true;
      try { await fn(); lastUpdate = Date.now(); $("#live").classList.remove("off"); }
      catch (e) { $("#live").classList.add("off"); }
      p.busy = false;
    }
    p.timer = setTimeout(tick, p.ms);
  };
  p.tick = tick;
  pollers.push(p);
  tick();
  return p;
}
// Refresh everything now (call after your own buy/sell/TP/SL).
function liveNow() { pollers.forEach((p) => p.tick()); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) liveNow(); });

// setText: only touch the DOM when the value actually changed (no flicker).
function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
function setHTML(el, html) { if (el && el._html !== html) { el.innerHTML = html; el._html = html; } }

// Shared portfolio poll: updates the balance pill, notices changes made elsewhere
// (Telegram bot, TP/SL auto-sells) and tells the page via a "portfolio" event.
let portfolio = null, knownSig = null;
const quiet = { until: 0 }; // suppress toasts right after our own action
async function refreshPortfolio() {
  let pf;
  try { pf = await api("/api/portfolio"); } catch (e) { setText($("#bal"), "Not logged in"); throw e; }
  setText($("#bal"), `🧪 ${pf.balance.toFixed(2)} SOL (paper)`);
  const sig = Object.fromEntries(pf.positions.map((p) => [p.id, p]));
  if (knownSig && Date.now() > quiet.until) {
    for (const [id, old] of Object.entries(knownSig)) {
      const now = sig[id];
      if (!now) toast(`📕 Position $${esc(old.symbol)} closed`);
      else if (Math.abs(now.tokens - old.tokens) > 1e-9) toast(`🔄 Position $${esc(now.symbol)} changed`);
    }
    for (const [id, p] of Object.entries(sig)) if (!knownSig[id]) toast(`📗 New position $${esc(p.symbol)}`);
  }
  knownSig = sig;
  portfolio = pf;
  document.dispatchEvent(new CustomEvent("portfolio", { detail: pf }));
}
// Call right after your own trade: no "changed elsewhere" toast, refresh everything.
function afterAction() { quiet.until = Date.now() + 1500; knownSig = null; liveNow(); }
