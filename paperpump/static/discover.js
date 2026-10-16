// Discover feed widget (Trending / New pairs / Gainers), shared by the Trade and Discover pages.
// Reads the server's ONE cached feed (/api/discover); filters are saved per user on the server.
function mountDiscover(root, onOpen) {
  root.innerHTML = `
  <div class="card pad dtop">
    <div class="seg dtabs" role="tablist">
      <button data-tab="trending">🔥 Trending</button><button data-tab="new">🌱 New</button><button data-tab="gainers">🚀 Gainers</button>
    </div>
    <div class="dbar">
      <div class="seg sm dtfs"><button data-tf="m5">5m</button><button data-tf="h1">1h</button><button data-tf="h6">6h</button><button data-tf="h24">24h</button></div>
      <button class="btn sm dfbtn" aria-expanded="false">⚙️ Filters <span class="dfcount"></span></button>
    </div>
    <form class="fpanel" hidden>
      <div class="presets"></div>
      <div class="fgrid">
        <div><label>Min market cap ($)</label><input name="min_mc" type="number" inputmode="decimal" min="0"></div>
        <div><label>Max market cap ($)</label><input name="max_mc" type="number" inputmode="decimal" min="0"></div>
        <div><label>Min liquidity ($)</label><input name="min_liq" type="number" inputmode="decimal" min="0"></div>
        <div><label>Min volume ($, timeframe)</label><input name="min_vol" type="number" inputmode="decimal" min="0"></div>
        <div><label>Min age (minutes)</label><input name="min_age_m" type="number" inputmode="decimal" min="0"></div>
        <div><label>Max age (minutes)</label><input name="max_age_m" type="number" inputmode="decimal" min="0"></div>
        <div><label>Min % change (timeframe)</label><input name="min_change" type="number" inputmode="decimal"></div>
      </div>
      <div class="factions"><button type="button" class="btn sm dreset">Reset</button><button class="btn primary sm">Apply &amp; save</button></div>
    </form>
  </div>
  <div class="card dcard"><div class="dlist"><div class="empty" style="padding:40px">Loading the feed…</div></div><div class="pad mu dfoot"></div></div>`;
  const q = (s) => root.querySelector(s);
  const S = JSON.parse(localStorage.getItem("pp_disc") || '{"tab":"trending","tf":"h1"}');
  const TFL = { m5: "5m", h1: "1h", h6: "6h", h24: "24h" };
  const pctTxt = (v) => (v > 0 ? "+" : "") + (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(1)) + "%";
  const ageTxt = (m) => m == null ? "?" : m < 60 ? Math.round(m) + "m" : m < 1440 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d";
  const num = (n) => n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K" : String(n);
  const mark = (sel, attr, val) => root.querySelectorAll(sel + " button").forEach((b) => b.classList.toggle("on", b.dataset[attr] === val));
  const save = () => localStorage.setItem("pp_disc", JSON.stringify({ tab: S.tab, tf: S.tf }));
  let presets = {}, filters = {}, poller;
  mark(".dtabs", "tab", S.tab); mark(".dtfs", "tf", S.tf);
  q(".dtabs").onclick = (e) => { const t = e.target.dataset.tab; if (t) { S.tab = t; save(); mark(".dtabs", "tab", t); poller.tick(); } };
  q(".dtfs").onclick = (e) => { const t = e.target.dataset.tf; if (t) { S.tf = t; save(); mark(".dtfs", "tf", t); poller.tick(); } };
  q(".dfbtn").onclick = () => { const p = q(".fpanel"); p.hidden = !p.hidden; q(".dfbtn").setAttribute("aria-expanded", !p.hidden); };
  const fillForm = (f) => { for (const el of q(".fpanel").querySelectorAll("input")) el.value = f[el.name] ?? ""; };
  const showCount = () => { const n = Object.keys(filters).length; setText(q(".dfcount"), n ? `(${n})` : ""); };
  async function saveFilters(f) {
    try { filters = await api("/api/discover/filters", f); fillForm(filters); showCount(); poller.tick(); toast("Filters saved"); }
    catch (e) { toast("❌ " + esc(e.message)); }
  }
  q(".fpanel").onsubmit = (e) => {
    e.preventDefault();
    const f = {}; for (const el of e.target.querySelectorAll("input")) if (el.value !== "") f[el.name] = +el.value;
    saveFilters(f);
  };
  q(".dreset").onclick = () => saveFilters({});
  q(".presets").onclick = (e) => { const k = e.target.dataset.p; if (k) saveFilters(presets[k]); };
  q(".dlist").onclick = (e) => { const r = e.target.closest("[data-mint]"); if (r) onOpen(r.dataset.mint); };

  poller = live(async () => {
    if (root.hidden) return;  // collapsed (a coin is open): no polling
    const d = await api(`/api/discover?tab=${S.tab}&tf=${S.tf}`);
    if (!Object.keys(presets).length) {
      presets = d.presets;
      q(".presets").innerHTML = Object.keys(presets).map((k) => `<button type="button" class="btn sm" data-p="${esc(k)}">${esc(k[0].toUpperCase() + k.slice(1))}</button>`).join("");
      filters = d.filters; fillForm(filters); showCount();
    }
    const tf = S.tf;
    setHTML(q(".dlist"), d.rows.length ? d.rows.map((r) => `
      <button type="button" class="drow" data-mint="${esc(r.mint)}">
        ${r.icon ? `<img class="ic" src="${esc(r.icon)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="ic ph">${esc(r.symbol.slice(0, 1))}</span>`}
        <span class="dn"><b>${esc(r.symbol)}</b><span class="mu">${esc(r.name)}</span></span>
        <span class="dc ${r.change[tf] >= 0 ? "g" : "r"}">${pctTxt(r.change[tf])}<i>${TFL[tf]}</i></span>
        <span class="dm"><span><i>MC</i>${usd(r.market_cap)}</span><span><i>Liq</i>${usd(r.liquidity_usd)}</span><span><i>Vol</i>${usd(r.volume[tf])}</span><span><i>Txns</i>${num(r.txns[tf])}</span><span><i>Age</i>${ageTxt(r.age_m)}</span></span>
      </button>`).join("") : `<div class="empty" style="padding:40px">${d.total ? "No coins match your filters." : "The feed is loading or the data source is busy. It retries automatically."}</div>`);
    setText(q(".dfoot"), `${d.rows.length} of ${d.total} coins · Solana SOL pairs, liquidity ≥ $1K · shared feed refreshes every ~${Math.round(d.refresh_s)}s` +
      (d.age_s != null ? ` · updated ${Math.round(d.age_s)}s ago` : ""));
  }, 30000);
  return poller;
}
