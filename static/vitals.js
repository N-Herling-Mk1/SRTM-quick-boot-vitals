/* ==========================================================================
   srtm-quickboot-vitals :: vitals.js
   Polls /api/vitals, renders the three clocks, the canary strip, and the
   channel grid. Progress state is always visible -- never a silent wait.
   ========================================================================== */

const POLL_SEC = parseInt(document.body.dataset.poll || "5", 10);

const el = {
  lamp:    document.getElementById("lamp"),
  lampTxt: document.getElementById("lampTxt"),
  vPoll:   document.getElementById("vPoll"),
  vSrc:    document.getElementById("vSrc"),
  clkPoll: document.getElementById("clkPoll"),
  clkSrc:  document.getElementById("clkSrc"),
  canary:  document.getElementById("canaryRow"),
  grid:    document.getElementById("grid"),
  bar:     document.getElementById("bar"),
  barTxt:  document.getElementById("barTxt"),
  err:     document.getElementById("err"),
  ftrMeta: document.getElementById("ftrMeta"),
  filters: document.getElementById("filters"),
};

let filter = "ALL";
let latest = null;
let tick = 0;
let inflight = false;

/* ------------------------------------------------------------- formatting */
const secs = (v) => (v === null || v === undefined) ? "\u2014" : `${v.toFixed(1)}s`;

function sig(v) {
  if (v === null || v === undefined) return "\u2014";
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e6)) return v.toExponential(2);
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(3);
}

function shortVal(v) {
  if (v === null || v === undefined) return "\u2014";
  const s = String(v);
  const n = parseFloat(s);
  if (!Number.isNaN(n) && s.trim() !== "" && /^-?[\d.eE+]+$/.test(s.trim())) {
    return sig(n);
  }
  return s.length > 16 ? s.slice(0, 15) + "\u2026" : s;
}

const LAMP = {
  live:    ["live",   "PIPELINE LIVE"],
  watch:   ["watch",  "WATCH"],
  frozen:  ["frozen", "SOURCE FROZEN"],
  dead:    ["dead",   "PIPELINE DEAD"],
  warming: ["watch",  "WARMING UP"],
  unknown: ["watch",  "NO CANARIES"],
};

/* ----------------------------------------------------------------- render */
function render(d) {
  latest = d;
  el.err.hidden = true;

  const c = d.clocks;

  el.vPoll.textContent = secs(c.t_poll);
  el.clkPoll.dataset.state = c.t_poll_state;

  const cans = d.channels.filter((x) => x.canary);
  const worst = cans.reduce((acc, x) => {
    const rank = { stale: 3, watch: 2, warming: 1, live: 0 };
    return (rank[x.verdict] ?? 0) > (rank[acc] ?? 0) ? x.verdict : acc;
  }, "live");
  const oldest = cans.reduce(
    (m, x) => (x.t_change !== null && (m === null || x.t_change > m) ? x.t_change : m),
    null
  );
  el.vSrc.textContent = oldest === null ? "\u2014" : secs(oldest);
  el.clkSrc.dataset.state =
    worst === "stale" ? "frozen" : worst === "watch" ? "watch"
    : worst === "warming" ? "warming" : "live";

  // overall lamp -- dead pipeline dominates a frozen source
  let overall = c.t_poll_state === "dead" ? "dead" : c.source_state;
  const [ls, lt] = LAMP[overall] || LAMP.unknown;
  el.lamp.dataset.state = ls;
  el.lampTxt.textContent = lt;

  // canary strip
  el.canary.innerHTML = cans.length
    ? cans.map((x) => `
        <div class="can" data-v="${x.verdict}">
          <div class="can-f">${x.field}</div>
          <div class="can-v">${shortVal(x.value)}</div>
          <div class="can-t">t_change ${secs(x.t_change)} &middot; n=${x.n}</div>
        </div>`).join("")
    : `<div class="empty">no canary channels present in this window</div>`;

  // channel grid
  const flagged = new Set(["stale", "watch", "flat"]);
  const rows = d.channels.filter((x) =>
    filter === "ALL" ? true
    : filter === "ALARM" ? flagged.has(x.verdict)
    : x.cls === filter
  );

  el.grid.innerHTML = rows.length
    ? rows.map((x) => {
        const stats = x.numeric && x.mean !== null
          ? `<b>&mu;</b> ${sig(x.mean)} &plusmn; ${sig(x.sigma)}`
          : `<b>&mu;</b> n/a`;
        const right = x.verdict === "warming"
          ? `warming ${x.n}/${d.min_samples}`
          : `t&Delta; ${secs(x.t_change)}`;
        return `
          <div class="ch" data-v="${x.verdict}" title="${x.field} \u2014 ${x.cls}">
            <div class="ch-top">
              <span class="ch-f">${x.field}</span>
              <span class="ch-c">${x.cls}</span>
            </div>
            <div class="ch-v">${shortVal(x.value)}</div>
            <div class="ch-s"><span>${stats}</span><span>${right}</span></div>
          </div>`;
      }).join("")
    : `<div class="empty">no channels match this filter</div>`;

  const flaggedN = d.channels.filter((x) => flagged.has(x.verdict)).length;
  el.ftrMeta.textContent =
    `${rows.length}/${d.channels.length} shown \u00b7 ${flaggedN} flagged \u00b7 ` +
    `window ${d.window_min}m \u00b7 updated ${new Date().toLocaleTimeString()}`;
}

/* ------------------------------------------------------------------ fetch */
async function poll() {
  if (inflight) return;
  inflight = true;
  el.barTxt.textContent = "querying influxdb\u2026";
  try {
    const r = await fetch("/api/vitals", { cache: "no-store" });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    render(d);
    el.barTxt.textContent = `last poll ok \u00b7 next in ${POLL_SEC}s`;
  } catch (e) {
    el.err.hidden = false;
    el.err.textContent = `QUERY FAILED \u2014 ${e.message}`;
    el.lamp.dataset.state = "dead";
    el.lampTxt.textContent = "NO DATA";
    el.barTxt.textContent = "last poll failed \u00b7 retrying";
  } finally {
    inflight = false;
    tick = 0;
  }
}

/* --------------------------------------------------------------- progress */
setInterval(() => {
  tick += 0.25;
  const pct = Math.min(100, (tick / POLL_SEC) * 100);
  el.bar.style.width = pct + "%";
  if (tick >= POLL_SEC) poll();
}, 250);

/* ---------------------------------------------------------------- filters */
el.filters.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  [...el.filters.querySelectorAll("button")].forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  filter = b.dataset.f;
  if (latest) render(latest);
});

poll();
