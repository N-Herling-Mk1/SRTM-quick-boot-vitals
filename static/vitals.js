/* ==========================================================================
   srtm-quickboot-vitals :: vitals.js   rev 2
   Poll -> render three clocks, canary strip, searchable channel grid.
   Click any channel for a detail view with a canvas time series.
   No charting library: one dependency-free canvas draw, so nothing to
   install on a box where pip is already a minefield.
   ========================================================================== */

const POLL_SEC = parseInt(document.body.dataset.poll || "5", 10);

const el = {
  lamp: document.getElementById("lamp"),
  lampTxt: document.getElementById("lampTxt"),
  vPoll: document.getElementById("vPoll"),
  vSrc: document.getElementById("vSrc"),
  clkPoll: document.getElementById("clkPoll"),
  clkSrc: document.getElementById("clkSrc"),
  canary: document.getElementById("canaryRow"),
  grid: document.getElementById("grid"),
  bar: document.getElementById("bar"),
  barTxt: document.getElementById("barTxt"),
  err: document.getElementById("err"),
  ftrMeta: document.getElementById("ftrMeta"),
  filters: document.getElementById("filters"),
  search: document.getElementById("search"),
  ov: document.getElementById("ov"),
  dtName: document.getElementById("dtName"),
  dtSub: document.getElementById("dtSub"),
  dtStats: document.getElementById("dtStats"),
  dtRng: document.getElementById("dtRng"),
  dtNote: document.getElementById("dtNote"),
  dtX: document.getElementById("dtX"),
  plot: document.getElementById("plot"),
  heatGrid: document.getElementById("heatGrid"),
  heatLegend: document.getElementById("heatLegend"),
  heatTip: document.getElementById("heatTip"),
  helpBtn: document.getElementById("helpBtn"),
  helpOv: document.getElementById("helpOv"),
  helpX: document.getElementById("helpX"),
};

const HEAT_LABEL = {
  green:  "on schedule",
  yellow: "lagging",
  orange: "well overdue",
  red:    "effectively stopped",
  static: "identity string \u2014 never changes",
  nobase: "no baseline \u2014 run tools/build_baseline.py",
};

let filter = "ALL";
let query = "";
let latest = null;
let tick = 0;
let inflight = false;
let openField = null;
let openMinutes = 30;

const FLAGGED = new Set(["stale", "watch", "flat"]);

/* ------------------------------------------------------------ formatting */
const secs = (v) => (v === null || v === undefined) ? "\u2014" : `${v.toFixed(1)}s`;

function sig(v) {
  if (v === null || v === undefined) return "\u2014";
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e6)) return v.toExponential(2);
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(3);
}

function shortVal(v, max = 16) {
  if (v === null || v === undefined) return "\u2014";
  const s = String(v);
  if (/^-?[\d.eE+]+$/.test(s.trim()) && s.trim() !== "") {
    const n = parseFloat(s);
    if (!Number.isNaN(n)) return sig(n);
  }
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const LAMP = {
  live: ["live", "PIPELINE LIVE"],
  watch: ["watch", "WATCH"],
  frozen: ["frozen", "SOURCE FROZEN"],
  dead: ["dead", "PIPELINE DEAD"],
  warming: ["watch", "WARMING UP"],
  unknown: ["watch", "NO CANARIES"],
};

/* ---------------------------------------------------------------- render */
function visible(d) {
  return d.channels.filter((x) => {
    if (query && !x.field.toLowerCase().includes(query)) return false;
    if (filter === "ALL") return true;
    if (filter === "ALARM") return FLAGGED.has(x.verdict);
    return x.cls === filter;
  });
}

function render(d) {
  latest = d;
  el.err.hidden = true;
  const c = d.clocks;

  el.vPoll.textContent = secs(c.t_poll);
  el.clkPoll.dataset.state = c.t_poll_state;

  const cans = d.channels.filter((x) => x.canary);
  const rank = { stale: 3, watch: 2, warming: 1, live: 0 };
  const worst = cans.reduce(
    (acc, x) => ((rank[x.verdict] ?? 0) > (rank[acc] ?? 0) ? x.verdict : acc), "live");
  const oldest = cans.reduce(
    (m, x) => (x.t_change !== null && (m === null || x.t_change > m) ? x.t_change : m), null);

  el.vSrc.textContent = oldest === null ? "\u2014" : secs(oldest);
  el.clkSrc.dataset.state =
    worst === "stale" ? "frozen" : worst === "watch" ? "watch"
      : worst === "warming" ? "warming" : "live";

  const overall = c.t_poll_state === "dead" ? "dead" : c.source_state;
  const [ls, lt] = LAMP[overall] || LAMP.unknown;
  el.lamp.dataset.state = ls;
  el.lampTxt.textContent = lt;

  el.canary.innerHTML = cans.length
    ? cans.map((x) => `
        <div class="can" data-v="${x.verdict}" data-field="${esc(x.field)}">
          <div class="can-f">${esc(x.field)}</div>
          <div class="can-v">${esc(shortVal(x.value))}</div>
          <div class="can-t">t&Delta; ${secs(x.t_change)} &middot; n=${x.n}</div>
        </div>`).join("")
    : `<div class="empty">no canary channels in this window</div>`;

  // ---- cadence map -------------------------------------------------------
  const hc = d.heat || {};
  const cuts = d.heat_cuts || { yellow: 2, orange: 5, red: 20 };
  el.heatLegend.innerHTML = [
    ["g", "green", `\u2264${cuts.yellow}\u00d7`],
    ["y", "yellow", `\u2264${cuts.orange}\u00d7`],
    ["o", "orange", `\u2264${cuts.red}\u00d7`],
    ["r", "red", `>${cuts.red}\u00d7`],
    ["s", "static", ""],
    ["n", "nobase", ""],
  ].filter(([, k]) => (hc[k] || 0) > 0)
   .map(([cls, k, rng]) =>
      `<span class="lg"><i class="${cls}"></i>${hc[k]} ${k}${rng ? " " + rng : ""}</span>`)
   .join("");

  el.heatGrid.innerHTML = d.channels.map((x) => {
    const r = x.ratio === null || x.ratio === undefined ? "" : ` ${x.ratio}\u00d7`;
    return `<div class="hc" tabindex="0" data-h="${x.heat}" data-field="${esc(x.field)}"
      data-tip="${esc(x.field)}|${x.cls}|${x.heat}|${r}|${secs(x.t_change)}|${
        x.baseline ? x.baseline.toFixed(1) + "s" : "\u2014"}"></div>`;
  }).join("");

  const rows = visible(d);
  el.grid.innerHTML = rows.length
    ? rows.map((x) => {
        const stats = x.numeric && x.mean !== null
          ? `<b>&mu;</b> ${sig(x.mean)} &plusmn; ${sig(x.sigma)}`
          : `<b>&mu;</b> n/a`;
        const right = x.verdict === "warming"
          ? `warming ${x.n}/${d.min_samples}`
          : `t&Delta; ${secs(x.t_change)}`;
        return `
          <div class="ch" data-v="${x.verdict}" data-field="${esc(x.field)}"
               title="${esc(x.field)} \u2014 ${x.cls} \u2014 click for detail">
            <div class="ch-top">
              <span class="ch-f">${esc(x.field)}</span>
              <span class="ch-c">${x.cls}</span>
            </div>
            <div class="ch-v">${esc(shortVal(x.value))}</div>
            <div class="ch-s"><span>${stats}</span><span>${right}</span></div>
          </div>`;
      }).join("")
    : `<div class="empty">nothing matches ${query ? `"${esc(query)}"` : "this filter"}</div>`;

  const flaggedN = d.channels.filter((x) => FLAGGED.has(x.verdict)).length;
  el.ftrMeta.textContent =
    `${rows.length}/${d.channels.length} shown \u00b7 ${flaggedN} flagged \u00b7 ` +
    `window ${d.window_min}m \u00b7 ${new Date().toLocaleTimeString()}` +
    (d.baseline_fields ? "" : "  \u2014 NO BASELINES: run tools/build_baseline.py");
}

/* ----------------------------------------------------------------- fetch */
async function poll() {
  if (inflight) return;
  inflight = true;
  el.barTxt.textContent = "querying influxdb\u2026";
  try {
    const r = await fetch("api/vitals", { cache: "no-store" });
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

setInterval(() => {
  tick += 0.25;
  el.bar.style.width = Math.min(100, (tick / POLL_SEC) * 100) + "%";
  if (tick >= POLL_SEC) poll();
}, 250);

/* --------------------------------------------------------------- detail */
function statCell(k, v) {
  return `<div class="st"><div class="st-k">${k}</div><div class="st-v">${v}</div></div>`;
}

async function openDetail(field, minutes) {
  openField = field;
  openMinutes = minutes;
  el.ov.hidden = false;
  el.dtName.textContent = field;
  el.dtSub.textContent = "loading\u2026";
  el.dtStats.innerHTML = "";
  el.dtNote.textContent = "";
  [...el.dtRng.querySelectorAll("button")].forEach((b) =>
    b.classList.toggle("on", parseInt(b.dataset.m, 10) === minutes));

  try {
    const r = await fetch(`api/channel/${encodeURIComponent(field)}?minutes=${minutes}`,
      { cache: "no-store" });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    if (openField !== field) return;   // user moved on mid-flight

    el.dtSub.textContent =
      `${d.cls}${d.canary ? " \u00b7 CANARY" : ""} \u00b7 ${d.n} samples over ${d.minutes}m`;

    el.dtStats.innerHTML = [
      statCell("CURRENT", esc(shortVal(d.raw_last, 22))),
      statCell("MEAN", sig(d.mean)),
      statCell("SIGMA", sig(d.sigma)),
      statCell("MIN", sig(d.min)),
      statCell("MAX", sig(d.max)),
      statCell("T_CHANGE", secs(d.t_change)),
    ].join("");

    drawPlot(d);
    el.dtNote.textContent = d.numeric
      ? (d.notes || "")
      : "Non-numeric channel \u2014 no plot or statistics. " + (d.notes || "");
  } catch (e) {
    el.dtSub.textContent = `failed \u2014 ${e.message}`;
    clearPlot();
  }
}

function ctxSetup() {
  const cv = el.plot;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  return { g, w, h };
}

function clearPlot() {
  const { g, w, h } = ctxSetup();
  g.fillStyle = "#5f7d8c";
  g.font = "12px 'Share Tech Mono', monospace";
  g.textAlign = "center";
  g.fillText("no data", w / 2, h / 2);
}

function drawPlot(d) {
  const pts = (d.series || []).filter((p) => p[1] !== null && !Number.isNaN(p[1]));
  if (!d.numeric || pts.length < 2) return clearPlot();

  const { g, w, h } = ctxSetup();
  const PAD = { l: 62, r: 12, t: 14, b: 26 };
  const iw = w - PAD.l - PAD.r, ih = h - PAD.t - PAD.b;

  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }          // flat channel still renders
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;

  const X = (v) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * iw;
  const Y = (v) => PAD.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

  g.strokeStyle = "#b6cdd7"; g.lineWidth = 1;
  g.fillStyle = "#5f7d8c";
  g.font = "10px 'Share Tech Mono', monospace";
  g.textAlign = "right"; g.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const yv = y0 + ((y1 - y0) * i) / 4, py = Y(yv);
    g.beginPath(); g.moveTo(PAD.l, py); g.lineTo(w - PAD.r, py); g.stroke();
    g.fillText(sig(yv), PAD.l - 7, py);
  }

  // +/- 1 sigma band around the mean -- the liveness signal, made visible
  if (d.mean !== null && d.sigma !== null && d.sigma > 0) {
    const top = Y(d.mean + d.sigma), bot = Y(d.mean - d.sigma);
    g.fillStyle = "rgba(2,102,120,.10)";
    g.fillRect(PAD.l, Math.min(top, bot), iw, Math.abs(bot - top));
    g.strokeStyle = "rgba(2,102,120,.5)";
    g.setLineDash([4, 4]); g.beginPath();
    g.moveTo(PAD.l, Y(d.mean)); g.lineTo(w - PAD.r, Y(d.mean));
    g.stroke(); g.setLineDash([]);
  }

  g.strokeStyle = "#026678"; g.lineWidth = 1.6;
  g.beginPath();
  pts.forEach((p, i) => (i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))));
  g.stroke();

  const last = pts[pts.length - 1];
  g.fillStyle = "#ff6b1a";
  g.beginPath(); g.arc(X(last[0]), Y(last[1]), 3.2, 0, Math.PI * 2); g.fill();

  g.fillStyle = "#5f7d8c"; g.textBaseline = "top";
  g.textAlign = "left";
  g.fillText(new Date(x0).toLocaleTimeString(), PAD.l, h - PAD.b + 7);
  g.textAlign = "right";
  g.fillText(new Date(x1).toLocaleTimeString(), w - PAD.r, h - PAD.b + 7);
}

function closeDetail() {
  el.ov.hidden = true;
  openField = null;
}

/* --------------------------------------------------------------- events */
function wireClick(node) {
  node.addEventListener("click", (e) => {
    const card = e.target.closest("[data-field]");
    if (card) openDetail(card.dataset.field, openMinutes);
  });
}
wireClick(el.grid);
wireClick(el.canary);
wireClick(el.heatGrid);

function showTip(cell, x, y) {
  const [f, cls, heat, ratio, tchg, base] = (cell.dataset.tip || "").split("|");
  el.heatTip.innerHTML =
    `<b>${f}</b><br>${cls} \u00b7 ${heat}${ratio ? " " + ratio : ""} ` +
    `\u2014 ${HEAT_LABEL[heat] || ""}<br>t\u0394 ${tchg} \u00b7 baseline ${base}`;
  el.heatTip.hidden = false;
  const r = el.heatTip.getBoundingClientRect();
  el.heatTip.style.left =
    Math.min(x + 14, window.innerWidth - r.width - 10) + "px";
  el.heatTip.style.top =
    (y + r.height + 20 > window.innerHeight ? y - r.height - 12 : y + 16) + "px";
}

el.heatGrid.addEventListener("mousemove", (e) => {
  const c = e.target.closest(".hc");
  if (c) showTip(c, e.clientX, e.clientY); else el.heatTip.hidden = true;
});
el.heatGrid.addEventListener("mouseleave", () => { el.heatTip.hidden = true; });
el.heatGrid.addEventListener("focusin", (e) => {
  const c = e.target.closest(".hc");
  if (!c) return;
  const b = c.getBoundingClientRect();
  showTip(c, b.left, b.bottom);
});
el.heatGrid.addEventListener("focusout", () => { el.heatTip.hidden = true; });
el.heatGrid.addEventListener("keydown", (e) => {
  const c = e.target.closest(".hc");
  if (c && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openDetail(c.dataset.field, openMinutes);
  }
});

el.helpBtn.addEventListener("click", () => { el.helpOv.hidden = false; });
el.helpX.addEventListener("click", () => { el.helpOv.hidden = true; });
el.helpOv.addEventListener("click", (e) => {
  if (e.target === el.helpOv) el.helpOv.hidden = true;
});

el.dtX.addEventListener("click", closeDetail);
el.ov.addEventListener("click", (e) => { if (e.target === el.ov) closeDetail(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeDetail(); el.helpOv.hidden = true; }
  if (e.key === "?" && document.activeElement !== el.search) el.helpOv.hidden = false;
  if (e.key === "/" && document.activeElement !== el.search) {
    e.preventDefault(); el.search.focus();
  }
});

el.dtRng.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b && openField) openDetail(openField, parseInt(b.dataset.m, 10));
});

el.filters.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  [...el.filters.querySelectorAll("button")].forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  filter = b.dataset.f;
  if (latest) render(latest);
});

el.search.addEventListener("input", () => {
  query = el.search.value.trim().toLowerCase();
  if (latest) render(latest);
});

window.addEventListener("resize", () => {
  if (openField && !el.ov.hidden) openDetail(openField, openMinutes);
});

poll();
