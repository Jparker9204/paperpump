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
    <div class="links">${[["/", "Trade"], ["/portfolio", "Portfolio"], ["/history", "History"], ["/leaderboard", "Leaderboard"]]
      .map(([h, n]) => `<a href="${h}" class="${n === active ? "on" : ""}">${n}</a>`).join("")}</div>
    <div class="sp"></div><div class="pill" id="bal">… SOL (paper)</div></div>
  <div class="toast" id="toast"></div>`);
  refreshBalance();
}

async function refreshBalance() {
  try { const me = await api("/api/me"); $("#bal").textContent = `🧪 ${me.balance.toFixed(2)} SOL (paper)`; return me; }
  catch (e) { $("#bal").textContent = "Not logged in"; }
}
