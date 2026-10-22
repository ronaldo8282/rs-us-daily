/* TPO Letter Hourglass MVP — shared price scale (TV-style) + zoom + virtualized columns */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const RANGE_PRESETS = {
    "12m": { sessions: 252, lookbackDays: 400, yahooRange: "1y", label: "12M (~252)" },
    "6m": { sessions: 126, lookbackDays: 220, yahooRange: "6mo", label: "6M (~126)" },
    "3m": { sessions: 63, lookbackDays: 110, yahooRange: "3mo", label: "3M (~63)" },
    "1m": { sessions: 21, lookbackDays: 45, yahooRange: "1mo", label: "1M (~21)" },
  };
  const DEFAULT_RANGE = "3m";

  const FIRST_PAINT = 80; // enough for 60d overview on first paint
  const FILL_CHUNK = 40;
  const BASE_ROW_H = 13;
  const HEAD_H = 44;
  const HEAD_H_THIN = 26;
  const AXIS_W = 56;
  const OVERSCAN_MIN = 4;
  const ZOOM_MIN = 0.02; // allow ~60 thin columns in a typical viewport
  const ZOOM_MAX = 2.5;
  const RIGHT_EDGE_PAD = 260; // blank space after newest day so latest HG is readable

  // Static GitHub Pages mode: load bundled data/profiles.json (no Flask).
  const STATIC_MODE =
    !!(typeof window !== "undefined" && window.TPO_STATIC) ||
    /[?&]static=1(?:&|$)/.test(location.search) ||
    location.protocol === "file:" ||
    /(?:^|\.)github\.io$/i.test(location.hostname) ||
    /(?:^|\.)github\.io(?::\d+)?$/i.test(String(location.host || ""));

  function staticProfilesUrl() {
    try {
      return new URL("data/profiles.json", document.baseURI || location.href).href;
    } catch (_) {
      return "data/profiles.json";
    }
  }

  async function readResponseJson(res, label) {
    const ct = (res.headers && res.headers.get("content-type")) || "";
    const text = await res.text();
    const trimmed = (text || "").replace(/^\uFEFF/, "").trimStart();
    const looksHtml =
      trimmed.startsWith("<") ||
      /^<!DOCTYPE/i.test(trimmed) ||
      /^<html/i.test(trimmed);
    const ctOk = /json/i.test(ct);
    if (looksHtml || (!ctOk && trimmed && !trimmed.startsWith("{") && !trimmed.startsWith("["))) {
      throw new Error(
        "靜態版無 API（" +
          (label || res.url || "response") +
          " 回咗 HTML）。請用 data/profiles.json，或喺本地 Flask 跑。"
      );
    }
    try {
      return JSON.parse(text);
    } catch (e) {
      throw new Error(
        "靜態版無 API／JSON 解析失敗（" +
          (label || "response") +
          "）：" +
          (e.message || e)
      );
    }
  }

  async function loadStaticProfiles() {
    const urls = [staticProfilesUrl(), "data/profiles.json", "./data/profiles.json"];
    // de-dupe
    const seen = new Set();
    const list = [];
    for (const u of urls) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      list.push(u);
    }
    let lastErr = null;
    for (const url of list) {
      try {
        const res = await fetch(url, { cache: "no-cache" });
        if (!res.ok) throw new Error("HTTP " + res.status + " for " + url);
        return await readResponseJson(res, url);
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr || new Error("static profiles.json not found");
  }

  // Full static snapshot (all baked sessions); range presets slice locally.
  let staticBundle = null;

  function applyRangeToStatic(bundle, preset) {
    const allIds = (bundle && bundle.sessionIds) || [];
    const want = Math.max(1, (preset && preset.sessions) || RANGE_PRESETS[DEFAULT_RANGE].sessions);
    const slicedIds = allIds.length > want ? allIds.slice(-want) : allIds.slice();
    const idSet = new Set(slicedIds);
    const byId = new Map();
    for (const p of (bundle && bundle.profiles) || []) {
      if (p && p.sessionId && idSet.has(p.sessionId)) byId.set(p.sessionId, p);
    }
    const profiles = slicedIds.map((id) => byId.get(id)).filter(Boolean);
    const available = allIds.length;
    const short = available < want;
    const baseNote = (bundle && bundle.note) || "";
    const rangeNote =
      "Range " +
      ((preset && preset.label) || want) +
      " · showing " +
      profiles.length +
      "/" +
      want +
      (short ? " (baked " + available + " available — history short)" : "");
    return Object.assign({}, bundle, {
      sessionIds: slicedIds,
      profiles: profiles,
      returned: profiles.length,
      total: available,
      wantSessions: want,
      availableSessions: available,
      note: rangeNote + (baseNote ? " · " + baseNote : ""),
    });
  }

  // ---- state ----
  let sessionIds = []; // oldest → newest
  let profileById = new Map();
  let lastMeta = null;
  let renderScheduled = false;
  let fillInFlight = false;
  let profilesLoading = false;
  let lastParamsKey = "";
  let zoomX = 1; // column / prints density (charW)
  let zoomY = 1; // row height / font height
  let linkAxes = true; // default linked = old single-zoom feel
  let globalGrid = null; // { minKey, maxKey, step, nBins }
  let scrollSyncing = false;
  let lastGeo = null; // { widths, offsets, totalW, gap, axisW, rh, hh, bodyH, barMode }
  let crosshair = { sid: null, price: null, key: null, localX: 0, localY: 0, visible: false, step: 5, colIdx: null };
  // Press-drag on HG: vertical → adjust price level; horizontal pan → clear. Scroll also clears.
  let chDrag = null; // { pointerId, startX, startY, mode:'price'|'pan'|'track'|null, lockColIdx, lockSid, lockLocalX }
  // P3: drag left price axis → zoomY only (TV-style). Auto-unlinks.
  let yAxisDrag = null; // { pointerId, startY, zoomY0, anchorX, anchorY }
  let singlePinSegments = []; // { startIdx, endIdx, key, price, sid, step, extendsToPad }

  // Composite HG overlays: Day → Week → Month; custom multi-range exclusive with week/month
  let weekHgOn = false;
  let monthHgOn = false;
  let rangeGroups = []; // [{ id, startIdx, endIdx }] completed day merges (non-overlapping)
  let rangeAnchorIdx = null; // first tap while selecting next group
  let rangeDraft = null; // { startIdx, endIdx } while selecting (may be single day)
  let nextRangeId = 1;
  const compositeCache = new Map(); // key -> merged profile
  const RANGE_COLOR_N = 5;


  function showErr(msg) {
    const el = $("err");
    if (!msg) {
      el.classList.remove("show");
      el.textContent = "";
      return;
    }
    el.textContent = msg;
    el.classList.add("show");
  }

  function showSrcNote(msg) {
    const el = $("srcNote");
    if (!msg) {
      el.classList.remove("show");
      el.textContent = "";
      return;
    }
    el.textContent = msg;
    el.classList.add("show");
  }

  function currentRange() {
    const mode = $("rangeMode").value || DEFAULT_RANGE;
    return RANGE_PRESETS[mode] || RANGE_PRESETS[DEFAULT_RANGE];
  }

  function wantAlign() {
    return !!($("alignPrices") && $("alignPrices").checked);
  }

  function setAlignDefaultForRange() {
    // Align ON for all multi-month walls
    if ($("alignPrices")) $("alignPrices").checked = true;
  }

  function fmtPrice(p, step) {
    const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
    return Number(p).toFixed(decimals);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Per-day width from max TPO letter string length; zoom scales ch + padding.
  const CHAR_W0 = 7.2; // monospace px at zoom=1 (~11px font)
  const COL_PAD0 = 10; // left+right padding inside column at zoom=1
  const COL_MIN_PRINTS = 2; // empty / placeholder floor
  const PLACEHOLDER_PRINTS = 6;

  function maxPrintsOf(profile) {
    if (!profile || !profile.rows || !profile.rows.length) return COL_MIN_PRINTS;
    let m = 0;
    for (let i = 0; i < profile.rows.length; i++) {
      const r = profile.rows[i];
      // Prefer true TPO density when present (composites); else letter/count
      const n =
        r.density != null
          ? r.density
          : (r.letters && r.letters.length) || r.count || 0;
      if (n > m) m = n;
    }
    // also consider POC row
    if (profile.poc) {
      const pn =
        profile.poc.density != null
          ? profile.poc.density
          : profile.poc.count || 0;
      if (pn > m) m = pn;
    }
    return Math.max(COL_MIN_PRINTS, m);
  }

  /** Bar-width print count: composite density when set, else letters/count. */
  function densityCountOf(row) {
    if (!row) return 0;
    if (row.density != null) return row.density;
    return printCountOf(row);
  }

  /**
   * Density silhouette width in px.
   * No percentage floor: on wide day-merge columns a 12% floor made every
   * low-density spike/extreme bin the same fat bar → solid red blocks.
   * Absolute 2px min keeps single prints visible.
   */
  function densityBarWidthPx(n, maxPrints, innerW) {
    const maxP = Math.max(1, maxPrints || 1);
    const frac = Math.min(1, Math.max(0, n) / maxP);
    const w = Math.max(2, Math.round(frac * Math.max(2, innerW || 2)));
    return w;
  }

  // Bar/overview: keep ≥1.5px per print so HG bell never collapses to a stick
  const BAR_PX_PER_PRINT = 1.5;
  const COL_MIN_BAR = 10;


  function sessionMid(profile) {
    // Session mid (H+L)/2 from profile price rows (no true close in TPO cache)
    if (!profile || !profile.rows || !profile.rows.length) {
      return profile && profile.poc ? profile.poc.price : null;
    }
    let hi = -Infinity;
    let lo = Infinity;
    for (const r of profile.rows) {
      if (r.price > hi) hi = r.price;
      if (r.price < lo) lo = r.price;
    }
    if (!isFinite(hi) || !isFinite(lo)) {
      return profile.poc ? profile.poc.price : null;
    }
    return (hi + lo) / 2;
  }

  function sessionTrendPrice(profile) {
    // Prefer mid; fall back to POC
    const mid = sessionMid(profile);
    if (mid != null) return mid;
    return profile && profile.poc ? profile.poc.price : null;
  }

  /** Actual session/composite high & low from price rows (for trend Y scale). */
  function profileHiLo(profile) {
    if (!profile) return null;
    if (profile.rows && profile.rows.length) {
      let hi = -Infinity;
      let lo = Infinity;
      for (const r of profile.rows) {
        if (r.price > hi) hi = r.price;
        if (r.price < lo) lo = r.price;
      }
      if (isFinite(hi) && isFinite(lo)) return { hi, lo };
    }
    const mid = sessionTrendPrice(profile);
    if (mid == null) return null;
    return { hi: mid, lo: mid };
  }

  /** Week/month trend point: last trading-day mid (close proxy) so amplitude is real. */
  function groupTrendPrice(group) {
    if (!group) return null;
    if (group.sids && group.sids.length) {
      for (let i = group.sids.length - 1; i >= 0; i--) {
        const p = profileById.get(group.sids[i]);
        const mid = p ? sessionTrendPrice(p) : null;
        if (mid != null) return mid;
      }
    }
    return group.profile ? sessionTrendPrice(group.profile) : null;
  }

  function dayColWFromPrints(prints) {
    if (isBarMode()) {
      const unit = Math.max(BAR_PX_PER_PRINT, CHAR_W0 * zoomX);
      return Math.max(COL_MIN_BAR, Math.round(prints * unit + 2));
    }
    const ch = CHAR_W0 * zoomX;
    const pad = Math.max(2, COL_PAD0 * Math.min(zoomX, 1));
    return Math.max(4, Math.round(prints * ch + pad));
  }

  function dayColW(profile) {
    return dayColWFromPrints(maxPrintsOf(profile));
  }

  function colGap() {
    return zoomX < 0.2 ? 1 : 2;
  }

  function rowH() {
    // Bar/overview: keep a visible bin strip so the bell silhouette never blanks
    if (isBarMode()) return Math.max(1.5, BASE_ROW_H * zoomY);
    return Math.max(1, BASE_ROW_H * zoomY);
  }

  function fontPx() {
    // Letter height follows Y; X drives column width separately.
    // No hard 13px ceiling (that froze visual stretch above ~118% zoomY).
    // Keep slightly under rowH so glyphs sit inside the bin.
    const rh = BASE_ROW_H * zoomY;
    const target = 11 * zoomY;
    return Math.max(2, Math.min(target, rh * 0.92));
  }

  function headH() {
    // shrink header when zoomed out (overview) — follow column density
    return zoomX < 0.35 ? HEAD_H_THIN : HEAD_H;
  }

  function isBarMode() {
    // Letters need BOTH axes readable (old bar at single zoom < 0.5).
    // Do not call rowH/fontPx here (they call isBarMode).
    return zoomX < 0.5 || zoomY < 0.5;
  }

  function printCountOf(row) {
    if (!row) return 0;
    if (row.letters && row.letters.length) return row.letters.length;
    return row.count || 0;
  }

  // Cumulative column geometry for variable-width virtualization.
  // Hand-picked day merges insert an opaque merged HG column, then ghost
  // (semi-transparent) constituent day columns immediately to its right:
  //   [Merged A] [day1 ghost] [day2 ghost] …
  // Week/month modes keep plain day geometry (they use group primary render).
  function buildColGeometry(opts) {
    opts = opts || {};
    const gap = colGap();
    const useGhostMerges =
      !opts.plain &&
      !weekHgOn &&
      !monthHgOn &&
      completedRangeGroups().length > 0;
    if (!useGhostMerges) {
      const widths = new Array(sessionIds.length);
      const offsets = new Array(sessionIds.length + 1);
      offsets[0] = 0;
      for (let i = 0; i < sessionIds.length; i++) {
        const p = profileById.get(sessionIds[i]);
        const w = p ? dayColW(p) : dayColWFromPrints(PLACEHOLDER_PRINTS);
        widths[i] = w;
        offsets[i + 1] = offsets[i] + w + gap;
      }
      const contentW = offsets[sessionIds.length];
      const dayToVisual = new Array(sessionIds.length);
      for (let i = 0; i < sessionIds.length; i++) dayToVisual[i] = i;
      return {
        widths,
        offsets,
        gap,
        contentW,
        rightEdgePad: RIGHT_EDGE_PAD,
        totalW: contentW + RIGHT_EDGE_PAD,
        visualCols: null,
        dayToVisual,
        mergeGhost: false,
      };
    }

    const rangeOverlays = buildAllRangeGroups();
    const mergeByStart = new Map();
    const covered = new Set();
    for (let ri = 0; ri < rangeOverlays.length; ri++) {
      const g = rangeOverlays[ri];
      mergeByStart.set(g.startIdx, g);
      for (let j = g.startIdx; j <= g.endIdx; j++) covered.add(j);
    }

    const visualCols = [];
    const widths = [];
    for (let i = 0; i < sessionIds.length; i++) {
      if (mergeByStart.has(i)) {
        const g = mergeByStart.get(i);
        visualCols.push({
          type: "merge",
          group: g,
          dayIdx: i,
          startIdx: g.startIdx,
          endIdx: g.endIdx,
          sid: (g.sids && g.sids[g.sids.length - 1]) || g.label,
        });
        widths.push(compositeColW(g.profile, "range"));
        for (let j = g.startIdx; j <= g.endIdx; j++) {
          const p = profileById.get(sessionIds[j]);
          visualCols.push({
            type: "ghost",
            dayIdx: j,
            group: g,
            sid: sessionIds[j],
          });
          widths.push(p ? dayColW(p) : dayColWFromPrints(PLACEHOLDER_PRINTS));
        }
        continue;
      }
      if (covered.has(i)) continue;
      const p = profileById.get(sessionIds[i]);
      visualCols.push({ type: "day", dayIdx: i, sid: sessionIds[i] });
      widths.push(p ? dayColW(p) : dayColWFromPrints(PLACEHOLDER_PRINTS));
    }

    const offsets = new Array(widths.length + 1);
    offsets[0] = 0;
    for (let i = 0; i < widths.length; i++) {
      offsets[i + 1] = offsets[i] + widths[i] + gap;
    }
    const dayToVisual = new Array(sessionIds.length).fill(-1);
    for (let v = 0; v < visualCols.length; v++) {
      const vc = visualCols[v];
      if (vc.type === "day" || vc.type === "ghost") dayToVisual[vc.dayIdx] = v;
    }
    const contentW = offsets[widths.length] || 0;
    return {
      widths,
      offsets,
      gap,
      contentW,
      rightEdgePad: RIGHT_EDGE_PAD,
      totalW: contentW + RIGHT_EDGE_PAD,
      visualCols,
      dayToVisual,
      mergeGhost: true,
    };
  }

  function dayOffsetRight(geo, dayIdx) {
    if (!geo) return 0;
    if (geo.dayToVisual && geo.dayToVisual[dayIdx] != null && geo.dayToVisual[dayIdx] >= 0) {
      const v = geo.dayToVisual[dayIdx];
      return geo.offsets[v + 1] != null ? geo.offsets[v + 1] : geo.contentW;
    }
    return geo.offsets[dayIdx + 1] != null ? geo.offsets[dayIdx + 1] : geo.contentW;
  }

  function findColAtX(offsets, x) {
    // largest i with offsets[i] <= x
    let lo = 0;
    let hi = offsets.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (offsets[mid] <= x) lo = mid;
      else hi = mid - 1;
    }
    return Math.min(lo, Math.max(0, offsets.length - 2));
  }

  function overscanCols(avgW) {
    return Math.max(OVERSCAN_MIN, Math.ceil(160 / Math.max(4, avgW || 40)));
  }

  // ---- shared price grid across loaded sessions ----
  function computeGlobalGrid() {
    let minKey = Infinity;
    let maxKey = -Infinity;
    let step = parseFloat($("points").value) || 5;
    let n = 0;
    for (const p of profileById.values()) {
      if (!p || !p.rows || !p.rows.length) continue;
      step = p.step || step;
      for (const r of p.rows) {
        if (r.key < minKey) minKey = r.key;
        if (r.key > maxKey) maxKey = r.key;
      }
      n++;
    }
    if (!n || !isFinite(minKey)) {
      globalGrid = null;
      return null;
    }
    // Small padding so spikes aren't flush to edge
    const pad = 2;
    minKey -= pad;
    maxKey += pad;
    globalGrid = {
      minKey,
      maxKey,
      step,
      nBins: maxKey - minKey + 1,
    };
    return globalGrid;
  }

  const ZOOM_PRESETS = [10, 25, 50, 75, 100];

  function updateZoomLabel() {
    const sel = $("zoomSelect");
    const dual = $("zoomDualLabel");
    const linkBtn = $("btnLinkAxes");
    if (linkBtn) {
      linkBtn.setAttribute("aria-pressed", linkAxes ? "true" : "false");
      linkBtn.classList.toggle("active", linkAxes);
      linkBtn.textContent = linkAxes ? "連動" : "分軸";
      linkBtn.title = linkAxes
        ? "闊／高連動（撳轉分軸）"
        : "闊／高分開（撳轉連動）";
    }
    if (dual) {
      if (linkAxes) {
        dual.hidden = true;
        dual.textContent = "";
      } else {
        dual.hidden = false;
        dual.textContent =
          "闊 " + Math.round(zoomX * 100) + "% · 高 " + Math.round(zoomY * 100) + "%";
      }
    }
    if (!sel) return;
    // Linked: one %; unlinked: select shows Y (default +/- axis) with optional custom
    const pct = Math.round((linkAxes ? zoomX : zoomY) * 100);
    const custom = sel.querySelector("option[data-custom]");
    if (custom) custom.remove();
    if (ZOOM_PRESETS.includes(pct)) {
      sel.value = String(pct);
      return;
    }
    const opt = document.createElement("option");
    opt.value = String(pct);
    opt.textContent = pct + "%";
    opt.dataset.custom = "1";
    opt.selected = true;
    sel.insertBefore(opt, sel.firstChild);
  }

  // ---- CSV fallback ----
  function parseCSV(text) {
    const lines = text
      .trim()
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#"));
    if (lines.length < 2) throw new Error("CSV needs a header and at least one row.");
    const header = lines[0].split(/[,;\t]/).map((h) => h.trim().toLowerCase().replace(/['"]/g, ""));
    const findCol = (...names) => {
      for (const n of names) {
        const i = header.indexOf(n);
        if (i >= 0) return i;
      }
      return -1;
    };
    let iDt = findCol("datetime", "date", "time", "timestamp", "dt");
    let iHi = findCol("high", "h");
    let iLo = findCol("low", "l");
    if (iDt < 0 || iHi < 0 || iLo < 0) {
      if (header.length >= 3 && !isNaN(parseFloat(lines[1].split(/[,;\t]/)[1]))) {
        iDt = 0;
        iHi = 1;
        iLo = 2;
      } else {
        throw new Error("CSV needs columns: datetime, high, low.");
      }
    }
    const bars = [];
    for (let r = 1; r < lines.length; r++) {
      const cols = lines[r].split(/[,;\t]/).map((c) => c.trim().replace(/^["']|["']$/g, ""));
      if (cols.length < 3) continue;
      let d = new Date(cols[iDt]);
      if (isNaN(d.getTime())) d = new Date(cols[iDt].replace(" ", "T"));
      if (isNaN(d.getTime())) throw new Error("Bad datetime on row " + (r + 1));
      const high = parseFloat(cols[iHi]);
      const low = parseFloat(cols[iLo]);
      if (isNaN(high) || isNaN(low)) throw new Error("Bad high/low on row " + (r + 1));
      bars.push({ t: d.getTime(), high, low });
    }
    bars.sort((a, b) => a.t - b.t);
    return bars;
  }

  // ---- unaligned (per-day own scale) — day mode / align off ----
  function renderDayColumnHtml(result, opts) {
    const {
      rows, poc, vah, val, ibHi, ibLo, dualKeys, spikeKeys, step, sessionId, totalTpo,
      vaTop, vaBot, letterCount,
    } = result;
    const { showPrice, compact } = opts;
    const dual = new Set(dualKeys || []);
    const spike = new Set(spikeKeys || []);
    const nLetters = letterCount != null ? letterCount : 0;

    const lines = [];
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const classes = [];
      if (r.key === poc.key) classes.push("row-poc");
      if (i >= vaTop && i <= vaBot) classes.push("row-va");
      if (dual.has(r.key)) classes.push("row-dual");
      if (spike.has(r.key) && !(i >= vaTop && i <= vaBot)) classes.push("row-spike");
      const inIb =
        ibHi != null &&
        r.price <= ibHi + step / 2 &&
        r.price >= ibLo - step / 2;
      if (inIb) classes.push("row-ib");

      let tags = "";
      if (!compact && r.key === poc.key) tags = `<span class="tag poc">POC</span>`;
      else if (compact && r.key === poc.key) tags = `<span class="tag poc">·</span>`;

      const priceHtml = showPrice
        ? `<span class="price">${fmtPrice(r.price, step)}</span>`
        : "";
      lines.push(
        `<span class="${classes.join(" ")}">${priceHtml}<span class="letters">${escapeHtml(
          r.letters
        )}</span>${tags}</span>`
      );
    }

    const head = `<div class="day-head"><b>${sessionId}</b>
      <div class="day-meta">POC ${fmtPrice(poc.price, step)} · VA ${fmtPrice(val.price, step)}–${fmtPrice(
      vah.price,
      step
    )} · ${nLetters}L / ${totalTpo}p</div>
    </div>`;
    const cls = compact ? "day-col" : "day-col single";
    return `<div class="${cls}" data-sid="${sessionId}">${head}<pre class="profile">${lines.join(
      "\n"
    )}</pre></div>`;
  }

  function placeholderCol(sid, w, bodyH) {
    const h = bodyH != null ? `height:${bodyH}px` : "min-height:120px";
    const weekKey = weekStartKey(sid);
    const weekBand = weekBandOf(sid);
    return `<div class="day-col day-placeholder aligned week-band-${weekBand}" data-sid="${sid}" data-week="${escapeHtml(weekKey)}" data-week-band="${weekBand}" style="width:${w}px;${h}">
      <div class="day-head"><b>${sid}</b><div class="day-meta">…</div></div>
      <div class="day-body"></div>
    </div>`;
  }

  // ---- aligned column: absolute rows on shared bin grid (no empty DOM) ----
  function renderAlignedColumn(result, grid, w, rh, fp, hh, colOpts) {
    colOpts = colOpts || {};
    const {
      rows, poc, vah, val, ibHi, ibLo, dualKeys, spikeKeys, step, sessionId, totalTpo,
      letterCount,
    } = result;
    const dual = new Set(dualKeys || []);
    const spike = new Set(spikeKeys || []);
    const bodyH = grid.nBins * rh;
    const nLetters = letterCount != null ? letterCount : 0;
    const vaHi = Math.max(vah.key, val.key);
    const vaLo = Math.min(vah.key, val.key);
    const thin = w < 48 || zoomX < 0.4;
    const barMode = isBarMode();
    const maxPrints = Math.max(1, maxPrintsOf(result));
    // Compact date label for thin cols: MM-DD
    let label = sessionId;
    if (thin && /^\d{4}-\d{2}-\d{2}/.test(sessionId)) {
      label = sessionId.slice(5); // MM-DD
    } else if (sessionId.length > 10) {
      label = sessionId.slice(5) || sessionId;
    }

    // Full cell height in bar mode (tiny overlap hides hairline gaps between bins)
    const cellH = barMode ? Math.max(rh, 1.5) + (rh < 3 ? 0.35 : 0) : rh;
    const innerW = Math.max(4, w - (barMode ? 0 : 2));

    const cells = [];
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (r.key < grid.minKey || r.key > grid.maxKey) continue;
      const top = (grid.maxKey - r.key) * rh;
      const n = barMode ? densityCountOf(r) : printCountOf(r);
      if (barMode && n <= 0) continue;
      const classes = ["tpo-cell"];
      if (barMode) classes.push("bar-only");
      if (r.key === poc.key) classes.push("row-poc");
      if (r.key >= vaLo && r.key <= vaHi) classes.push("row-va");
      if (dual.has(r.key)) classes.push("row-dual");
      if (spike.has(r.key) && !(r.key >= vaLo && r.key <= vaHi)) classes.push("row-spike");
      if (
        ibHi != null &&
        r.price <= ibHi + step / 2 &&
        r.price >= ibLo - step / 2
      ) {
        classes.push("row-ib");
      }
      let style = `top:${top}px;height:${cellH}px;line-height:${cellH}px;font-size:${fp}px`;
      let text = "";
      if (barMode) {
        // Density ∝ TPO prints across full price range (incl. spike extremes)
        const barW = densityBarWidthPx(n, maxPrints, innerW);
        style += `;width:${barW}px;min-width:2px;left:0;right:auto`;
      } else {
        text = escapeHtml(r.letters || "");
        if (zoomX < 0.65 && n > 0) {
          const minBlock = Math.max(2, Math.round((n / maxPrints) * innerW * 0.85));
          style += `;min-width:${minBlock}px`;
        }
      }
      const titleBits = [sessionId, fmtPrice(r.price, step) + " pts"];
      if (r.letters) titleBits.push(r.letters);
      else if (n) titleBits.push(String(n) + "p");
      cells.push(
        `<div class="${classes.join(" ")}" style="${style}" title="${escapeHtml(
          titleBits.join(" · ")
        )}">${text}</div>`
      );
    }

    const pocTop = (grid.maxKey - poc.key) * rh + rh / 2;
    const meta = thin
      ? ""
      : `<div class="day-meta">${fmtPrice(poc.price, step)} · ${nLetters}L</div>`;
    const isGhost = !!colOpts.ghost;
    const selectable = colOpts.selectable !== false && !isGhost;
    const headCls = selectable ? "day-head day-head-select" : "day-head";
    const headTitle = isGhost
      ? "合併組內原日（半透明） · " + sessionId
      : "撳選合併範圍 · " + sessionId;
    const head = `<div class="${headCls}" style="height:${hh}px" data-idx="${colOpts.idx != null ? colOpts.idx : ""}" title="${escapeHtml(headTitle)}"><b title="${sessionId}">${escapeHtml(
      label
    )}</b>${meta}</div>`;
    const thinCls = thin ? " thin" : "";
    const barCls = barMode ? " bar-mode" : "";
    const fadeCls = colOpts.faded || isGhost ? " day-faded" : "";
    const ghostCls = isGhost ? " day-merge-ghost" : "";
    const selCls = colOpts.rangeSelected ? " day-range-sel" : "";
    const anchorCls = colOpts.rangeAnchor ? " day-range-anchor" : "";
    const weekKey = weekStartKey(sessionId);
    const weekBand = weekBandOf(sessionId);
    const weekCls = " week-band-" + weekBand;
    return `<div class="day-col aligned${thinCls}${barCls}${fadeCls}${ghostCls}${selCls}${anchorCls}${weekCls}" data-sid="${sessionId}" data-date="${escapeHtml(sessionId)}" data-week="${escapeHtml(weekKey)}" data-week-band="${weekBand}" data-idx="${colOpts.idx != null ? colOpts.idx : ""}" style="width:${w}px;min-width:0">
      ${head}
      <div class="day-body" style="height:${bodyH}px;position:relative">
        <div class="poc-line" style="top:${pocTop}px"></div>
        ${cells.join("")}
      </div>
    </div>`;
  }

  function latestLivePrice() {
    // Newest loaded session mid (H+L)/2; fall back to its POC.
    if (!sessionIds.length) return null;
    for (let i = sessionIds.length - 1; i >= 0; i--) {
      const p = profileById.get(sessionIds[i]);
      if (!p) continue;
      const price = sessionTrendPrice(p);
      if (price != null && isFinite(price)) {
        return { price, sid: sessionIds[i], step: p.step || (globalGrid && globalGrid.step) || 5 };
      }
    }
    return null;
  }

  function renderPriceAxis(grid, rh, fp, hh) {
    const bodyH = grid.nBins * rh;
    const step = grid.step;
    hh = hh != null ? hh : HEAD_H;
    // Aim ~1 label per 20–28px; snap to nice round prices
    const minGapPx = rh < 4 ? 28 : 20;
    let nicePts = 50;
    if (step >= 1) {
      const candidates = [25, 50, 100, 200, 500, 1000];
      nicePts = candidates.find((n) => (n / step) * rh >= minGapPx) || 1000;
    } else {
      nicePts = step * Math.max(1, Math.ceil(minGapPx / rh));
    }
    const niceKeys = Math.max(1, Math.round(nicePts / step));

    const labels = [];
    // Walk from a nice key at or below maxKey
    let k0 = Math.floor(grid.maxKey / niceKeys) * niceKeys;
    if (k0 > grid.maxKey) k0 -= niceKeys;
    for (let k = k0; k >= grid.minKey; k -= niceKeys) {
      if (k < grid.minKey || k > grid.maxKey) continue;
      const price = k * step;
      const top = (grid.maxKey - k) * rh;
      labels.push(
        `<div class="axis-label" style="top:${top}px;height:${Math.max(
          rh,
          12
        )}px;line-height:${Math.max(rh, 12)}px;font-size:${Math.max(
          8,
          Math.min(11, fp)
        )}px">${fmtPrice(price, step)}</div>`
      );
    }

    // Live / latest index mark on left axis (mid of newest session)
    let liveHtml = "";
    const live = latestLivePrice();
    if (live && live.price != null && step > 0) {
      const liveKey = live.price / step;
      if (liveKey >= grid.minKey - 0.5 && liveKey <= grid.maxKey + 0.5) {
        const top = (grid.maxKey - liveKey) * rh;
        const liveStr = fmtPrice(live.price, step);
        liveHtml =
          `<div class="axis-live" style="top:${top}px" title="最新市價（${escapeHtml(live.sid || "")} mid）">` +
          `<span class="axis-live-line" aria-hidden="true"></span>` +
          `<span class="axis-live-tag">${liveStr}</span>` +
          `</div>`;
      }
    }

    return `<div class="price-axis" style="width:${AXIS_W}px">
      <div class="day-head axis-head" style="height:${hh}px"><b>Price</b><div class="day-meta">Pts ${step}</div></div>
      <div class="day-body axis-body" style="height:${bodyH}px;position:relative">${labels.join("")}${liveHtml}</div>
    </div>`;
  }


  // ---- single-pin (isolated single-print) orphan highlights ----
  // Consecutive adjacent single-TPO levels (key runs of length ≥2) are NOT pins.
  function computeSinglePinSegments() {
    singlePinSegments = [];
    if (!wantAlign() || !globalGrid || !sessionIds.length) return singlePinSegments;
    const n = sessionIds.length;
    const step = globalGrid.step;
    const occupancy = new Array(n); // Set of keys with any print
    const isolated = new Array(n); // Set of isolated single-print keys

    for (let i = 0; i < n; i++) {
      const p = profileById.get(sessionIds[i]);
      const occ = new Set();
      const countByKey = new Map();
      if (p && p.rows) {
        for (let r = 0; r < p.rows.length; r++) {
          const row = p.rows[r];
          const c = printCountOf(row);
          if (c > 0) {
            occ.add(row.key);
            countByKey.set(row.key, c);
          }
        }
      }
      occupancy[i] = occ;

      const singleKeys = [];
      for (const [k, c] of countByKey) {
        if (c === 1) singleKeys.push(k);
      }
      singleKeys.sort((a, b) => a - b);
      const iso = new Set();
      let runStart = 0;
      while (runStart < singleKeys.length) {
        let runEnd = runStart;
        while (
          runEnd + 1 < singleKeys.length &&
          singleKeys[runEnd + 1] === singleKeys[runEnd] + 1
        ) {
          runEnd++;
        }
        // Only length-1 runs count as single pins
        if (runEnd === runStart) iso.add(singleKeys[runStart]);
        runStart = runEnd + 1;
      }
      isolated[i] = iso;
    }

    for (let i = 0; i < n; i++) {
      for (const key of isolated[i]) {
        let endIdx = n; // exclusive; n ⇒ extend through RIGHT_EDGE_PAD
        for (let j = i + 1; j < n; j++) {
          if (occupancy[j].has(key)) {
            endIdx = j;
            break;
          }
        }
        const rowPrice = (() => {
          const p = profileById.get(sessionIds[i]);
          if (p && p.rows) {
            for (let r = 0; r < p.rows.length; r++) {
              if (p.rows[r].key === key) return p.rows[r].price;
            }
          }
          return key * step;
        })();
        singlePinSegments.push({
          startIdx: i,
          endIdx,
          key,
          price: rowPrice,
          sid: sessionIds[i],
          step,
          extendsToPad: endIdx === n,
        });
      }
    }
    return singlePinSegments;
  }

  function renderSinglePinLayerHtml(geo, grid, rh, hh) {
    if (!singlePinSegments.length || !geo || !grid) return "";
    const { offsets, contentW, rightEdgePad } = geo;
    const edgePad = rightEdgePad != null ? rightEdgePad : RIGHT_EDGE_PAD;
    const cw = contentW != null ? contentW : offsets[sessionIds.length];
    const layerW = cw + edgePad;
    const bodyH = grid.nBins * rh;
    const layerH = hh + bodyH;
    const bars = [];
    for (let s = 0; s < singlePinSegments.length; s++) {
      const pin = singlePinSegments[s];
      if (pin.key < grid.minKey || pin.key > grid.maxKey) continue;
      const left = offsets[pin.startIdx];
      const right =
        pin.endIdx < sessionIds.length ? offsets[pin.endIdx] : cw + edgePad;
      const width = Math.max(1, right - left);
      const top = hh + (grid.maxKey - pin.key) * rh;
      const h = Math.max(1, rh);
      const ptsLabel = fmtPrice(pin.price, pin.step) + " pts";
      const title = ptsLabel + " · leftover " + pin.sid;
      bars.push(
        `<div class="single-pin-bar" style="left:${left}px;top:${top}px;width:${width}px;height:${h}px"` +
          ` data-pts="${escapeHtml(ptsLabel)}" data-date="${escapeHtml(pin.sid)}"` +
          ` data-key="${pin.key}" title="${escapeHtml(title)}"></div>`
      );
    }
    if (!bars.length) return "";
    return (
      `<div class="single-pin-layer" id="singlePinLayer" style="width:${layerW}px;height:${layerH}px">` +
      bars.join("") +
      `</div>`
    );
  }

  function hideSinglePinTip() {
    const tip = $("singlePinTip");
    if (tip) {
      tip.classList.remove("show");
      tip.setAttribute("aria-hidden", "true");
    }
  }

  function showSinglePinTip(bar, clientX, clientY) {
    const tip = $("singlePinTip");
    const wrap = $("hgWrap");
    if (!tip || !wrap || !bar) return;
    const pts = bar.getAttribute("data-pts") || "—";
    const date = bar.getAttribute("data-date") || "—";
    tip.innerHTML =
      `<span class="sp-pts">${escapeHtml(pts)}</span>` +
      `<span class="sp-sep">·</span>` +
      `<span class="sp-date">leftover ${escapeHtml(date)}</span>`;
    tip.classList.add("show");
    tip.setAttribute("aria-hidden", "false");
    const rect = wrap.getBoundingClientRect();
    let x = clientX - rect.left + 12;
    let y = clientY - rect.top + 12;
    const maxX = rect.width - 200;
    const maxY = rect.height - 36;
    if (x > maxX) x = clientX - rect.left - 200;
    if (y > maxY) y = clientY - rect.top - 36;
    tip.style.transform = `translate(${Math.max(4, x)}px, ${Math.max(4, y)}px)`;
  }


  // ---- Composite HG (week / month / multi-day merge) ----
  const DAY_FADE_OPACITY = 0.3;

  function completedRangeGroups() {
    return rangeGroups.filter((g) => g && g.endIdx > g.startIdx);
  }

  function rangesOverlap(a0, a1, b0, b1) {
    return !(a1 < b0 || b1 < a0);
  }

  function findOverlappingRangeGroup(startIdx, endIdx) {
    const a = Math.min(startIdx, endIdx);
    const b = Math.max(startIdx, endIdx);
    for (let i = 0; i < rangeGroups.length; i++) {
      const g = rangeGroups[i];
      if (rangesOverlap(a, b, g.startIdx, g.endIdx)) return g;
    }
    return null;
  }

  function rangeLabelFor(startIdx, endIdx) {
    const a = Math.min(startIdx, endIdx);
    const b = Math.max(startIdx, endIdx);
    const sa = sessionIds[a] || "";
    const sb = sessionIds[b] || "";
    if (!sa) return "合併";
    if (a === b) return sa.slice(5);
    return sa.slice(5) + "–" + sb.slice(5);
  }

  function compositeActive() {
    return !!(weekHgOn || monthHgOn || completedRangeGroups().length);
  }

  /** Week/month replace the day wall with in-flow narrow density columns (not overlays). Month wins. */
  function compositePrimaryKind() {
    if (monthHgOn) return "month";
    if (weekHgOn) return "week";
    return null;
  }

  function buildPrimaryGroups() {
    const kind = compositePrimaryKind();
    if (kind === "month") return buildMonthGroups();
    if (kind === "week") return buildWeekGroups();
    return [];
  }

  /** Width for a merged HG column (month stays narrow density; week wider for letters/POC/VA). */
  function compositeColW(profile, kind) {
    if (!profile) return dayColWFromPrints(PLACEHOLDER_PRINTS);
    const w = dayColW(profile);
    // Soft caps so multi-month / multi-week walls stay scannable
    if (kind === "month") return Math.min(w, Math.max(28, Math.round(80 * Math.max(zoomX, 0.12))));
    if (kind === "week") {
      // Week HG: enough width for letter cells + readable POC/VA head (not ultra-narrow bars)
      return Math.min(w, Math.max(56, Math.round(168 * Math.max(zoomX, 0.18))));
    }
    return Math.min(w, Math.max(36, Math.round(100 * Math.max(zoomX, 0.15))));
  }

  function buildGroupGeometry(groups) {
    const gap = colGap();
    const kind = compositePrimaryKind() || ((groups[0] && groups[0].kind) || "week");
    const n = groups.length;
    const widths = new Array(n);
    const offsets = new Array(n + 1);
    offsets[0] = 0;
    for (let i = 0; i < n; i++) {
      const g = groups[i];
      const w = compositeColW(g && g.profile, (g && g.kind) || kind);
      widths[i] = w;
      offsets[i + 1] = offsets[i] + w + gap;
    }
    const contentW = offsets[n] || 0;
    return {
      widths,
      offsets,
      gap,
      contentW,
      rightEdgePad: RIGHT_EDGE_PAD,
      totalW: contentW + RIGHT_EDGE_PAD,
      groupMode: kind,
    };
  }

  function buildAlphabetList() {
    const mode = ($("alphabet") && $("alphabet").value) || "hk";
    const firstLetter = (($("firstLetter") && $("firstLetter").value) || "D").trim() || "D";
    const upperSkip = "ABCDEFGHILMNOPQRSTUVWXYZ";
    const lowerSkip = "abcdefghiklmnopqrstuvwxyz";
    const classicU = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    const classicL = "abcdefghijklmnopqrstuvwxyz";
    if (mode === "hk") {
      let seq = (upperSkip + lowerSkip).split("");
      const fl = firstLetter.toUpperCase();
      let idx = seq.findIndex((c) => c.toUpperCase() === fl);
      if (idx > 0) seq = seq.slice(idx).concat(seq.slice(0, idx));
      else if (idx === -1) seq = [fl].concat(seq.filter((c) => c.toUpperCase() !== fl));
      return seq;
    }
    return (classicU + classicL).split("");
  }

  function parseSidDate(sid) {
    if (!sid || !/^\d{4}-\d{2}-\d{2}/.test(sid)) return null;
    const y = +sid.slice(0, 4);
    const m = +sid.slice(5, 7);
    const d = +sid.slice(8, 10);
    return new Date(y, m - 1, d, 12, 0, 0);
  }

  function weekStartKey(sid) {
    const dt = parseSidDate(sid);
    if (!dt) return "week:" + sid;
    const day = dt.getDay(); // 0=Sun
    const sinceMon = (day + 6) % 7;
    const mon = new Date(dt);
    mon.setDate(dt.getDate() - sinceMon);
    const y = mon.getFullYear();
    const m = String(mon.getMonth() + 1).padStart(2, "0");
    const d = String(mon.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }

  /** Alternate 0/1 band for ISO-ish trading weeks (Mon-start) — day-wall tint. */
  function weekBandOf(sid) {
    const wk = weekStartKey(sid);
    const dt = parseSidDate(wk.indexOf("week:") === 0 ? sid : wk);
    if (!dt) {
      let h = 0;
      for (let i = 0; i < wk.length; i++) h = (h + wk.charCodeAt(i) * (i + 1)) | 0;
      return h & 1;
    }
    // Week number within year from Monday key (stable alternate across weeks)
    const jan1 = new Date(dt.getFullYear(), 0, 1, 12, 0, 0);
    const weekNum = Math.floor((dt.getTime() - jan1.getTime()) / (7 * 86400000));
    return weekNum & 1;
  }

  function monthKey(sid) {
    if (sid && sid.length >= 7) return sid.slice(0, 7);
    return "month:" + sid;
  }

  function vaPctValue() {
    const v = parseFloat($("vaPct") && $("vaPct").value);
    return isFinite(v) ? v : 70;
  }

  /** Merge day profiles: one letter per day (presence); also sum intraday prints as density for HG bars. */
  function mergeDayProfiles(sids, label) {
    const cacheKey =
      (label || "merge") +
      "|" +
      sids.join(",") +
      "|" +
      (($("alphabet") && $("alphabet").value) || "hk") +
      "|" +
      (($("firstLetter") && $("firstLetter").value) || "D") +
      "|" +
      vaPctValue();
    if (compositeCache.has(cacheKey)) return compositeCache.get(cacheKey);

    const alphabet = buildAlphabetList();
    const loaded = [];
    for (let i = 0; i < sids.length; i++) {
      const p = profileById.get(sids[i]);
      if (p && p.rows && p.rows.length) loaded.push({ sid: sids[i], profile: p, letter: alphabet[i % alphabet.length] });
    }
    if (!loaded.length) {
      // Do not cache null — profiles may still be filling in.
      return null;
    }

    const step = loaded[0].profile.step || parseFloat($("points").value) || 5;
    const binMap = new Map(); // key -> letter string chars (1 per day presence)
    const densityMap = new Map(); // key -> sum of intraday TPO prints (HG silhouette)
    let ibHi = null;
    let ibLo = null;
    for (const item of loaded) {
      const keys = new Set();
      for (const r of item.profile.rows) {
        const n = printCountOf(r);
        if (n > 0) {
          keys.add(r.key);
          densityMap.set(r.key, (densityMap.get(r.key) || 0) + n);
        }
      }
      for (const k of keys) {
        const arr = binMap.get(k) || [];
        arr.push(item.letter);
        binMap.set(k, arr);
      }
      if (item.profile.ibHi != null) {
        ibHi = ibHi == null ? item.profile.ibHi : Math.max(ibHi, item.profile.ibHi);
        ibLo = ibLo == null ? item.profile.ibLo : Math.min(ibLo, item.profile.ibLo);
      }
    }

    const keys = Array.from(binMap.keys()).sort((a, b) => b - a);
    const rows = keys.map((k) => {
      const letters = binMap.get(k).join("");
      // count = day-presence (POC/VA/letters); density = true prints for bar silhouette
      return {
        key: k,
        price: k * step,
        letters,
        count: letters.length,
        density: densityMap.get(k) || letters.length,
      };
    });
    if (!rows.length) {
      return null;
    }

    const maxC = Math.max(...rows.map((r) => r.count));
    const mid = (rows[0].price + rows[rows.length - 1].price) / 2;
    let poc = rows[0];
    let bestDist = Infinity;
    for (const r of rows) {
      if (r.count === maxC) {
        const d = Math.abs(r.price - mid);
        if (d < bestDist) {
          bestDist = d;
          poc = r;
        }
      }
    }

    const totalTpo = rows.reduce((s, r) => s + r.count, 0);
    const target = Math.ceil((totalTpo * vaPctValue()) / 100);
    let pocIdx = rows.findIndex((r) => r.key === poc.key);
    let vaTop = pocIdx;
    let vaBot = pocIdx;
    let acc = rows[pocIdx].count;
    while (acc < target) {
      const above = vaTop > 0 ? rows[vaTop - 1].count : -1;
      const below = vaBot < rows.length - 1 ? rows[vaBot + 1].count : -1;
      if (above < 0 && below < 0) break;
      if (above >= below) {
        vaTop -= 1;
        acc += rows[vaTop].count;
      } else {
        vaBot += 1;
        acc += rows[vaBot].count;
      }
    }
    const vah = rows[vaTop];
    const val = rows[vaBot];

    const dualKeys = [];
    const threshold = Math.max(2, Math.floor(maxC * 0.7));
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (i === pocIdx || r.count < threshold) continue;
      if (vaTop <= i && i <= vaBot) continue;
      const lo = Math.min(i, pocIdx);
      const hi = Math.max(i, pocIdx);
      let minBetween = Infinity;
      for (let j = lo + 1; j < hi; j++) minBetween = Math.min(minBetween, rows[j].count);
      if (!isFinite(minBetween)) minBetween = 0;
      if (minBetween < r.count * 0.6 && minBetween < maxC * 0.5) dualKeys.push(r.key);
    }

    const spikeKeys = [];
    for (let i = 0; i < vaTop; i++) if (rows[i].count <= 2) spikeKeys.push(rows[i].key);
    for (let i = vaBot + 1; i < rows.length; i++) if (rows[i].count <= 2) spikeKeys.push(rows[i].key);

    const result = {
      sessionId: label || loaded[0].sid + "…",
      letters: loaded.map((x, i) => ({ letter: x.letter, sid: x.sid, ord: i })),
      rows,
      poc,
      vah,
      val,
      vaTop,
      vaBot,
      ibHi,
      ibLo,
      totalTpo,
      dualKeys,
      spikeKeys,
      step,
      letterCount: loaded.length,
      composite: true,
      dayCount: loaded.length,
    };
    compositeCache.set(cacheKey, result);
    return result;
  }

  function groupIndicesByKey(keyFn) {
    const groups = [];
    let curKey = null;
    let start = 0;
    for (let i = 0; i < sessionIds.length; i++) {
      const k = keyFn(sessionIds[i]);
      if (curKey == null) {
        curKey = k;
        start = i;
      } else if (k !== curKey) {
        groups.push({ key: curKey, startIdx: start, endIdx: i - 1 });
        curKey = k;
        start = i;
      }
    }
    if (curKey != null) groups.push({ key: curKey, startIdx: start, endIdx: sessionIds.length - 1 });
    return groups;
  }

  function buildWeekGroups() {
    return groupIndicesByKey(weekStartKey)
      .map((g) => {
        const sids = sessionIds.slice(g.startIdx, g.endIdx + 1);
        const profile = mergeDayProfiles(sids, "W " + g.key);
        if (!profile) return null;
        const label =
          sids.length === 1
            ? sids[0].slice(5)
            : sids[0].slice(5) + "–" + sids[sids.length - 1].slice(5);
        return { kind: "week", startIdx: g.startIdx, endIdx: g.endIdx, sids, profile, label: "週 " + label };
      })
      .filter(Boolean);
  }

  function buildMonthGroups() {
    return groupIndicesByKey(monthKey)
      .map((g) => {
        const sids = sessionIds.slice(g.startIdx, g.endIdx + 1);
        const profile = mergeDayProfiles(sids, "M " + g.key);
        if (!profile) return null;
        return {
          kind: "month",
          startIdx: g.startIdx,
          endIdx: g.endIdx,
          sids,
          profile,
          label: "月 " + g.key,
        };
      })
      .filter(Boolean);
  }

  function buildOneRangeGroup(g, colorIdx) {
    if (!g || g.endIdx <= g.startIdx) return null;
    const a = Math.min(g.startIdx, g.endIdx);
    const b = Math.max(g.startIdx, g.endIdx);
    const sids = sessionIds.slice(a, b + 1);
    if (sids.length < 2) return null;
    const profile = mergeDayProfiles(
      sids,
      "R" + g.id + " " + sids[0] + ".." + sids[sids.length - 1]
    );
    if (!profile) return null;
    return {
      kind: "range",
      id: g.id,
      colorIdx: colorIdx != null ? colorIdx : 0,
      startIdx: a,
      endIdx: b,
      sids,
      profile,
      label: rangeLabelFor(a, b) + " 合併",
    };
  }

  function buildAllRangeGroups() {
    return completedRangeGroups()
      .map((g, i) => buildOneRangeGroup(g, i % RANGE_COLOR_N))
      .filter(Boolean);
  }

  function indicesUnderComposite() {
    const set = new Set();
    const groups = completedRangeGroups();
    if (groups.length) {
      for (const g of groups) {
        for (let i = g.startIdx; i <= g.endIdx; i++) set.add(i);
      }
      return set;
    }
    if (monthHgOn || weekHgOn) {
      for (let i = 0; i < sessionIds.length; i++) set.add(i);
    }
    return set;
  }

  function spanLeftWidth(geo, startIdx, endIdx) {
    const gap = geo.gap || 0;
    const left = geo.offsets[startIdx];
    const right = geo.offsets[endIdx + 1] - gap;
    return { left, width: Math.max(4, right - left) };
  }

  function renderCompositeColumnHtml(group, grid, left, width, rh, fp, hh, opts) {
    opts = opts || {};
    const faded = !!opts.faded;
    const forceLetters = !!opts.forceLetters;
    const result = group.profile;
    const {
      rows, poc, vah, val, ibHi, ibLo, dualKeys, spikeKeys, step, totalTpo, letterCount,
    } = result;
    const dual = new Set(dualKeys || []);
    const spike = new Set(spikeKeys || []);
    const bodyH = grid.nBins * rh;
    const vaHi = Math.max(vah.key, val.key);
    const vaLo = Math.min(vah.key, val.key);
    // forceLetters only when not in bar mode (high zoom); at fit-all use bar silhouette
    const barMode = forceLetters ? false : isBarMode();
    // Density silhouette uses summed intraday prints (not day-presence letters)
    const maxPrints = Math.max(1, maxPrintsOf(result));
    const cellH = barMode ? Math.max(rh, 1.5) + (rh < 3 ? 0.35 : 0) : rh;
    const innerW = Math.max(4, width - (barMode ? 0 : 4));
    const nLetters = letterCount != null ? letterCount : 0;

    const cells = [];
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (r.key < grid.minKey || r.key > grid.maxKey) continue;
      const top = (grid.maxKey - r.key) * rh;
      const nLettersRow = printCountOf(r);
      const n = barMode ? densityCountOf(r) : nLettersRow;
      if (barMode && n <= 0) continue;
      const classes = ["tpo-cell"];
      if (barMode) classes.push("bar-only");
      if (r.key === poc.key) classes.push("row-poc");
      if (r.key >= vaLo && r.key <= vaHi) classes.push("row-va");
      if (dual.has(r.key)) classes.push("row-dual");
      if (spike.has(r.key) && !(r.key >= vaLo && r.key <= vaHi)) classes.push("row-spike");
      if (ibHi != null && r.price <= ibHi + step / 2 && r.price >= ibLo - step / 2) {
        classes.push("row-ib");
      }
      let style = `top:${top}px;height:${cellH}px;line-height:${cellH}px;font-size:${fp}px`;
      let text = "";
      if (barMode) {
        // Full-range density silhouette (spike/extremes included — no % width floor)
        const barW = densityBarWidthPx(n, maxPrints, innerW);
        style += `;width:${barW}px;min-width:2px;left:0;right:auto`;
      } else {
        text = escapeHtml(r.letters || "");
        // Wide merge overlays: size by density so extremes stay jagged, not full-width color slabs
        const dn = densityCountOf(r);
        const densW = densityBarWidthPx(dn, maxPrints, innerW);
        const letterW = Math.max(2, Math.round((nLettersRow || 1) * Math.max(4, fp * 0.62)));
        const barW = Math.max(densW, letterW);
        style += `;width:${barW}px;min-width:2px;left:0;right:auto`;
      }
      const titleBits = [group.label, fmtPrice(r.price, step) + " pts"];
      if (r.letters) titleBits.push(r.letters);
      cells.push(
        `<div class="${classes.join(" ")}" style="${style}" title="${escapeHtml(
          titleBits.join(" · ")
        )}">${text}</div>`
      );
    }

    const pocTop = (grid.maxKey - poc.key) * rh + rh / 2;
    const thin = width < 72 || zoomX < 0.35;
    const isWeek = (group.kind || "") === "week";
    // Week HG always shows POC + VA in head; month/range keep compact when thin
    let meta = "";
    if (isWeek || !thin) {
      const vaBits =
        "VA " + fmtPrice(val.price, step) + "–" + fmtPrice(vah.price, step);
      const extra = thin
        ? ""
        : " · " + nLetters + "L / " + totalTpo + "p";
      meta =
        `<div class="day-meta">POC ${fmtPrice(poc.price, step)} · ${vaBits}${extra}</div>`;
    }
    const fadeCls = faded ? " composite-faded" : "";
    const kindCls = " composite-" + (group.kind || "merge");
    const flowCls = opts.flow ? " composite-flow" : "";
    const lettersCls = forceLetters ? " composite-letters" : "";
    const colorIdx =
      opts.colorIdx != null
        ? opts.colorIdx
        : group.colorIdx != null
          ? group.colorIdx
          : 0;
    const colorCls =
      (group.kind || "") === "range" ? " merge-c" + (colorIdx % RANGE_COLOR_N) : "";
    const leftStyle = opts.flow ? "" : `left:${left}px;`;
    return (
      `<div class="composite-col${kindCls}${fadeCls}${flowCls}${lettersCls}${colorCls}" data-kind="${escapeHtml(group.kind || "")}" data-gid="${group.id != null ? group.id : ""}"` +
      ` style="${leftStyle}width:${width}px;min-width:${width}px">` +
      `<div class="day-head composite-head" style="height:${hh}px"><b title="${escapeHtml(
        group.label
      )}">${escapeHtml(group.label)}</b>${meta}</div>` +
      `<div class="day-body" style="height:${bodyH}px;position:relative">` +
      `<div class="poc-line" style="top:${pocTop}px"></div>` +
      cells.join("") +
      `</div></div>`
    );
  }

  function renderCompositeLayersHtml(geo, grid, rh, fp, hh, viewFirst, viewLast) {
    // Hand-picked day merges now render in-flow as [merge][ghost days]
    // (see buildColGeometry + virtualRenderAlignedDays). Week/month use
    // virtualRenderGroupPrimary. No full-span overlays remain.
    return "";
  }

  function clearRangeSelection() {
    rangeGroups = [];
    rangeAnchorIdx = null;
    rangeDraft = null;
  }

  function clearOneRangeGroup(id) {
    const nid = typeof id === "string" ? parseInt(id, 10) : id;
    rangeGroups = rangeGroups.filter((g) => g.id !== nid);
    syncCompositeButtons();
    scheduleRender();
  }

  function clearDayMergesOnly() {
    rangeGroups = [];
    rangeAnchorIdx = null;
    rangeDraft = null;
    syncCompositeButtons();
    scheduleRender();
  }

  function setMergePanelOpen(open) {
    const mp = $("mergePanel");
    const btn = $("btnMergeInfo");
    const on = !!open;
    if (mp) {
      mp.hidden = !on;
      mp.classList.toggle("is-open", on);
    }
    if (btn) {
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function isMergePanelOpen() {
    const mp = $("mergePanel");
    return !!(mp && !mp.hidden && mp.classList.contains("is-open"));
  }

  function toggleMergePanel() {
    const next = !isMergePanelOpen();
    setMergePanelOpen(next);
    if (next) syncMergeGroupList();
  }

  function startAddMergeGroup() {
    if (weekHgOn || monthHgOn) {
      weekHgOn = false;
      monthHgOn = false;
    }
    rangeAnchorIdx = null;
    rangeDraft = null;
    setMergePanelOpen(true);
    syncCompositeButtons();
    scheduleRender();
    const hint = $("compositeHint");
    if (hint) hint.textContent = "加新組：撳起始日，再撳結束日";
    const ph = $("mergePanelHint");
    if (ph) ph.textContent = "撳兩個日期頭選連續日 · 唔好同現有組合併重疊";
  }

  function setWeekHg(on) {
    weekHgOn = !!on;
    if (weekHgOn) clearRangeSelection();
    if (weekHgOn || monthHgOn) {
      if ($("alignPrices") && !$("alignPrices").checked) $("alignPrices").checked = true;
    }
    syncCompositeButtons();
    compositeCache.clear();
    scheduleRender();
  }

  function setMonthHg(on) {
    monthHgOn = !!on;
    if (monthHgOn) clearRangeSelection();
    if (weekHgOn || monthHgOn) {
      if ($("alignPrices") && !$("alignPrices").checked) $("alignPrices").checked = true;
    }
    syncCompositeButtons();
    compositeCache.clear();
    scheduleRender();
  }

  function clearAllComposites() {
    weekHgOn = false;
    monthHgOn = false;
    clearRangeSelection();
    syncCompositeButtons();
    scheduleRender();
  }

  function rangeGroupStats(g, colorIdx) {
    /** Build TPO / VA / POC strings for one day-merge group. Never throws. */
    const empty = { tpo: "—", va: "—", poc: "—", ok: false };
    try {
      const built = buildOneRangeGroup(g, colorIdx);
      const p = built && built.profile;
      if (!p) return empty;
      const step = p.step || 5;
      let tpoShow = 0;
      if (p.rows && p.rows.length) {
        tpoShow = p.rows.reduce((s, r) => s + densityCountOf(r), 0);
      }
      if (!tpoShow) tpoShow = p.totalTpo || 0;
      const pocStr = p.poc && p.poc.price != null ? fmtPrice(p.poc.price, step) : "—";
      const vaLo = p.val && p.val.price != null ? fmtPrice(p.val.price, step) : "—";
      const vaHi = p.vah && p.vah.price != null ? fmtPrice(p.vah.price, step) : "—";
      return {
        tpo: String(tpoShow),
        va: vaLo + "–" + vaHi,
        poc: pocStr,
        ok: true,
      };
    } catch (e) {
      console.warn("rangeGroupStats", e);
      return empty;
    }
  }

  function syncMergeGroupList() {
    const list = $("mergeGroupList");
    const countEl = $("mergeCount");
    const btn = $("btnMergeInfo");
    const groups = completedRangeGroups();
    const countTxt = groups.length ? "(" + groups.length + ")" : "";
    if (countEl) countEl.textContent = countTxt;
    if (btn) {
      btn.textContent = groups.length ? "合併資訊 " + countTxt : "合併資訊";
    }
    if (!list) return;
    if (!groups.length) {
      list.innerHTML = '<div class="merge-empty">未有日合併組 · 撳兩個日期頭</div>';
      return;
    }
    list.innerHTML = groups
      .map((g, i) => {
        const lab = rangeLabelFor(g.startIdx, g.endIdx);
        const n = g.endIdx - g.startIdx + 1;
        const st = rangeGroupStats(g, i % RANGE_COLOR_N);
        const titleBits = [lab, n + "日", "TPO " + st.tpo, "VA " + st.va, "POC " + st.poc];
        return (
          `<div class="merge-row" data-gid="${g.id}">` +
          `<span class="merge-swatch merge-c${i % RANGE_COLOR_N}" aria-hidden="true"></span>` +
          `<div class="merge-lab-wrap">` +
          `<span class="merge-lab" title="${escapeHtml(titleBits.join(" · "))}">${escapeHtml(lab)} · ${n}日</span>` +
          `<div class="merge-stats" title="${escapeHtml(titleBits.join(" · "))}">` +
          `<span class="merge-stat"><b>TPO</b> ${escapeHtml(st.tpo)}</span>` +
          `<span class="merge-stat"><b>VA</b> ${escapeHtml(st.va)}</span>` +
          `<span class="merge-stat"><b>POC</b> ${escapeHtml(st.poc)}</span>` +
          `</div>` +
          `</div>` +
          `<button type="button" class="merge-x" data-clear-gid="${g.id}" title="清呢一組">×</button>` +
          `</div>`
        );
      })
      .join("");
  }

  function syncCompositeButtons() {
    const bw = $("btnWeekHg");
    const bm = $("btnMonthHg");
    const bc = $("btnClearMerge");
    if (bw) {
      bw.classList.toggle("active", weekHgOn);
      bw.setAttribute("aria-pressed", weekHgOn ? "true" : "false");
    }
    if (bm) {
      bm.classList.toggle("active", monthHgOn);
      bm.setAttribute("aria-pressed", monthHgOn ? "true" : "false");
    }
    if (bc) {
      const show = compositeActive() || rangeAnchorIdx != null;
      bc.hidden = !show;
      bc.disabled = !show;
    }
    syncMergeGroupList();
    const groups = completedRangeGroups();
    const hint = $("compositeHint");
    const ph = $("mergePanelHint");
    let msg = "";
    if (rangeAnchorIdx != null) {
      msg = "已選 " + (sessionIds[rangeAnchorIdx] || "") + " · 再撳另一日完成第 " + (groups.length + 1) + " 組";
    } else if (groups.length) {
      msg = groups.length + " 組日合併 · 再撳日期頭可加新組";
    } else if (monthHgOn && weekHgOn) {
      msg = "月欄（窄密度 HG）· 週按鈕讓路畀月";
    } else if (monthHgOn) {
      msg = "每月一欄密度 HG · 可橫掃幾個月";
    } else if (weekHgOn) {
      msg = "每週一欄密度 HG · 可橫掃幾週";
    } else {
      msg = "撳日期頭可合併連續日 · 可加多組";
    }
    if (hint) hint.textContent = msg;
    if (ph && rangeAnchorIdx == null) {
      ph.textContent = groups.length
        ? "可再撳日期頭加新組（唔好同現有重疊）"
        : "撳兩個日期頭選連續日；可加多組互唔重疊範圍";
    }
  }

  function onDayHeadSelect(idx) {
    if (idx == null || idx < 0 || idx >= sessionIds.length) return;
    // Selecting a range clears week/month
    if (weekHgOn || monthHgOn) {
      weekHgOn = false;
      monthHgOn = false;
    }
    if (rangeAnchorIdx == null) {
      rangeAnchorIdx = idx;
      rangeDraft = { startIdx: idx, endIdx: idx };
    } else {
      const a = Math.min(rangeAnchorIdx, idx);
      const b = Math.max(rangeAnchorIdx, idx);
      if (b === a) {
        // same day again → cancel in-progress only (keep completed groups)
        rangeAnchorIdx = null;
        rangeDraft = null;
      } else {
        const overlap = findOverlappingRangeGroup(a, b);
        if (overlap) {
          rangeAnchorIdx = null;
          rangeDraft = null;
          const msg =
            "與組「" +
            rangeLabelFor(overlap.startIdx, overlap.endIdx) +
            "」重疊 · 先清該組或另選";
          const hint = $("compositeHint");
          if (hint) hint.textContent = msg;
          const ph = $("mergePanelHint");
          if (ph) ph.textContent = msg;
          setMergePanelOpen(true);
          syncCompositeButtons();
          scheduleRender();
          return;
        }
        rangeGroups.push({ id: nextRangeId++, startIdx: a, endIdx: b });
        rangeAnchorIdx = null;
        rangeDraft = null;
        if ($("alignPrices") && !$("alignPrices").checked) $("alignPrices").checked = true;
        // Keep panel collapsed by default; user opens via 合併資訊
      }
    }
    syncCompositeButtons();
    scheduleRender();
  }


  function scheduleRender() {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
      renderScheduled = false;
      try {
        virtualRender();
      } catch (e) {
        console.error(e);
        try {
          showErr("Render: " + (e && e.message ? e.message : String(e)));
        } catch (_) {}
      }
    });
  }

  function virtualRender() {
    const sc = $("profilesScroll");
    if (!sc) return;
    if (!sessionIds.length) {
      // During fetchAndBuild we clear sessionIds before await — don't clobber the wall with
      // a sticky "No sessions" if a rAF from setSettingsCollapsed / scroll races the load.
      if (profilesLoading) {
        if (!sc.querySelector(".profile") && !sc.querySelector(".profiles-aligned")) {
          sc.innerHTML = `<div class="profiles-row" id="profilesRow"><div class="day-col single"><pre class="profile">Loading…</pre></div></div>`;
        }
        return;
      }
      sc.innerHTML = `<div class="profiles-row" id="profilesRow"><div class="day-col single"><pre class="profile">No sessions.</pre></div></div>`;
      const st = $("stats");
      if (st) st.innerHTML = "";
      return;
    }

    // Composites need shared price bins
    if (compositeActive() && $("alignPrices") && !$("alignPrices").checked) {
      $("alignPrices").checked = true;
    }
    const align = (wantAlign() || compositeActive()) && sessionIds.length > 0;
    const tRender0 = performance.now();

    if (align) {
      computeGlobalGrid();
      if (!globalGrid) {
        // Nothing loaded yet — placeholders
        virtualRenderUnaligned();
        return;
      }
      virtualRenderAligned(tRender0);
    } else {
      globalGrid = null;
      virtualRenderUnaligned();
    }
  }

  function virtualRenderUnaligned() {
    const sc = $("profilesScroll");
    if (!sc.querySelector("#profilesRow") || sc.querySelector(".profiles-aligned")) {
      sc.innerHTML = '<div class="profiles-row" id="profilesRow"></div>';
    }
    const row = $("profilesRow");
    const compact = sessionIds.length > 1;
    const geo = buildColGeometry({ plain: true });
    // unaligned HTML columns need a bit more room for price gutter when shown
    const widths = geo.widths.map((w) => Math.max(w, compact ? 80 : 180));
    const gap = 6;
    const offsets = [0];
    for (let i = 0; i < widths.length; i++) offsets.push(offsets[i] + widths[i] + gap);
    const totalW = offsets[offsets.length - 1];
    const scrollLeft = sc.scrollLeft;
    const viewW = sc.clientWidth || 800;
    const avgW = sessionIds.length ? totalW / sessionIds.length : 100;
    const os = overscanCols(avgW);

    let first = findColAtX(offsets, scrollLeft) - os;
    let last = findColAtX(offsets, scrollLeft + viewW) + os;
    first = Math.max(0, first);
    last = Math.min(sessionIds.length - 1, last);

    let priceIdx = -1;
    for (let i = first; i <= last; i++) {
      if (profileById.has(sessionIds[i])) {
        priceIdx = i;
        break;
      }
    }

    const parts = [];
    const leftPad = offsets[first];
    if (leftPad > 0) {
      parts.push(`<div class="virt-spacer" style="flex:0 0 ${leftPad}px;width:${leftPad}px;height:1px"></div>`);
    }
    for (let i = first; i <= last; i++) {
      const sid = sessionIds[i];
      const p = profileById.get(sid);
      if (p) {
        parts.push(renderDayColumnHtml(p, { showPrice: !compact || i === priceIdx, compact }));
      } else {
        parts.push(placeholderCol(sid, widths[i]));
      }
    }
    const contentW = offsets[offsets.length - 1];
    const virtRight = Math.max(0, contentW - offsets[last + 1]);
    const edgePad = RIGHT_EDGE_PAD;
    const rightPad = virtRight + edgePad;
    if (rightPad > 0) {
      parts.push(`<div class="virt-spacer" style="flex:0 0 ${rightPad}px;width:${rightPad}px;height:1px"></div>`);
    }

    const keep = sc.scrollLeft;
    const keepTop = sc.scrollTop;
    row.className = "profiles-row";
    row.innerHTML = parts.join("");
    sc.scrollLeft = keep;
    sc.scrollTop = keepTop;
    lastGeo = {
      align: false,
      widths,
      offsets,
      totalW: contentW + edgePad,
      contentW,
      rightEdgePad: edgePad,
      axisW: 0,
      gap,
    };
    updateTimeline();
    updateTrendChart();
    hideCrosshair();
    hideSinglePinTip();
    singlePinSegments = [];
    updateStats(null);
  }


  function virtualRenderGroupPrimary(tRender0) {
    const sc = $("profilesScroll");
    const grid = globalGrid;
    const rh = rowH();
    const fp = fontPx();
    const hh = headH();
    const bodyH = grid.nBins * rh;
    const groups = buildPrimaryGroups().filter(Boolean);
    if (!groups.length) {
      // fallback to day wall
      virtualRenderAlignedDays(tRender0);
      return;
    }
    const geo = buildGroupGeometry(groups);
    const { widths, offsets, contentW, gap } = geo;
    const edgePad = RIGHT_EDGE_PAD;
    const kind = geo.groupMode;

    const scrollLeft = sc.scrollLeft;
    const viewW = sc.clientWidth || 800;
    const avgW = groups.length ? contentW / groups.length : 80;
    const os = Math.max(2, overscanCols(avgW));

    let first = findColAtX(offsets, scrollLeft) - os;
    let last = findColAtX(offsets, scrollLeft + viewW) + os;
    first = Math.max(0, first);
    last = Math.min(groups.length - 1, last);

    const MAX_DOM = 60;
    if (last - first + 1 > MAX_DOM) {
      const mid = Math.floor((first + last) / 2);
      first = Math.max(0, mid - Math.floor(MAX_DOM / 2));
      last = Math.min(groups.length - 1, first + MAX_DOM - 1);
    }

    const spacerH = hh + bodyH;
    const parts = [];
    const leftPad = offsets[first];
    if (leftPad > 0) {
      parts.push(
        `<div class="virt-spacer" style="flex:0 0 ${leftPad}px;width:${leftPad}px;height:${spacerH}px"></div>`
      );
    }
    for (let i = first; i <= last; i++) {
      parts.push(
        renderCompositeColumnHtml(groups[i], grid, 0, widths[i], rh, fp, hh, {
          flow: true,
          faded: false,
          // Week: letters when readable; at fit-all / low zoom use density silhouette (same as day merge)
          // Month stays density bars always
          forceLetters: kind === "week" && !isBarMode(),
        })
      );
    }
    const virtRight = Math.max(0, contentW - offsets[last + 1]);
    const rightPad = virtRight + edgePad;
    if (rightPad > 0) {
      parts.push(
        `<div class="virt-spacer" style="flex:0 0 ${rightPad}px;width:${rightPad}px;height:${spacerH}px"></div>`
      );
    }

    const keep = sc.scrollLeft;
    const keepTop = sc.scrollTop;
    singlePinSegments = [];
    const axisHtml = renderPriceAxis(grid, rh, fp, hh);
    sc.innerHTML =
      `<div class="profiles-aligned">` +
      axisHtml +
      `<div class="profiles-row" id="profilesRow" style="gap:${gap}px;position:relative">` +
      `${parts.join("")}</div>` +
      `</div>`;
    sc.scrollLeft = keep;
    sc.scrollTop = keepTop;

    // Map group columns into lastGeo so timeline / trend / hit-test work
    const labelIds = groups.map((g) => {
      if (g.sids && g.sids.length) return g.sids[g.sids.length - 1];
      return g.label;
    });
    lastGeo = {
      widths,
      offsets,
      totalW: contentW + edgePad,
      contentW,
      rightEdgePad: edgePad,
      gap,
      axisW: AXIS_W,
      rh,
      hh,
      bodyH,
      align: true,
      // Week follows zoom bar-mode; month always density-style geo hint
      barMode: kind === "month" || isBarMode(),
      groupMode: kind,
      groups,
      // parallel arrays for timeline/trend (one entry per group column)
      groupLabels: groups.map((g) => g.label),
      groupSids: labelIds,
      sessionIdsOverride: labelIds,
    };
    updateTimeline();
    updateTrendChart();
    hideCrosshair();
    hideSinglePinTip();
    syncCompositeButtons();
    updateStats(null);
    const ms = (performance.now() - tRender0).toFixed(0);
    const st = $("stats");
    if (st) {
      const extra = st.innerHTML ? " · " + st.innerHTML : "";
      st.innerHTML = `${groups.length} ${kind === "month" ? "月" : "週"} HG · Render ${ms}ms${extra}`;
    }
  }

  function virtualRenderAligned(tRender0) {
    if (compositePrimaryKind()) {
      virtualRenderGroupPrimary(tRender0);
      return;
    }
    virtualRenderAlignedDays(tRender0);
  }

  function virtualRenderAlignedDays(tRender0) {
    const sc = $("profilesScroll");
    const grid = globalGrid;
    const rh = rowH();
    const fp = fontPx();
    const hh = headH();
    const bodyH = grid.nBins * rh;
    const geo = buildColGeometry();
    const { widths, offsets, totalW } = geo;
    const visualCols = geo.visualCols;
    const nCols = widths.length;
    const mergeGhost = !!geo.mergeGhost && visualCols && visualCols.length;

    const scrollLeft = sc.scrollLeft;
    const viewW = sc.clientWidth || 800;
    const avgW = nCols ? totalW / nCols : 40;
    const os = overscanCols(avgW);

    let first = findColAtX(offsets, scrollLeft) - os;
    let last = findColAtX(offsets, scrollLeft + viewW) + os;
    first = Math.max(0, first);
    last = Math.min(nCols - 1, last);

    // Cap DOM columns so extreme zoom-out still stays snappy
    const MAX_DOM = 90;
    if (last - first + 1 > MAX_DOM) {
      const mid = Math.floor((first + last) / 2);
      first = Math.max(0, mid - Math.floor(MAX_DOM / 2));
      last = Math.min(nCols - 1, first + MAX_DOM - 1);
    }

    const parts = [];
    const leftPad = offsets[first] || 0;
    const spacerH = hh + bodyH;
    if (leftPad > 0) {
      parts.push(
        `<div class="virt-spacer" style="flex:0 0 ${leftPad}px;width:${leftPad}px;height:${spacerH}px"></div>`
      );
    }

    const selectedSet = new Set();
    // Draft highlight only (completed merges use ghost columns, not sel tint)
    if (rangeDraft) {
      const dA = Math.min(rangeDraft.startIdx, rangeDraft.endIdx);
      const dB = Math.max(rangeDraft.startIdx, rangeDraft.endIdx);
      for (let i = dA; i <= dB; i++) selectedSet.add(i);
    }

    if (mergeGhost) {
      for (let i = first; i <= last; i++) {
        const vc = visualCols[i];
        const w = widths[i];
        if (!vc) continue;
        if (vc.type === "merge" && vc.group && vc.group.profile) {
          parts.push(
            renderCompositeColumnHtml(vc.group, grid, 0, w, rh, fp, hh, {
              flow: true,
              faded: false,
              forceLetters: !isBarMode(),
              colorIdx: vc.group.colorIdx,
            })
          );
        } else if (vc.type === "ghost" || vc.type === "day") {
          const sid = vc.sid || sessionIds[vc.dayIdx];
          const p = profileById.get(sid);
          if (p) {
            parts.push(
              renderAlignedColumn(p, grid, w, rh, fp, hh, {
                idx: vc.dayIdx,
                faded: vc.type === "ghost",
                ghost: vc.type === "ghost",
                rangeSelected: selectedSet.has(vc.dayIdx),
                rangeAnchor: rangeAnchorIdx === vc.dayIdx,
                selectable: vc.type !== "ghost",
              })
            );
          } else {
            parts.push(placeholderCol(sid, w, spacerH));
          }
        }
      }
    } else {
      for (let i = first; i <= last; i++) {
        const sid = sessionIds[i];
        const p = profileById.get(sid);
        const w = widths[i];
        if (p) {
          parts.push(
            renderAlignedColumn(p, grid, w, rh, fp, hh, {
              idx: i,
              faded: false,
              rangeSelected: selectedSet.has(i),
              rangeAnchor: rangeAnchorIdx === i,
            })
          );
        } else {
          parts.push(placeholderCol(sid, w, spacerH));
        }
      }
    }

    const contentW = geo.contentW != null ? geo.contentW : offsets[nCols];
    const virtRight = Math.max(0, contentW - (offsets[last + 1] || 0));
    const edgePad = geo.rightEdgePad != null ? geo.rightEdgePad : RIGHT_EDGE_PAD;
    const rightPad = virtRight + edgePad;
    if (rightPad > 0) {
      parts.push(
        `<div class="virt-spacer" style="flex:0 0 ${rightPad}px;width:${rightPad}px;height:${spacerH}px"></div>`
      );
    }

    const keep = sc.scrollLeft;
    const keepTop = sc.scrollTop;

    // Hide single-pin while any composite / day-merge is active
    if (compositeActive()) {
      singlePinSegments = [];
    } else {
      computeSinglePinSegments();
    }
    const pinLayer = renderSinglePinLayerHtml(
      { offsets, contentW, rightEdgePad: edgePad },
      grid,
      rh,
      hh
    );
    const axisHtml = renderPriceAxis(grid, rh, fp, hh);
    sc.innerHTML =
      `<div class="profiles-aligned">` +
      axisHtml +
      `<div class="profiles-row" id="profilesRow" style="gap:${geo.gap}px;position:relative">` +
      pinLayer +
      `${parts.join("")}</div>` +
      `</div>`;

    sc.scrollLeft = keep;
    sc.scrollTop = keepTop;

    lastGeo = {
      widths,
      offsets,
      totalW: contentW + edgePad,
      contentW,
      rightEdgePad: edgePad,
      gap: geo.gap,
      axisW: AXIS_W,
      rh,
      hh,
      bodyH,
      barMode: isBarMode(),
      align: true,
      visualCols: mergeGhost ? visualCols : null,
      dayToVisual: geo.dayToVisual,
      mergeGhost,
    };
    updateTimeline();
    updateTrendChart();
    if (crosshair.visible) refreshCrosshairHud();

    const ms = Math.round(performance.now() - (tRender0 || performance.now()));
    updateStats(ms);
  }

  function updateStats(renderMs) {
    const loaded = profileById.size;
    const firstId = sessionIds[0] || "—";
    const lastId = sessionIds[sessionIds.length - 1] || "—";
    const lastP = profileById.get(lastId);
    const step = lastP ? lastP.step : parseFloat($("points").value) || 5;
    const g = globalGrid;
    $("stats").innerHTML = [
      `<span><b>Days</b>${sessionIds.length}</span>`,
      `<span><b>Loaded</b>${loaded}/${sessionIds.length}</span>`,
      `<span><b>From</b>${firstId}</span>`,
      `<span><b>To</b>${lastId}</span>`,
      wantAlign() && g
        ? `<span><b>Scale</b>${fmtPrice(g.minKey * g.step, step)}–${fmtPrice(
            g.maxKey * g.step,
            step
          )} (${g.nBins} bins)</span>`
        : `<span><b>Align</b>off</span>`,
      linkAxes
        ? `<span><b>Zoom</b>${Math.round(zoomX * 100)}%</span>`
        : `<span><b>Zoom</b>闊${Math.round(zoomX * 100)}%·高${Math.round(zoomY * 100)}%</span>`,
      lastP ? `<span><b>Latest POC</b>${fmtPrice(lastP.poc.price, step)}</span>` : "",
      `<span><b>Points</b>${step}</span>`,
      renderMs != null ? `<span><b>Render</b>${renderMs}ms</span>` : "",
      lastMeta && lastMeta.timingMs
        ? `<span><b>API</b>${lastMeta.timingMs.total}ms</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");
    updateZoomLabel();
  }

  function scrollToNewest() {
    requestAnimationFrame(() => {
      const sc = $("profilesScroll");
      // In aligned mode content is wider; scroll to right
      sc.scrollLeft = sc.scrollWidth;
      scheduleRender();
    });
  }

  // ---- zoom (dual-axis: zoomX / zoomY, linkAxes) ----
  function clampZoom(z) {
    return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
  }

  /**
   * Set one or both axes. opts:
   *   skipAnchor, immediate,
   *   clientX/clientY (pointer anchor; else viewport center),
   *   unlink (force linkAxes=false before apply)
   */
  function setZoomXY(nx, ny, opts) {
    opts = opts || {};
    if (opts.unlink) linkAxes = false;
    const sc = $("profilesScroll");
    const prevX = zoomX;
    const prevY = zoomY;
    const nextX = nx != null && isFinite(nx) ? clampZoom(nx) : zoomX;
    const nextY = ny != null && isFinite(ny) ? clampZoom(ny) : zoomY;
    const changedX = Math.abs(nextX - prevX) > 1e-9;
    const changedY = Math.abs(nextY - prevY) > 1e-9;
    if (!changedX && !changedY) {
      updateZoomLabel();
      return;
    }

    let keyAtPointer = null;
    let anchorContentX = null;
    let localX = null;
    let localY = null;
    let useCenterY = true;
    let useCenterX = true;

    if (sc && !opts.skipAnchor) {
      const rect = sc.getBoundingClientRect();
      if (opts.clientX != null && isFinite(opts.clientX)) {
        localX = opts.clientX - rect.left;
        useCenterX = false;
      } else {
        localX = sc.clientWidth / 2;
      }
      if (opts.clientY != null && isFinite(opts.clientY)) {
        localY = opts.clientY - rect.top;
        useCenterY = false;
      } else {
        localY = sc.clientHeight / 2;
      }
      if (changedY && globalGrid && prevY > 0) {
        const rh0 = Math.max(2, BASE_ROW_H * prevY);
        const yInBody = sc.scrollTop + localY - HEAD_H;
        keyAtPointer = globalGrid.maxKey - yInBody / rh0;
      }
      if (changedX && prevX > 0) {
        anchorContentX = sc.scrollLeft + localX;
      }
    }

    zoomX = nextX;
    zoomY = nextY;
    updateZoomLabel();

    function applyAnchors() {
      if (!sc) return;
      if (changedY && globalGrid && keyAtPointer != null) {
        const rh1 = opts.immediate
          ? Math.max(2, BASE_ROW_H * zoomY)
          : rowH();
        const hh = opts.immediate ? HEAD_H : headH();
        const newTop = (globalGrid.maxKey - keyAtPointer) * rh1 - localY + hh;
        sc.scrollTop = Math.max(0, newTop);
      }
      if (changedX && anchorContentX != null && prevX > 0) {
        const scaled = anchorContentX * (zoomX / prevX);
        sc.scrollLeft = Math.max(0, scaled - localX);
      }
    }

    if (sc && !opts.skipAnchor && (changedX || changedY)) {
      if (opts.immediate) {
        applyAnchors();
        scheduleRender();
        return;
      }
      scheduleRender();
      requestAnimationFrame(() => {
        applyAnchors();
        scheduleRender();
      });
      return;
    }
    scheduleRender();
  }

  /** Linked / both-axes convenience (old setZoom). */
  function setZoom(z, opts) {
    setZoomXY(z, z, opts);
  }

  function zoomBy(factor, axis, opts) {
    opts = opts || {};
    axis = axis || (linkAxes ? "both" : "y");
    if (axis === "x") setZoomXY(zoomX * factor, null, opts);
    else if (axis === "y") setZoomXY(null, zoomY * factor, opts);
    else setZoomXY(zoomX * factor, zoomY * factor, opts);
  }

  /** +/- step: 5% normally; 2% when current axis ≤ 10%. axis: both|x|y */
  function zoomStep(dir, axis) {
    axis = axis || (linkAxes ? "both" : "y");
    const minPct = Math.round(ZOOM_MIN * 100);
    const maxPct = Math.round(ZOOM_MAX * 100);
    function stepOne(cur) {
      const pct = Math.round(cur * 100);
      const step = pct <= 10 ? 2 : 5;
      return Math.max(minPct, Math.min(maxPct, pct + dir * step)) / 100;
    }
    if (axis === "x") setZoomXY(stepOne(zoomX), null);
    else if (axis === "y") setZoomXY(null, stepOne(zoomY));
    else {
      const n = stepOne(zoomX);
      setZoomXY(n, n);
    }
  }

  function setLinkAxes(on) {
    linkAxes = !!on;
    if (linkAxes) {
      // Snap to shared value (geometric mean of current) so link feels intentional
      const z = clampZoom(Math.sqrt(Math.max(zoomX, 1e-6) * Math.max(zoomY, 1e-6)));
      zoomX = z;
      zoomY = z;
    }
    updateZoomLabel();
    scheduleRender();
  }

  // Natural (zoom=1) total width of a slice of days, from max TPO prints
  function naturalWidthForRange(fromIdx, toIdx) {
    const gap1 = 2;
    let sum = 0;
    for (let i = fromIdx; i <= toIdx; i++) {
      const p = profileById.get(sessionIds[i]);
      const prints = p ? maxPrintsOf(p) : PLACEHOLDER_PRINTS;
      sum += prints * CHAR_W0 + COL_PAD0 + gap1;
    }
    return Math.max(1, sum);
  }

  /** mode: both | x | y — Fit 闊／高／Both */
  function zoomFitAll(mode) {
    mode = mode || (($("fitAxisSelect") && $("fitAxisSelect").value) || "both");
    if (!wantAlign()) {
      $("alignPrices").checked = true;
    }
    computeGlobalGrid();
    const sc = $("profilesScroll");
    if (!sc || !sessionIds.length) {
      scheduleRender();
      return;
    }
    const viewH = Math.max(120, sc.clientHeight - HEAD_H - 8);
    const viewW = Math.max(160, sc.clientWidth - AXIS_W - 8 - RIGHT_EDGE_PAD);
    const natW = naturalWidthForRange(0, sessionIds.length - 1);
    const fitX = clampZoom(viewW / natW);
    const fitY = clampZoom(
      globalGrid ? viewH / (globalGrid.nBins * BASE_ROW_H) : 1
    );
    if (mode === "x") {
      // Fit 闊 = X only; auto-unlink so height stays put
      linkAxes = false;
      zoomX = fitX;
    } else if (mode === "y") {
      // Fit 高 = Y only; auto-unlink so day column width stays put
      linkAxes = false;
      zoomY = fitY;
    } else {
      // Fit both axes so all days + full price range are on screen
      const z = Math.min(fitX, fitY);
      zoomX = z;
      zoomY = z;
    }
    updateZoomLabel();
    scheduleRender();
    requestAnimationFrame(() => {
      sc.scrollTop = 0;
      sc.scrollLeft = 0;
      scheduleRender();
    });
  }

  function sliceWidthAtZoom(z, fromIdx, toIdx) {
    const prev = zoomX;
    zoomX = z;
    let sum = 0;
    const gap = colGap();
    // Use full merge-ghost geometry so Fit accounts for inserted merge columns
    const geo = buildColGeometry();
    if (geo.mergeGhost && geo.dayToVisual) {
      const v0 = geo.dayToVisual[fromIdx];
      const v1 = geo.dayToVisual[toIdx];
      if (v0 >= 0 && v1 >= 0) {
        // Include any merge column immediately before v0 if that merge covers fromIdx
        let startV = v0;
        if (
          geo.visualCols &&
          startV > 0 &&
          geo.visualCols[startV - 1] &&
          geo.visualCols[startV - 1].type === "merge" &&
          geo.visualCols[startV - 1].startIdx >= fromIdx &&
          geo.visualCols[startV - 1].startIdx <= toIdx
        ) {
          startV = startV - 1;
        }
        // Also: if fromIdx starts a merge, dayToVisual[fromIdx] is the ghost; merge is before it
        if (
          geo.visualCols &&
          v0 > 0 &&
          geo.visualCols[v0 - 1] &&
          geo.visualCols[v0 - 1].type === "merge" &&
          geo.visualCols[v0 - 1].startIdx === fromIdx
        ) {
          startV = v0 - 1;
        }
        for (let v = startV; v <= v1; v++) {
          sum += (geo.widths[v] || 0) + gap;
        }
        zoomX = prev;
        return sum;
      }
    }
    for (let i = fromIdx; i <= toIdx; i++) {
      const p = profileById.get(sessionIds[i]);
      sum += (p ? dayColW(p) : dayColWFromPrints(PLACEHOLDER_PRINTS)) + gap;
    }
    zoomX = prev;
    return sum;
  }

  /** Indices of the latest trading week (Mon-start) among loaded day sessions. */
  function latestWeekDayRange() {
    if (!sessionIds.length) return null;
    const toIdx = sessionIds.length - 1;
    const wk = weekStartKey(sessionIds[toIdx]);
    let fromIdx = toIdx;
    while (fromIdx > 0 && weekStartKey(sessionIds[fromIdx - 1]) === wk) fromIdx--;
    return { fromIdx: fromIdx, toIdx: toIdx, weekKey: wk };
  }

  function midKeyForDayRange(fromIdx, toIdx) {
    let sum = 0;
    let n = 0;
    for (let i = fromIdx; i <= toIdx; i++) {
      const p = profileById.get(sessionIds[i]);
      if (!p) continue;
      if (p.poc && p.poc.key != null) {
        sum += p.poc.key;
        n++;
        continue;
      }
      if (p.rows && p.rows.length) {
        let hi = -Infinity;
        let lo = Infinity;
        for (let j = 0; j < p.rows.length; j++) {
          const k = p.rows[j].key;
          if (k > hi) hi = k;
          if (k < lo) lo = k;
        }
        if (isFinite(hi) && isFinite(lo)) {
          sum += (hi + lo) / 2;
          n++;
        }
      }
    }
    if (n) return sum / n;
    if (globalGrid) return (globalGrid.minKey + globalGrid.maxKey) / 2;
    return null;
  }

  // Fit day columns [fromIdx..toIdx] into the viewport (day HG geometry).
  // Primarily an X fit; when linked, Y follows the same value (old behavior).
  function zoomFitDayRange(fromIdx, toIdx, opts) {
    opts = opts || {};
    if (!wantAlign()) {
      $("alignPrices").checked = true;
    }
    computeGlobalGrid();
    const sc = $("profilesScroll");
    if (!sc || !sessionIds.length) {
      scheduleRender();
      return;
    }
    fromIdx = Math.max(0, Math.min(fromIdx, sessionIds.length - 1));
    toIdx = Math.max(fromIdx, Math.min(toIdx, sessionIds.length - 1));
    // Reserve right-edge breathing room so newest columns aren't flush
    const viewW = Math.max(160, sc.clientWidth - AXIS_W - 8 - RIGHT_EDGE_PAD);

    const zMin = opts.minZoom != null ? opts.minZoom : ZOOM_MIN;
    const zMax = opts.maxZoom != null ? opts.maxZoom : Math.min(ZOOM_MAX, 1.2);
    let lo = ZOOM_MIN;
    let hi = Math.max(lo, zMax);
    for (let i = 0; i < 18; i++) {
      const mid = (lo + hi) / 2;
      if (sliceWidthAtZoom(mid, fromIdx, toIdx) <= viewW) lo = mid;
      else hi = mid;
    }
    // Prefer fitting; optional floor so a single day stays letter-readable
    let z = Math.max(zMin, Math.min(zMax, lo));
    // If even ZOOM_MIN is wider than view, stay at searched lo (may H-scroll)
    if (lo < zMin && sliceWidthAtZoom(zMin, fromIdx, toIdx) > viewW * 1.35) {
      z = lo;
    }
    zoomX = z;
    if (linkAxes) zoomY = z;
    updateZoomLabel();
    scheduleRender();
    requestAnimationFrame(() => {
      // Align so the range's right edge (latest) sits near the right of the view
      const geo = buildColGeometry();
      const right = dayOffsetRight(geo, toIdx);
      const targetLeft = Math.max(0, right + (geo.rightEdgePad || RIGHT_EDGE_PAD) * 0.15 - sc.clientWidth + AXIS_W);
      // Fallback: full right scroll when geometry still settling
      sc.scrollLeft = Math.max(targetLeft, Math.max(0, sc.scrollWidth - sc.clientWidth));
      if (globalGrid) {
        const rh = rowH();
        const hh = headH();
        let midKey = midKeyForDayRange(fromIdx, toIdx);
        if (midKey == null) midKey = (globalGrid.minKey + globalGrid.maxKey) / 2;
        const top = (globalGrid.maxKey - midKey) * rh - sc.clientHeight / 2 + hh;
        sc.scrollTop = Math.max(0, top);
      }
      scheduleRender();
      if (opts.flashSid) {
        requestAnimationFrame(() => flashDayColumn(opts.flashSid));
      }
    });
  }

  function flashDayColumn(sid) {
    const sc = $("profilesScroll");
    if (!sc || !sid) return;
    const el = sc.querySelector('.day-col[data-sid="' + sid + '"]');
    if (!el) return;
    el.classList.add("day-jump-flash");
    setTimeout(() => {
      try { el.classList.remove("day-jump-flash"); } catch (_) {}
    }, 1400);
  }

  // Fit ~nDays trailing columns; bar-mode floor keeps HG silhouette readable (may need H-scroll)
  function zoomFitDays(nDays) {
    const n = Math.min(Math.max(1, nDays), sessionIds.length || 1);
    const toIdx = sessionIds.length - 1;
    const fromIdx = Math.max(0, toIdx - n + 1);
    zoomFitDayRange(fromIdx, toIdx);
  }

  /** Default load: day HG for the latest trading week (not Fit-all / not 60d). */
  function zoomFitRecentWeek() {
    const range = latestWeekDayRange();
    if (!range) {
      scrollToNewest();
      return;
    }
    // Ensure day walls (not week/month merge) for the default view
    if (weekHgOn || monthHgOn) {
      weekHgOn = false;
      monthHgOn = false;
      clearRangeSelection();
      syncCompositeButtons();
    }
    zoomFitDayRange(range.fromIdx, range.toIdx);
  }

  /** One-tap: exit merge if needed, keep zoom, scroll to latest day + flash. */
  function jumpToLatestTradingDay() {
    if (!sessionIds.length) return;
    if (weekHgOn || monthHgOn) {
      weekHgOn = false;
      monthHgOn = false;
      // keep completed day-merge groups; only drop in-progress draft
      rangeAnchorIdx = null;
      rangeDraft = null;
      syncCompositeButtons();
    } else if (rangeAnchorIdx != null || rangeDraft) {
      rangeAnchorIdx = null;
      rangeDraft = null;
      syncCompositeButtons();
    }
    const toIdx = sessionIds.length - 1;
    const sid = sessionIds[toIdx];
    // Do NOT change zoom / display ratio — only align + flash
    scheduleRender();
    requestAnimationFrame(() => {
      const sc = $("profilesScroll");
      if (!sc) return;
      const geo = buildColGeometry();
      const right = dayOffsetRight(geo, toIdx);
      const targetLeft = Math.max(
        0,
        right + (geo.rightEdgePad || RIGHT_EDGE_PAD) * 0.15 - sc.clientWidth + AXIS_W
      );
      sc.scrollLeft = Math.max(targetLeft, Math.max(0, sc.scrollWidth - sc.clientWidth));
      if (globalGrid) {
        const rh = rowH();
        const hh = headH();
        let midKey = midKeyForDayRange(toIdx, toIdx);
        if (midKey == null) midKey = (globalGrid.minKey + globalGrid.maxKey) / 2;
        const top = (globalGrid.maxKey - midKey) * rh - sc.clientHeight / 2 + hh;
        sc.scrollTop = Math.max(0, top);
      }
      scheduleRender();
      requestAnimationFrame(() => flashDayColumn(sid));
    });
  }

  function zoomReset() {
    setZoom(1, { skipAnchor: true });
  }

  function qsParams(extra) {
    const preset = currentRange();
    const qs = new URLSearchParams({
      source: $("source").value,
      symbol: ($("symbol").value || "NQ1!").trim(),
      interval: "5m",
      range: preset.yahooRange,
      lookback_days: String(preset.lookbackDays),
      sessions: String(preset.sessions),
      range_mode: $("rangeMode").value || "6m",
      points: $("points").value,
      block_min: $("blockMin").value,
      open: $("sessOpen").value,
      close: $("sessClose").value,
      tz: $("tz").value,
      alphabet: $("alphabet").value,
      first_letter: $("firstLetter").value,
      va_pct: $("vaPct").value,
      tail: "1",
    });
    const date = ($("sessDate").value || "").trim();
    if (date) qs.set("date", date);
    if (extra) {
      for (const [k, v] of Object.entries(extra)) qs.set(k, String(v));
    }
    return qs;
  }

  function ingestProfiles(payload) {
    if (payload.sessionIds && payload.sessionIds.length) {
      sessionIds = payload.sessionIds.slice();
    }
    for (const p of payload.profiles || []) {
      profileById.set(p.sessionId, p);
    }
    lastMeta = payload;
    if (wantAlign()) computeGlobalGrid();
  }

  async function fillOlder() {
    if (STATIC_MODE) return;
    if (fillInFlight) return;
    const total = sessionIds.length;
    const loaded = profileById.size;
    if (loaded >= total) return;
    fillInFlight = true;
    try {
      let suffix = 0;
      for (let i = sessionIds.length - 1; i >= 0; i--) {
        if (profileById.has(sessionIds[i])) suffix++;
        else break;
      }
      const qs = qsParams({
        limit: FILL_CHUNK,
        offset: suffix,
        tail: "1",
      });
      const t0 = performance.now();
      if (STATIC_MODE) return;
      const res = await fetch("/api/profiles?" + qs.toString());
      const payload = await readResponseJson(res, "/api/profiles");
      if (!res.ok || payload.error) throw new Error(payload.error || "HTTP " + res.status);
      ingestProfiles(payload);
      profilesLoading = false;
      const ms = Math.round(performance.now() - t0);
      showSrcNote(
        [
          payload.source,
          payload.symbol,
          `${profileById.size}/${sessionIds.length} days`,
          `fill +${(payload.profiles || []).length} in ${ms}ms`,
          payload.cacheHits != null ? `cache hit ${payload.cacheHits}` : "",
          globalGrid
            ? `scale ${fmtPrice(globalGrid.minKey * globalGrid.step, globalGrid.step)}–${fmtPrice(
                globalGrid.maxKey * globalGrid.step,
                globalGrid.step
              )}`
            : "",
          payload.note,
        ]
          .filter(Boolean)
          .join(" · ")
      );
      scheduleRender();
      if (profileById.size < sessionIds.length) {
        setTimeout(() => {
          fillInFlight = false;
          fillOlder();
        }, 16);
        return;
      }
    } catch (e) {
      console.error(e);
      showErr("Background fill: " + (e.message || e));
    } finally {
      fillInFlight = false;
    }
  }

  async function fetchAndBuild(opts) {
    opts = opts || {};
    showErr("");
    showSrcNote(STATIC_MODE ? "Loading static snapshot…" : "Loading…");
    const btn = $("btnFetch");
    btn.disabled = true;
    const t0 = performance.now();
    try {
      profilesLoading = true;
      profileById = new Map();
      compositeCache.clear();
      sessionIds = [];
      globalGrid = null;
      setAlignDefaultForRange();
      let payload;
      if (STATIC_MODE && opts.refresh) staticBundle = null;
      if (STATIC_MODE) {
        const preset = currentRange();
        lastParamsKey = "static:" + (($("rangeMode") && $("rangeMode").value) || DEFAULT_RANGE);
        if (!staticBundle) staticBundle = await loadStaticProfiles();
        if (!staticBundle || staticBundle.error) {
          throw new Error((staticBundle && staticBundle.error) || "static load failed");
        }
        payload = applyRangeToStatic(staticBundle, preset);
      } else {
      const firstLimit =
        currentRange().sessions <= FIRST_PAINT ? currentRange().sessions : FIRST_PAINT;
      const qs = qsParams({
        limit: firstLimit,
        offset: 0,
        tail: "1",
        refresh: opts.refresh ? "1" : "0",
      });
      lastParamsKey = qs.toString();
      const res = await fetch("/api/profiles?" + qs.toString());
      payload = await readResponseJson(res, "/api/profiles");
      if (!res.ok || payload.error) throw new Error(payload.error || "HTTP " + res.status);
      }

      if ((payload.symbol || "").includes("HSI")) {
        if ($("tz").value === "America/New_York") $("tz").value = "Asia/Hong_Kong";
      }

      ingestProfiles(payload);
      const ms = Math.round(performance.now() - t0);
      const wantS = currentRange().sessions;
      const haveS = sessionIds.length;
      const availS = payload.availableSessions != null ? payload.availableSessions : payload.total;
      const rangeLabel = currentRange().label || wantS;
      showSrcNote(
        [
          payload.source,
          payload.symbol,
          payload.shortName,
          `${haveS}/${wantS} · ${rangeLabel}` +
            (availS != null && availS < wantS ? ` (available ${availS})` : ""),
          STATIC_MODE ? `${ms}ms static` : `${ms}ms (api ${payload.timingMs && payload.timingMs.total}ms)`,
          wantAlign() ? "aligned scale" : "per-day scale",
          payload.barsFrom,
          !STATIC_MODE && payload.cacheMisses
            ? `built ${payload.rebuilt}`
            : STATIC_MODE
              ? "baked snapshot"
              : "cache warm",
          payload.note,
        ]
          .filter(Boolean)
          .join(" · ")
      );
      scheduleRender();
      // After first paint: day HG of the latest trading week (not Fit-all)
      requestAnimationFrame(() => zoomFitRecentWeek());

      if (profileById.size < sessionIds.length) {
        setTimeout(fillOlder, 30);
      }
    } catch (e) {
      console.error(e);
      profilesLoading = false;
      showErr(e.message || String(e));
    } finally {
      profilesLoading = false;
      btn.disabled = false;
    }
  }

  function rebuildVisible() {
    if (STATIC_MODE) {
      staticBundle = null;
      showSrcNote("靜態版：Reload snapshot（無 Flask API）…");
      fetchAndBuild({ refresh: false });
      return;
    }
    fetchAndBuild({ refresh: false });
  }

  async function loadSample() {
    showErr("");
    try {
      const res = await fetch("sample.csv");
      if (!res.ok) throw new Error("Could not fetch sample.csv");
      $("csvText").value = await res.text();
      $("sessOpen").value = "0930";
      $("sessClose").value = "1600";
      $("tz").value = "America/New_York";
      $("points").value = "5";
      $("alphabet").value = "hk";
      $("firstLetter").value = "D";
      $("rangeMode").value = "1m";
      document.querySelectorAll(".range-btn").forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-range") === "1m");
      });
      showSrcNote("Sample is US-hours demo; prefer Fetch HSIF.");
      showErr("Sample CSV is fallback only. Click Fetch & build for HSIF (fast cached path).");
    } catch (e) {
      showErr(e.message || String(e));
    }
  }


  // ---- timeline + crosshair (date + price points) ----
  function hideCrosshair() {
    crosshair.visible = false;
    const ov = $("crosshairOverlay");
    if (ov) ov.classList.remove("show");
    const tl = $("tlReadout");
    if (tl) tl.innerHTML = "Date · <b>Pts</b>";
    updateTrendChart();
  }

  function updateTimeline() {
    const inner = $("timelineInner");
    const sc = $("profilesScroll");
    if (!inner || !sc) return;
    if (!lastGeo || !sessionIds.length || !lastGeo.offsets) {
      inner.innerHTML = "";
      inner.style.width = "0px";
      return;
    }
    const { widths, offsets, totalW, axisW } = lastGeo;
    const pad = 8;
    const fullW = axisW + totalW + pad;
    inner.style.width = fullW + "px";

    let idList;
    if (lastGeo.groupMode && lastGeo.groupLabels && lastGeo.groupLabels.length) {
      idList = lastGeo.groupLabels;
    } else if (lastGeo.visualCols && lastGeo.visualCols.length) {
      idList = lastGeo.visualCols.map((vc) => {
        if (vc.type === "merge" && vc.group) return vc.group.label || "合併";
        return vc.sid || sessionIds[vc.dayIdx] || "";
      });
    } else {
      idList = sessionIds;
    }
    const ticks = [];
    let lastX = -999;
    const avgW = idList.length ? totalW / idList.length : 40;
    const stepN = Math.max(1, Math.ceil(42 / Math.max(8, avgW)));
    for (let i = 0; i < idList.length; i += stepN) {
      const x = axisW + offsets[i];
      if (x - lastX < 36 && i !== 0) continue;
      lastX = x;
      let label = idList[i];
      if (/^\d{4}-\d{2}-\d{2}/.test(label)) label = label.slice(5);
      else if (typeof label === "string" && label.indexOf("月 ") === 0) label = label.slice(2);
      else if (typeof label === "string" && label.indexOf("週 ") === 0) label = label.slice(2);
      else if (typeof label === "string" && label.indexOf(" 合併") > 0) {
        label = label.replace(" 合併", "");
      }
      const w = Math.max(widths[i] || 24, 24);
      ticks.push(
        `<div class="tl-tick" data-idx="${i}" style="left:${x}px;width:${w}px">${escapeHtml(label)}</div>`
      );
    }
    const last = idList.length - 1;
    if (last > 0 && last % stepN !== 0) {
      const x = axisW + offsets[last];
      let label = idList[last];
      if (/^\d{4}-\d{2}-\d{2}/.test(label)) label = label.slice(5);
      else if (typeof label === "string" && label.indexOf("月 ") === 0) label = label.slice(2);
      else if (typeof label === "string" && label.indexOf("週 ") === 0) label = label.slice(2);
      else if (typeof label === "string" && label.indexOf(" 合併") > 0) {
        label = label.replace(" 合併", "");
      }
      ticks.push(
        `<div class="tl-tick" data-idx="${last}" style="left:${x}px;width:${Math.max(widths[last] || 24, 24)}px">${escapeHtml(label)}</div>`
      );
    }
    inner.innerHTML = ticks.join("");
    syncTimelineScroll();
  }

  function syncTimelineScroll() {
    const inner = $("timelineInner");
    const sc = $("profilesScroll");
    if (!inner || !sc) return;
    inner.style.transform = `translateX(${-sc.scrollLeft}px)`;
  }

  function updateTrendChart() {
    const canvas = $("trendCanvas");
    const sc = $("profilesScroll");
    const badge = document.querySelector(".trend-badge");
    if (!canvas || !sc) return;
    if (!lastGeo || !sessionIds.length) {
      const ctx0 = canvas.getContext("2d");
      if (ctx0) {
        canvas.width = canvas.clientWidth || 1;
        canvas.height = canvas.clientHeight || 1;
        ctx0.clearRect(0, 0, canvas.width, canvas.height);
      }
      return;
    }

    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(1, canvas.clientWidth || sc.clientWidth || 800);
    const cssH = Math.max(1, canvas.clientHeight || 84);
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const axisW = lastGeo.align ? (lastGeo.axisW || 0) : 0;
    const { widths, offsets, contentW } = lastGeo;
    const cw = contentW != null ? contentW : lastGeo.totalW - (lastGeo.rightEdgePad || 0);
    if (!widths || !offsets) return;

    const groupMode = !!(lastGeo.groupMode && lastGeo.groups && lastGeo.groups.length);
    const plotL = axisW;
    const plotR = cssW;
    const plotT = 16;
    const plotB = cssH - 6;
    const plotH = Math.max(1, plotB - plotT);

    // Collect points; week/month also keep each column's actual H/L for Y scale
    const pts = [];
    const nCols = widths.length;
    const visualCols = lastGeo.visualCols;
    for (let i = 0; i < nCols; i++) {
      let price = null;
      let sid = sessionIds[i];
      let hi = null;
      let lo = null;
      if (groupMode && lastGeo.groups[i]) {
        const g = lastGeo.groups[i];
        price = groupTrendPrice(g);
        sid = (g.sids && g.sids.length && g.sids[g.sids.length - 1]) || g.label;
        const hl = profileHiLo(g.profile);
        if (hl) {
          hi = hl.hi;
          lo = hl.lo;
        }
      } else if (visualCols && visualCols[i]) {
        const vc = visualCols[i];
        // Skip merge columns in trend — keep one mid per calendar day (ghost/day)
        if (vc.type === "merge") continue;
        sid = vc.sid || sessionIds[vc.dayIdx];
        const p = profileById.get(sid);
        price = p ? sessionTrendPrice(p) : null;
        const hl = profileHiLo(p);
        if (hl) {
          hi = hl.hi;
          lo = hl.lo;
        }
      } else {
        const p = profileById.get(sessionIds[i]);
        price = p ? sessionTrendPrice(p) : null;
        const hl = profileHiLo(p);
        if (hl) {
          hi = hl.hi;
          lo = hl.lo;
        }
      }
      if (price == null) continue;
      if (hi == null) hi = price;
      if (lo == null) lo = price;
      const xCenter = axisW + offsets[i] + widths[i] / 2 - sc.scrollLeft;
      pts.push({ i, sid, price, hi, lo, xCenter, w: widths[i] });
    }
    if (!pts.length) return;

    // Visible columns (with small x pad for edge continuity)
    let visible = pts.filter((p) => p.xCenter >= plotL - 40 && p.xCenter <= plotR + 40);
    if (!visible.length) visible = pts.slice(); // fallback if scroll lands in pad

    // Y scale:
    // - Week/month HG: actual high-low of *visible* composites (often 1000+ pts) —
    //   never reuse a narrow day-mid span that clips/squashes the line.
    // - Day wall: mid-to-mid of visible days (unchanged feel).
    let yMin = Infinity;
    let yMax = -Infinity;
    if (groupMode) {
      for (const p of visible) {
        if (p.lo < yMin) yMin = p.lo;
        if (p.hi > yMax) yMax = p.hi;
      }
    } else {
      for (const p of visible) {
        if (p.price < yMin) yMin = p.price;
        if (p.price > yMax) yMax = p.price;
      }
    }
    if (!isFinite(yMin) || !isFinite(yMax)) return;

    const step = globalGrid ? globalGrid.step : 5;
    // Week/month: keep modest pad but never shrink below a readable band
    const minSpan = groupMode ? Math.max(200, step * 40) : step * 2;
    let span = yMax - yMin;
    if (span < minSpan) {
      const mid = (yMin + yMax) / 2;
      yMin = mid - minSpan / 2;
      yMax = mid + minSpan / 2;
      span = minSpan;
    }
    const padY = Math.max(span * (groupMode ? 0.06 : 0.12), step * 2);
    yMin -= padY;
    yMax += padY;
    if (yMax <= yMin) {
      yMin -= 10;
      yMax += 10;
    }

    function yOf(price) {
      return plotT + ((yMax - price) / (yMax - yMin)) * plotH;
    }

    // axis gutter
    if (axisW > 0) {
      ctx.fillStyle = "#0a0e14";
      ctx.fillRect(0, 0, axisW, cssH);
      ctx.strokeStyle = "#2d3a4f";
      ctx.beginPath();
      ctx.moveTo(axisW + 0.5, 0);
      ctx.lineTo(axisW + 0.5, cssH);
      ctx.stroke();
      ctx.fillStyle = "#8b9bb4";
      ctx.font = "10px ui-monospace, Menlo, Consolas, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(fmtPrice(yMax, step), axisW - 4, plotT + 4);
      ctx.fillText(fmtPrice(yMin, step), axisW - 4, plotB - 4);
      const midP = (yMin + yMax) / 2;
      ctx.fillText(fmtPrice(midP, step), axisW - 4, yOf(midP));
    }

    // clip to plot area
    ctx.save();
    ctx.beginPath();
    ctx.rect(plotL, 0, plotR - plotL, cssH);
    ctx.clip();

    // grid
    ctx.strokeStyle = "#1a2332";
    ctx.lineWidth = 1;
    for (let g = 0; g < 3; g++) {
      const yy = plotT + (plotH * g) / 2;
      ctx.beginPath();
      ctx.moveTo(plotL, yy);
      ctx.lineTo(plotR, yy);
      ctx.stroke();
    }

    // line + fill (visible columns)
    if (visible.length) {
      ctx.beginPath();
      ctx.moveTo(visible[0].xCenter, yOf(visible[0].price));
      for (let k = 1; k < visible.length; k++) {
        ctx.lineTo(visible[k].xCenter, yOf(visible[k].price));
      }
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 1.75;
      ctx.lineJoin = "round";
      ctx.stroke();

      // soft fill
      ctx.lineTo(visible[visible.length - 1].xCenter, plotB);
      ctx.lineTo(visible[0].xCenter, plotB);
      ctx.closePath();
      ctx.fillStyle = "#38bdf822";
      ctx.fill();

      // dots when columns are wide enough (use column count, not day count)
      const avgW = nCols ? cw / nCols : 20;
      if (avgW >= 10) {
        for (const p of visible) {
          ctx.beginPath();
          ctx.arc(p.xCenter, yOf(p.price), avgW >= 28 ? 2.5 : 1.5, 0, Math.PI * 2);
          ctx.fillStyle = "#7dd3fc";
          ctx.fill();
        }
      }
    }

    // crosshair marker (group-aware)
    if (crosshair.visible && crosshair.colIdx != null && crosshair.colIdx >= 0) {
      const i = crosshair.colIdx;
      if (i < nCols && widths[i] != null) {
        const x = axisW + offsets[i] + widths[i] / 2 - sc.scrollLeft;
        let price = null;
        if (groupMode && lastGeo.groups[i]) {
          price = groupTrendPrice(lastGeo.groups[i]);
        } else if (lastGeo.visualCols && lastGeo.visualCols[i]) {
          const vc = lastGeo.visualCols[i];
          if (vc.type === "merge" && vc.group) {
            price = groupTrendPrice(vc.group);
          } else {
            const p = profileById.get(vc.sid || sessionIds[vc.dayIdx]);
            price = p ? sessionTrendPrice(p) : null;
          }
        } else {
          const p = profileById.get(sessionIds[i]);
          price = p ? sessionTrendPrice(p) : null;
        }
        ctx.strokeStyle = "#93c5fd99";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, cssH);
        ctx.stroke();
        if (price != null) {
          const yy = yOf(price);
          ctx.beginPath();
          ctx.arc(x, yy, 3.5, 0, Math.PI * 2);
          ctx.fillStyle = "#fbbf24";
          ctx.fill();
          ctx.strokeStyle = "#0a0e14";
          ctx.stroke();
        }
      }
    }

    ctx.restore();

    if (badge && pts.length) {
      const last = pts[pts.length - 1];
      const spanPts = Math.round(yMax - yMin);
      const modeTag = groupMode ? (lastGeo.groupMode === "month" ? "月" : "週") + " H–L" : "mid";
      badge.innerHTML =
        `Trend · <b>${modeTag}</b> · last ${escapeHtml(fmtPrice(last.price, step))}` +
        (groupMode ? ` · scale ${escapeHtml(String(spanPts))}pts` : "");
    }
  }



  function hitTestChart(clientX, clientY) {
    const sc = $("profilesScroll");
    if (!sc || !lastGeo || !lastGeo.align || !globalGrid) return null;
    const rect = sc.getBoundingClientRect();
    const localX = clientX - rect.left;
    const localY = clientY - rect.top;
    if (localX < 0 || localY < 0 || localX > rect.width || localY > rect.height) return null;

    const style = getComputedStyle(sc);
    const padL = parseFloat(style.paddingLeft) || 0;
    const padT = parseFloat(style.paddingTop) || 0;
    const xContent = localX + sc.scrollLeft - padL;
    const yContent = localY + sc.scrollTop - padT;

    const axisW = lastGeo.axisW || 0;
    const xInRow = xContent - axisW;
    let colIdx = 0;
    let sid = null;
    if (xInRow >= 0 && lastGeo.offsets) {
      const nCols = lastGeo.widths ? lastGeo.widths.length : sessionIds.length;
      colIdx = findColAtX(lastGeo.offsets, xInRow);
      colIdx = Math.max(0, Math.min(nCols - 1, colIdx));
      if (lastGeo.groupMode && lastGeo.groups && lastGeo.groups[colIdx]) {
        const g = lastGeo.groups[colIdx];
        sid = (g.sids && g.sids.length && g.sids[g.sids.length - 1]) || g.label;
      } else if (lastGeo.visualCols && lastGeo.visualCols[colIdx]) {
        const vc = lastGeo.visualCols[colIdx];
        if (vc.type === "merge" && vc.group) {
          sid = vc.group.label || vc.sid;
        } else {
          sid = vc.sid || sessionIds[vc.dayIdx];
        }
      } else {
        sid = sessionIds[colIdx];
      }
    }

    const yInBody = yContent - lastGeo.hh;
    const key = Math.round(globalGrid.maxKey - yInBody / Math.max(0.01, lastGeo.rh));
    const clampedKey = Math.max(globalGrid.minKey, Math.min(globalGrid.maxKey, key));
    const price = clampedKey * globalGrid.step;

    return {
      localX,
      localY,
      colIdx,
      sid,
      key: clampedKey,
      price,
      step: globalGrid.step,
    };
  }

  function refreshCrosshairHud() {
    if (!crosshair.visible) return;
    const ov = $("crosshairOverlay");
    const hud = $("chHud");
    const v = $("chV");
    const h = $("chH");
    const tl = $("tlReadout");
    if (!ov || !hud || !v || !h) return;
    ov.classList.add("show");
    v.style.left = crosshair.localX + "px";
    h.style.top = crosshair.localY + "px";

    const dateStr = crosshair.sid || "—";
    const ptsStr = crosshair.price != null
      ? fmtPrice(crosshair.price, crosshair.step || 5) + " pts"
      : "—";
    hud.innerHTML =
      `<span class="ch-date">${escapeHtml(dateStr)}</span>` +
      `<span class="ch-sep">·</span>` +
      `<span class="ch-pts">${escapeHtml(ptsStr)}</span>`;

    const sc = $("profilesScroll");
    const maxX = (sc && sc.clientWidth) || 400;
    const maxY = (sc && sc.clientHeight) || 300;
    let hx = crosshair.localX + 12;
    let hy = crosshair.localY + 12;
    if (hx > maxX - 160) hx = crosshair.localX - 160;
    if (hy > maxY - 40) hy = crosshair.localY - 36;
    hud.style.transform = `translate(${Math.max(4, hx)}px, ${Math.max(4, hy)}px)`;

    if (tl) {
      tl.innerHTML = `${escapeHtml(dateStr)} · <b>${escapeHtml(ptsStr)}</b>`;
    }

    document.querySelectorAll(".tl-tick.active").forEach((el) => el.classList.remove("active"));
    if (crosshair.colIdx != null) {
      const tick = document.querySelector(`.tl-tick[data-idx="${crosshair.colIdx}"]`);
      if (tick) tick.classList.add("active");
    }
    updateTrendChart();
  }

  function applyCrosshairHit(hit, lock) {
    if (!hit || hit.price == null) return;
    crosshair.visible = true;
    crosshair.price = hit.price;
    crosshair.key = hit.key;
    crosshair.step = hit.step;
    crosshair.localY = hit.localY;
    if (lock) {
      crosshair.sid = lock.sid;
      crosshair.colIdx = lock.colIdx;
      crosshair.localX = lock.localX;
    } else {
      crosshair.sid = hit.sid;
      crosshair.colIdx = hit.colIdx;
      crosshair.localX = hit.localX;
    }
    refreshCrosshairHud();
  }

  function onChartPointer(ev) {
    const hit = hitTestChart(ev.clientX, ev.clientY);
    if (!hit || !hit.sid) {
      hideCrosshair();
      return;
    }
    applyCrosshairHit(hit, null);
  }

  function endChDrag(ev) {
    if (!chDrag) return;
    if (ev && ev.pointerId != null && chDrag.pointerId !== ev.pointerId) return;
    const sc = $("profilesScroll");
    if (sc && chDrag.pointerId != null) {
      try { sc.releasePointerCapture(chDrag.pointerId); } catch (_) {}
    }
    chDrag = null;
  }

  // Range toggle
  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.getAttribute("data-range");
      $("rangeMode").value = mode;
      document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      setAlignDefaultForRange();
      fetchAndBuild();
    });
  });

  $("btnFetch").addEventListener("click", () => fetchAndBuild({ refresh: true }));
  $("btnBuild").addEventListener("click", rebuildVisible);
  $("btnSample").addEventListener("click", loadSample);
  $("btnFile").addEventListener("click", () => $("fileInput").click());

  $("alignPrices").addEventListener("change", () => {
    scheduleRender();
    if (wantAlign() && profileById.size) {
      computeGlobalGrid();
      scheduleRender();
    }
  });

  function zoomStepAxisFromEvent(ev) {
    if (linkAxes) return "both";
    return ev && ev.shiftKey ? "x" : "y";
  }
  $("btnZoomIn").addEventListener("click", (ev) => zoomStep(1, zoomStepAxisFromEvent(ev)));
  $("btnZoomOut").addEventListener("click", (ev) => zoomStep(-1, zoomStepAxisFromEvent(ev)));
  $("btnZoomFit").addEventListener("click", () => zoomFitAll());
  const fitSel = $("fitAxisSelect");
  if (fitSel) {
    fitSel.addEventListener("change", () => {
      /* selection only; Fit button applies */
    });
  }
  const linkBtn = $("btnLinkAxes");
  if (linkBtn) {
    linkBtn.addEventListener("click", () => setLinkAxes(!linkAxes));
  }
  const zoomSel = $("zoomSelect");
  if (zoomSel) {
    zoomSel.addEventListener("change", () => {
      const pct = parseInt(zoomSel.value, 10);
      if (!Number.isFinite(pct) || pct <= 0) return;
      // Preset = one-shot both axes (even when unlinked)
      setZoom(pct / 100);
    });
  }
  const btn30 = $("btnView30");
  const btn60 = $("btnView60");
  if (btn30) btn30.addEventListener("click", () => zoomFitDays(30));
  if (btn60) btn60.addEventListener("click", () => zoomFitDays(60));
  const btnLatest = $("btnLatestDay");
  if (btnLatest) btnLatest.addEventListener("click", () => jumpToLatestTradingDay());

  // Wheel: Ctrl/Cmd = both axes; Shift = X only (auto-unlink);
  // bare wheel over left price axis = Y only (auto-unlink)
  $("profilesScroll").addEventListener(
    "wheel",
    (ev) => {
      const ctrl = ev.ctrlKey || ev.metaKey;
      const shift = ev.shiftKey;
      const onAxis =
        ev.target &&
        ev.target.closest &&
        ev.target.closest(".price-axis");
      if (!ctrl && !shift && !onAxis) return;
      ev.preventDefault();
      const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
      const anchor = { clientX: ev.clientX, clientY: ev.clientY };
      if (ctrl) {
        // Overall zoom — both axes
        setZoomXY(zoomX * factor, zoomY * factor, anchor);
      } else if (shift) {
        // Shift+wheel → X; unlink so Y stays put
        setZoomXY(zoomX * factor, null, Object.assign({ unlink: true }, anchor));
      } else {
        // Wheel on price axis → Y only (TV price-scale feel)
        setZoomXY(null, zoomY * factor, Object.assign({ unlink: true }, anchor));
      }
    },
    { passive: false }
  );

  // Mobile pinch-zoom (2-finger) — P1+P2: keep linked (both axes same); P4 may split
  (function bindPinchZoom() {
    const sc = $("profilesScroll");
    if (!sc) return;
    const PINCH_SENS = 1.85; // >1 ⇒ same finger travel = larger zoom change
    let pinch = null; // { dist0, zoom0, midX, midY, gesture? }
    let lastApplied = 0;
    function distOf(t0, t1) {
      const dx = t0.clientX - t1.clientX;
      const dy = t0.clientY - t1.clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }
    function midOf(t0, t1) {
      return {
        clientX: (t0.clientX + t1.clientX) / 2,
        clientY: (t0.clientY + t1.clientY) / 2,
      };
    }
    function applyPinchScale(rawRatio) {
      if (!(rawRatio > 0) || !isFinite(rawRatio) || !pinch) return;
      const boosted = Math.pow(rawRatio, PINCH_SENS);
      const next = pinch.zoom0 * boosted;
      // Skip tiny no-ops to cut wasted renders, but stay responsive (~every frame)
      const ref = Math.max(zoomX, 1e-6);
      if (Math.abs(next - zoomX) / ref < 0.004 && Math.abs(next - zoomY) / Math.max(zoomY, 1e-6) < 0.004) {
        return;
      }
      lastApplied = next;
      setZoomXY(next, next, {
        immediate: true,
        clientX: pinch.midX,
        clientY: pinch.midY,
      });
    }
    sc.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches.length === 2) {
          const m = midOf(ev.touches[0], ev.touches[1]);
          // Linked pinch uses geometric mean as start so both axes move together
          const z0 = Math.sqrt(Math.max(zoomX, 1e-6) * Math.max(zoomY, 1e-6));
          pinch = {
            dist0: distOf(ev.touches[0], ev.touches[1]),
            zoom0: z0,
            midX: m.clientX,
            midY: m.clientY,
          };
          lastApplied = z0;
          // Pinch supersedes price-drag / pinned crosshair
          if (chDrag) endChDrag(null);
          if (crosshair.visible) hideCrosshair();
        } else if (ev.touches.length < 2) {
          pinch = null;
        }
      },
      { passive: true }
    );
    sc.addEventListener(
      "touchmove",
      (ev) => {
        if (!pinch || pinch.gesture || ev.touches.length !== 2) return;
        const d0 = pinch.dist0;
        if (!(d0 > 8)) return;
        ev.preventDefault();
        const m = midOf(ev.touches[0], ev.touches[1]);
        pinch.midX = m.clientX;
        pinch.midY = m.clientY;
        const d1 = distOf(ev.touches[0], ev.touches[1]);
        applyPinchScale(d1 / d0);
      },
      { passive: false }
    );
    const endPinch = (ev) => {
      if (!ev.touches || ev.touches.length < 2) pinch = null;
    };
    sc.addEventListener("touchend", endPinch, { passive: true });
    sc.addEventListener("touchcancel", endPinch, { passive: true });
    // Older iOS Safari gesture events (best-effort; prevent page zoom on chart)
    sc.addEventListener(
      "gesturestart",
      (ev) => {
        try { ev.preventDefault(); } catch (_) {}
        const z0 = Math.sqrt(Math.max(zoomX, 1e-6) * Math.max(zoomY, 1e-6));
        pinch = { dist0: 1, zoom0: z0, midX: null, midY: null, gesture: true };
        lastApplied = z0;
      },
      { passive: false }
    );
    sc.addEventListener(
      "gesturechange",
      (ev) => {
        if (!pinch || !pinch.gesture) return;
        try { ev.preventDefault(); } catch (_) {}
        const s = ev.scale;
        if (s && isFinite(s)) applyPinchScale(s);
      },
      { passive: false }
    );
    sc.addEventListener(
      "gestureend",
      () => {
        pinch = null;
      },
      { passive: true }
    );
  })();

  // ---- P3: drag left price axis → continuous zoomY (TV-style) ----
  // Convention (TradingView price scale):
  //   drag UP on axis → expand price range on screen → smaller zoomY (壓扁 / denser bins)
  //   drag DOWN on axis → enlarge bins → larger zoomY (拉長)
  // Price under pointer stays anchored via setZoomXY clientY.
  (function bindPriceAxisZoomY() {
    const sc = $("profilesScroll");
    if (!sc) return;
    const SENS = 0.0075; // ln(zoom) per px; ~half-viewport drag ≈ 2× zoomY

    function endYAxisDrag(ev) {
      if (!yAxisDrag) return;
      if (ev && ev.pointerId != null && yAxisDrag.pointerId !== ev.pointerId) return;
      const el = sc.querySelector(".price-axis");
      if (el && yAxisDrag.pointerId != null) {
        try { el.releasePointerCapture(yAxisDrag.pointerId); } catch (_) {}
        el.classList.remove("y-axis-dragging");
      }
      yAxisDrag = null;
    }

    sc.addEventListener(
      "pointerdown",
      (ev) => {
        if (ev.pointerType === "mouse" && ev.button !== 0) return;
        if (ev.isPrimary === false) return;
        const ax =
          ev.target && ev.target.closest && ev.target.closest(".price-axis");
        if (!ax) return;
        // Own the gesture — do not start crosshair / day-merge drag
        ev.preventDefault();
        ev.stopPropagation();
        if (chDrag) endChDrag(null);
        hideCrosshair();
        hideSinglePinTip();
        yAxisDrag = {
          pointerId: ev.pointerId,
          startY: ev.clientY,
          zoomY0: zoomY,
          anchorX: ev.clientX,
          anchorY: ev.clientY,
        };
        ax.classList.add("y-axis-dragging");
        try { ax.setPointerCapture(ev.pointerId); } catch (_) {}
      },
      { passive: false, capture: true }
    );

    sc.addEventListener(
      "pointermove",
      (ev) => {
        if (!yAxisDrag || ev.pointerId !== yAxisDrag.pointerId) return;
        ev.preventDefault();
        // dy > 0 (finger down) → larger zoomY (拉長); dy < 0 → 壓扁
        const dy = ev.clientY - yAxisDrag.startY;
        const next = yAxisDrag.zoomY0 * Math.exp(dy * SENS);
        setZoomXY(null, next, {
          unlink: true,
          immediate: true,
          clientX: yAxisDrag.anchorX,
          clientY: yAxisDrag.anchorY,
        });
      },
      { passive: false, capture: true }
    );

    sc.addEventListener("pointerup", endYAxisDrag, { capture: true, passive: true });
    sc.addEventListener("pointercancel", endYAxisDrag, { capture: true, passive: true });

    sc.addEventListener(
      "touchmove",
      (ev) => {
        if (yAxisDrag && ev.cancelable) ev.preventDefault();
      },
      { passive: false }
    );
  })();

  $("fileInput").addEventListener("change", async (ev) => {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    try {
      const text = await f.text();
      $("csvText").value = text;
      const bars = parseCSV(text);
      const symbol = ($("symbol").value || "UPLOAD").trim();
      if (STATIC_MODE) {
        throw new Error("靜態版無 API：GitHub Pages 唔支援 Upload/ingest，請用本地 Flask。");
      }
      const res = await fetch("/api/ingest/tv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: symbol.startsWith("HK") || symbol.includes(":") ? symbol : "UPLOAD:" + symbol,
          bars: bars.map((b) => ({ t: b.t, h: b.high, l: b.low })),
          interval: "5m",
          timezone: $("tz").value,
        }),
      });
      const payload = await readResponseJson(res, "/api/ingest/tv");
      if (!res.ok || payload.error) throw new Error(payload.error || "ingest failed");
      $("source").value = "tv";
      $("symbol").value = symbol.startsWith("HK") || symbol.includes(":") ? symbol : "UPLOAD:" + symbol;
      showSrcNote("Ingested " + payload.barCount + " bars → building…");
      await fetchAndBuild({ refresh: true });
    } catch (e) {
      showErr(e.message || String(e));
    }
  });

  document.querySelectorAll(".chip[data-sym]").forEach((el) => {
    el.addEventListener("click", () => {
      $("symbol").value = el.getAttribute("data-sym");
      const src = el.getAttribute("data-src");
      if (src) $("source").value = src;
    });
  });

  $("profilesScroll").addEventListener(
    "scroll",
    () => {
      // Panning / scrolling the HG wall clears pinned date+pts so it cannot lag behind
      if (crosshair.visible && !(chDrag && chDrag.mode === "price")) {
        hideCrosshair();
      }
      scheduleRender();
      syncTimelineScroll();
      updateTrendChart();
    },
    { passive: true }
  );

  const scEl = $("profilesScroll");
  if (scEl) {
    const CH_SLOP = 8; // px before classifying vertical price-drag vs horizontal pan

    scEl.addEventListener(
      "pointerdown",
      (ev) => {
        if (ev.pointerType === "mouse" && ev.button !== 0) return;
        const t = ev.target;
        if (t && t.closest) {
          if (t.closest(".price-axis")) return; // P3 Y-axis zoom owns this hit
          if (t.closest(".day-head-select")) return; // day-merge head taps
          if (t.closest("button, a, input, select, label, .merge-panel")) return;
        }
        // Two-finger pinch owns the gesture
        if (ev.isPrimary === false) return;
        const hit = hitTestChart(ev.clientX, ev.clientY);
        if (!hit || !hit.sid) return;
        applyCrosshairHit(hit, null);
        chDrag = {
          pointerId: ev.pointerId,
          startX: ev.clientX,
          startY: ev.clientY,
          mode: null,
          lockColIdx: hit.colIdx,
          lockSid: hit.sid,
          lockLocalX: hit.localX,
        };
      },
      { passive: true }
    );

    scEl.addEventListener(
      "pointermove",
      (ev) => {
        if (chDrag && ev.pointerId === chDrag.pointerId) {
          const dx = ev.clientX - chDrag.startX;
          const dy = ev.clientY - chDrag.startY;
          if (!chDrag.mode) {
            if (Math.abs(dx) < CH_SLOP && Math.abs(dy) < CH_SLOP) return;
            const touchLike = ev.pointerType === "touch" || ev.pointerType === "pen";
            if (touchLike && Math.abs(dx) > Math.abs(dy)) {
              // Horizontal pan: drop selection, let native scroll continue
              chDrag.mode = "pan";
              hideCrosshair();
              endChDrag(ev);
              return;
            }
            // Vertical (or mouse) drag → track price level
            chDrag.mode = touchLike ? "price" : "track";
            try { scEl.setPointerCapture(ev.pointerId); } catch (_) {}
          }
          if (chDrag.mode === "pan") return;
          const hit = hitTestChart(ev.clientX, ev.clientY);
          if (!hit) return;
          if (chDrag.mode === "price") {
            // Lock column/date; only the horizontal pts line follows finger
            applyCrosshairHit(hit, {
              sid: chDrag.lockSid,
              colIdx: chDrag.lockColIdx,
              localX: chDrag.lockLocalX,
            });
          } else {
            applyCrosshairHit(hit, null);
          }
          return;
        }
        // Desktop hover follow when not pressing
        if (ev.pointerType === "mouse" && ev.buttons === 0) {
          onChartPointer(ev);
        }
      },
      { passive: true }
    );

    // Block native scroll while vertically dragging the price level (touch)
    scEl.addEventListener(
      "touchmove",
      (ev) => {
        if (chDrag && chDrag.mode === "price" && ev.cancelable) {
          ev.preventDefault();
        }
      },
      { passive: false }
    );

    scEl.addEventListener("pointerup", endChDrag, { passive: true });
    scEl.addEventListener("pointercancel", endChDrag, { passive: true });

    scEl.addEventListener(
      "pointerleave",
      (ev) => {
        if (ev.pointerType === "mouse" && !chDrag) {
          hideCrosshair();
          hideSinglePinTip();
        }
      },
      { passive: true }
    );

    scEl.addEventListener(
      "mouseover",
      (ev) => {
        const bar = ev.target && ev.target.closest && ev.target.closest(".single-pin-bar");
        if (bar) showSinglePinTip(bar, ev.clientX, ev.clientY);
      },
      { passive: true }
    );
    scEl.addEventListener(
      "mousemove",
      (ev) => {
        const bar = ev.target && ev.target.closest && ev.target.closest(".single-pin-bar");
        if (bar) showSinglePinTip(bar, ev.clientX, ev.clientY);
        else hideSinglePinTip();
      },
      { passive: true }
    );
  }

  // Redraw trend on pane resize
  if (typeof ResizeObserver !== "undefined") {
    const tp = $("trendPane");
    if (tp) {
      const ro = new ResizeObserver(() => updateTrendChart());
      ro.observe(tp);
    }
  }
  window.addEventListener("resize", () => updateTrendChart(), { passive: true });



  // —— Chrome info stack collapse (stats + legend; default collapsed) ——
  const CHROME_INFO_COLLAPSE_KEY = "tpo-chrome-info-collapsed";
  const chromeInfoWrap = document.getElementById("chromeInfoWrap");
  const btnToggleChromeInfo = $("btnToggleChromeInfo");

  function setChromeInfoCollapsed(collapsed, { persist = true } = {}) {
    if (!chromeInfoWrap) return;
    collapsed = !!collapsed;
    chromeInfoWrap.classList.toggle("info-collapsed", collapsed);
    if (btnToggleChromeInfo) {
      btnToggleChromeInfo.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btnToggleChromeInfo.textContent = collapsed ? "資訊" : "收起資訊";
      btnToggleChromeInfo.title = collapsed ? "展開資訊（統計／圖例）" : "收起資訊";
    }
    if (persist) {
      try {
        localStorage.setItem(CHROME_INFO_COLLAPSE_KEY, collapsed ? "1" : "0");
      } catch (_) {}
    }
    requestAnimationFrame(() => {
      try { scheduleRender(); } catch (_) {}
      try { updateTrendChart(); } catch (_) {}
      try { window.dispatchEvent(new Event("resize")); } catch (_) {}
    });
  }

  function readStoredChromeInfoCollapsed() {
    try {
      const v = localStorage.getItem(CHROME_INFO_COLLAPSE_KEY);
      if (v === "0") return false;
      if (v === "1") return true;
    } catch (_) {}
    return true; // 預設收起
  }

  if (btnToggleChromeInfo) {
    btnToggleChromeInfo.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const collapsed = !!(chromeInfoWrap && chromeInfoWrap.classList.contains("info-collapsed"));
      setChromeInfoCollapsed(!collapsed);
    }, true);
  }

  // —— Settings pane collapse (default collapsed; FAB always reachable) ——
  const SETTINGS_COLLAPSE_KEY = "tpo-settings-collapsed-v2";
  const layoutEl = document.getElementById("appLayout") || document.querySelector(".layout");
  const btnCollapseSettings = $("btnCollapseSettings");
  const btnExpandSettings = $("btnExpandSettings");

  function isNarrowViewport() {
    return window.matchMedia("(max-width: 900px)").matches;
  }

  function setSettingsCollapsed(collapsed, { persist = true } = {}) {
    if (!layoutEl) return;
    collapsed = !!collapsed;
    layoutEl.classList.toggle("settings-collapsed", collapsed);
    document.body.classList.toggle("tpo-settings-collapsed", collapsed);
    document.body.classList.toggle("tpo-settings-open", !collapsed);
    if (btnCollapseSettings) {
      btnCollapseSettings.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btnCollapseSettings.textContent = "收起";
      btnCollapseSettings.title = "收起設定";
      btnCollapseSettings.hidden = collapsed;
      btnCollapseSettings.disabled = collapsed;
    }
    if (btnExpandSettings) {
      // Always top-right: 「設定」when collapsed; 「收起」when open
      btnExpandSettings.classList.add("show");
      btnExpandSettings.classList.toggle("is-collapse", !collapsed);
      btnExpandSettings.textContent = collapsed ? "設定" : "收起";
      btnExpandSettings.setAttribute("aria-label", collapsed ? "展開設定" : "收起設定");
      btnExpandSettings.title = collapsed ? "展開設定" : "收起設定";
      btnExpandSettings.setAttribute("aria-hidden", "false");
      btnExpandSettings.hidden = false;
      btnExpandSettings.disabled = false;
    }
    if (persist) {
      try {
        localStorage.setItem(SETTINGS_COLLAPSE_KEY, collapsed ? "1" : "0");
      } catch (_) {}
    }
    requestAnimationFrame(() => {
      try { scheduleRender(); } catch (_) {}
      try { updateTrendChart(); } catch (_) {}
      try { window.dispatchEvent(new Event("resize")); } catch (_) {}
    });
  }

  function readStoredCollapsed() {
    try {
      const v = localStorage.getItem(SETTINGS_COLLAPSE_KEY);
      if (v === "0") return false; // user explicitly expanded last time
      if (v === "1") return true;
    } catch (_) {}
    return true; // 預設收起
  }

  function onCollapseClick(e) {
    e.preventDefault();
    e.stopPropagation();
    setSettingsCollapsed(true);
  }
  function onFabClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const collapsed = !!(layoutEl && layoutEl.classList.contains("settings-collapsed"));
    setSettingsCollapsed(!collapsed);
  }
  if (btnCollapseSettings) {
    btnCollapseSettings.addEventListener("click", onCollapseClick, true);
  }
  if (btnExpandSettings) {
    btnExpandSettings.addEventListener("click", onFabClick, true);
  }


  // ---- Legend info tips (POC / VA / Dual / Spike / IB / Single pin) ----
  const LEGEND_TIPS = {
    poc: {
      name: "POC",
      body: "全日 TPO 最多嘅價位；同分揀離 mid 最近。",
    },
    va: {
      name: "VA / VAH / VAL",
      body: "由 POC 向外擴，累積約 70% TPO → 價值區上下沿。",
    },
    ib: {
      name: "IB",
      body: "首兩格 TPO（Initial Balance）高低。",
    },
    dual: {
      name: "Dual",
      body: "VA 外次高峰：count≥max(2,最高×0.7)，同 POC 之間有谷（中間最少＜該行×0.6 且＜最高×0.5）。淺藍。",
    },
    spike: {
      name: "Spike",
      body: "VA 外薄尾，該行 count≤2。紅。",
    },
    singlepin: {
      name: "Single pin",
      body: "孤立單格（連續單格唔算）淡紫向右伸到撞 TPO；hover 條帶另有點數+遺留日。",
    },
  };

  const legendTipEl = $("legendTip");
  let legendTipOpenItem = null;
  let legendLongPressTimer = null;
  let legendTipPinned = false;

  function hideLegendTip() {
    if (legendLongPressTimer) {
      clearTimeout(legendLongPressTimer);
      legendLongPressTimer = null;
    }
    if (legendTipEl) {
      legendTipEl.classList.remove("show");
      legendTipEl.setAttribute("aria-hidden", "true");
    }
    if (legendTipOpenItem) {
      legendTipOpenItem.classList.remove("is-tip-open");
      legendTipOpenItem = null;
    }
    legendTipPinned = false;
  }

  function positionLegendTip(anchor) {
    if (!legendTipEl || !anchor) return;
    const pad = 8;
    const rect = anchor.getBoundingClientRect();
    const tip = legendTipEl;
    tip.style.left = "0px";
    tip.style.top = "0px";
    tip.classList.add("show");
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = rect.left;
    let top = rect.bottom + 6;
    if (left + tw > window.innerWidth - pad) left = window.innerWidth - tw - pad;
    if (left < pad) left = pad;
    if (top + th > window.innerHeight - pad) top = rect.top - th - 6;
    if (top < pad) top = pad;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  }

  function showLegendTip(item, { pin = false } = {}) {
    if (!legendTipEl || !item) return;
    const key = item.getAttribute("data-tip-key");
    const info = LEGEND_TIPS[key];
    if (!info) return;
    if (legendTipOpenItem && legendTipOpenItem !== item) {
      legendTipOpenItem.classList.remove("is-tip-open");
    }
    legendTipOpenItem = item;
    item.classList.add("is-tip-open");
    const nameEl = legendTipEl.querySelector(".lt-name");
    const bodyEl = legendTipEl.querySelector(".lt-body");
    if (nameEl) nameEl.textContent = info.name;
    if (bodyEl) bodyEl.textContent = info.body;
    legendTipEl.setAttribute("aria-hidden", "false");
    positionLegendTip(item);
    legendTipPinned = !!pin;
  }

  function bindLegendTips() {
    const items = document.querySelectorAll(".legend-item[data-tip-key]");
    items.forEach((item) => {
      const key = item.getAttribute("data-tip-key");
      const info = LEGEND_TIPS[key];
      if (info) {
        item.setAttribute("aria-label", `${info.name}：${info.body}`);
      }
      item.addEventListener("mouseenter", () => {
        if (!("ontouchstart" in window)) showLegendTip(item);
      });
      item.addEventListener("mouseleave", () => {
        if (!legendTipPinned) hideLegendTip();
      });
      item.addEventListener("focus", () => showLegendTip(item, { pin: true }));
      item.addEventListener("blur", () => hideLegendTip());
      item.addEventListener(
        "touchstart",
        (e) => {
          if (legendLongPressTimer) clearTimeout(legendLongPressTimer);
          legendLongPressTimer = setTimeout(() => {
            legendLongPressTimer = null;
            showLegendTip(item, { pin: true });
          }, 420);
        },
        { passive: true }
      );
      item.addEventListener(
        "touchend",
        () => {
          if (legendLongPressTimer) {
            clearTimeout(legendLongPressTimer);
            legendLongPressTimer = null;
            // short tap: toggle tip
            if (legendTipOpenItem === item && legendTipPinned) hideLegendTip();
            else showLegendTip(item, { pin: true });
          }
        },
        { passive: true }
      );
      item.addEventListener(
        "touchmove",
        () => {
          if (legendLongPressTimer) {
            clearTimeout(legendLongPressTimer);
            legendLongPressTimer = null;
          }
        },
        { passive: true }
      );
      item.addEventListener("click", (e) => {
        // desktop click / keyboard: toggle pin
        if ("ontouchstart" in window) return;
        e.preventDefault();
        if (legendTipOpenItem === item && legendTipPinned) hideLegendTip();
        else showLegendTip(item, { pin: true });
      });
      item.addEventListener("keydown", (e) => {
        if (e.key === "Escape") hideLegendTip();
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (legendTipOpenItem === item && legendTipPinned) hideLegendTip();
          else showLegendTip(item, { pin: true });
        }
      });
    });
    document.addEventListener(
      "pointerdown",
      (e) => {
        if (!legendTipOpenItem) return;
        const t = e.target;
        if (t && (t.closest && (t.closest(".legend-item") || t.closest("#legendTip")))) return;
        hideLegendTip();
      },
      true
    );
    window.addEventListener("scroll", () => {
      if (legendTipOpenItem) positionLegendTip(legendTipOpenItem);
    }, true);
    window.addEventListener("resize", () => {
      if (legendTipOpenItem) positionLegendTip(legendTipOpenItem);
    });
  }


  // ---- Composite HG toggles + date-head range select ----
  const btnWeekHg = $("btnWeekHg");
  const btnMonthHg = $("btnMonthHg");
  const btnClearMerge = $("btnClearMerge");
  if (btnWeekHg) {
    btnWeekHg.addEventListener("click", () => setWeekHg(!weekHgOn));
  }
  if (btnMonthHg) {
    btnMonthHg.addEventListener("click", () => setMonthHg(!monthHgOn));
  }
  if (btnClearMerge) {
    btnClearMerge.addEventListener("click", () => clearAllComposites());
  }
  const btnMergeInfo = $("btnMergeInfo");
  if (btnMergeInfo) {
    btnMergeInfo.addEventListener("click", () => toggleMergePanel());
  }
  const btnCollapseMergePanel = $("btnCollapseMergePanel");
  if (btnCollapseMergePanel) {
    btnCollapseMergePanel.addEventListener("click", () => setMergePanelOpen(false));
  }
  // Default collapsed (settings-style)
  setMergePanelOpen(false);

  const btnAddMergeGroup = $("btnAddMergeGroup");
  if (btnAddMergeGroup) {
    btnAddMergeGroup.addEventListener("click", () => startAddMergeGroup());
  }
  const btnClearDayMerges = $("btnClearDayMerges");
  if (btnClearDayMerges) {
    btnClearDayMerges.addEventListener("click", () => clearDayMergesOnly());
  }
  const mergeGroupList = $("mergeGroupList");
  if (mergeGroupList) {
    mergeGroupList.addEventListener("click", (ev) => {
      const btn = ev.target && ev.target.closest && ev.target.closest("[data-clear-gid]");
      if (!btn) return;
      ev.preventDefault();
      clearOneRangeGroup(btn.getAttribute("data-clear-gid"));
    });
  }
  if ($("alphabet")) {
    $("alphabet").addEventListener("change", () => {
      compositeCache.clear();
      if (compositeActive()) scheduleRender();
    });
  }
  if ($("firstLetter")) {
    $("firstLetter").addEventListener("change", () => {
      compositeCache.clear();
      if (compositeActive()) scheduleRender();
    });
  }
  if ($("vaPct")) {
    $("vaPct").addEventListener("change", () => {
      compositeCache.clear();
      if (compositeActive()) scheduleRender();
    });
  }

  const scForSelect = $("profilesScroll");
  if (scForSelect) {
    scForSelect.addEventListener("click", (ev) => {
      const head = ev.target && ev.target.closest && ev.target.closest(".day-head-select");
      if (!head) return;
      ev.preventDefault();
      ev.stopPropagation();
      let idx = parseInt(head.getAttribute("data-idx"), 10);
      if (!isFinite(idx)) {
        const col = head.closest(".day-col");
        idx = col ? parseInt(col.getAttribute("data-idx"), 10) : NaN;
      }
      if (!isFinite(idx)) {
        const sid = (head.closest(".day-col") || {}).getAttribute
          ? head.closest(".day-col").getAttribute("data-sid")
          : null;
        idx = sid ? sessionIds.indexOf(sid) : -1;
      }
      if (idx >= 0) onDayHeadSelect(idx);
    });
  }
  syncCompositeButtons();

  bindLegendTips();


  // Apply collapse ASAP (HTML already has settings-collapsed; re-assert after read)
  setSettingsCollapsed(readStoredCollapsed(), { persist: false });
  setChromeInfoCollapsed(readStoredChromeInfoCollapsed(), { persist: false });

  // If user rotates / resizes across breakpoint with no stored pref, keep current;
  // stored pref always wins (already applied).

  // Defaults
  $("alignPrices").checked = true;
  updateZoomLabel();

  if (location.protocol !== "file:") {
    fetchAndBuild();
  }
})();
