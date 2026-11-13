/* nearme map UI. Vanilla JS + Leaflet.

Two modes, decided at boot by whether /api/meta answers:
  server  - `python server.py`: the local API fetches QuickMap and filters.
  static  - GitHub Pages: no server exists, so one `nearme.json` snapshot
            (rebuilt by .github/workflows/publish.yml) is filtered here instead.
The static path mirrors server.py/src/nearme/api.py: bbox with a 0.04 deg pad on
the point OR the pavement trace, severity set, and haversine/point-to-polyline
radius around the anchor. Keep the two in step or the published map will quietly
agree with the local one only by accident. */
"use strict";

const LEVEL_ORDER = ["full", "one_way", "lanes", "shoulder", "blocked", "none"];
const LEVEL_LABEL = {
  full: "full closure",
  one_way: "one-way traffic",
  lanes: "lane closure",
  shoulder: "shoulder",
  blocked: "blockage",
  none: "no closure",
};
const MARK_R = { full: 11, one_way: 9, lanes: 8, shoulder: 6, blocked: 6, none: 5 };

const state = {
  me: null, // {lat, lon, label}
  levels: new Set(LEVEL_ORDER),
  features: [],
  byUid: new Map(),
  seen: new Map(), // uid -> first-seen epoch ms
  layers: { markers: null, traces: null, me: null },
  meta: null,
  pollTimer: null,
  mode: "server", // "server" | "static" (see the header comment)
  snapshot: null, // static mode: {type:"FeatureCollection", nearme:{...}}
};

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
const fmtAgo = (s) =>
  s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;

/* ---------------------------------------------------------------- map */
const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([38.6, -120.6], 6);

const bases = {
  // CARTO's basemaps used to be key-free and now serve an "API KEY REQUIRED"
  // watermark tile instead (200 OK, so it fails silently) - don't come back
  // here without a key.
  "Streets (OSM)": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }),
  "Satellite (Esri)": L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 18, attribution: "Tiles &copy; Esri" }
  ),
};
bases["Streets (OSM)"].addTo(map);
L.control.layers(bases, null, { position: "bottomright" }).addTo(map);

state.layers.markers = L.layerGroup().addTo(map);
state.layers.traces = L.layerGroup().addTo(map);
state.layers.me = L.layerGroup().addTo(map);

/* --------------------------------------------------------------- chips */
function buildChips(colors) {
  const box = $("chips");
  box.innerHTML = "";
  LEVEL_ORDER.forEach((lvl) => {
    const el = document.createElement("span");
    el.className = "chip";
    el.style.setProperty("--c", colors[lvl]);
    el.innerHTML = `<i class="dot"></i>${LEVEL_LABEL[lvl]}`;
    el.onclick = () => {
      if (state.levels.has(lvl)) state.levels.delete(lvl);
      else state.levels.add(lvl);
      el.dataset.off = state.levels.has(lvl) ? "0" : "1";
      render();
    };
    box.appendChild(el);
  });
}

/* --------------------------------------------------------------- fetch */
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function loadMeta() {
  state.meta = await getJSON("/api/meta");
  $("attrib").textContent = state.meta.attribution;
  if (!$("chips").childElementCount) buildChips(state.meta.levels);
  setFeed(state.meta.feeds, state.meta.error);
}

/* ------------------------------------------------- static (Pages) mode */
/* The filtering itself lives in static.js (pure, Node-testable) because it has
   to stay identical to what api.py does server-side. */
const STATIC_FILE = "nearme.json";

async function loadSnapshot() {
  // Cache-bust on the ~5 min grid so a poll actually sees a new build.
  const fc = await getJSON(`${STATIC_FILE}?v=${Math.floor(Date.now() / 300000)}`);
  NearmeStatic.prepareAll(fc.features || []);
  state.snapshot = fc;
  return fc;
}

function staticEvents() {
  if (!state.snapshot) return [];
  const b = map.getBounds();
  return NearmeStatic.filterFeatures(state.snapshot.features, {
    bbox: { west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() },
    levels: state.levels,
    center: state.me ? [state.me.lat, state.me.lon] : null,
    radius: state.me && Number($("radius").value) > 0 ? Number($("radius").value) : null,
  });
}

function setSnapshotAge(generatedAt) {
  const age = Date.now() / 1000 - generatedAt;
  $("feed").textContent = `snapshot ${fmtAgo(age)} · a scheduled job rebuilds it every ~30 min`;
}


async function geocode(q) {
  if (state.mode === "static") {
    // No server to proxy this through, so ask OSM directly. One request per
    // explicit search - never in the poll loop.
    const r = await fetch(
      "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=" +
        encodeURIComponent(q),
      { headers: { Accept: "application/json" } }
    );
    const rows = await r.json().catch(() => []);
    if (!r.ok || !rows.length) throw new Error("no match for " + q);
    return { lat: +rows[0].lat, lon: +rows[0].lon, label: rows[0].display_name };
  }
  return getJSON("/api/geocode?q=" + encodeURIComponent(q));
}

function setFeed(feeds, error) {
  const ages = Object.values(feeds || {})
    .filter((f) => f.fetched)
    .map((f) => f.age_seconds);
  const stale = Object.values(feeds || {}).some((f) => f.stale);
  const el = $("feed");
  if (error) el.textContent = `feed error: ${error}`;
  else if (!ages.length) el.textContent = "feed not loaded";
  else el.textContent = `${stale ? "⚠ " : ""}feed ${fmtAgo(Math.max(...ages))}`;
}

async function loadEvents() {
  const b = map.getBounds();
  const p = new URLSearchParams({
    bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(","),
    levels: [...state.levels].join(","),
  });
  const radius = Number($("radius").value);
  if (state.me) {
    p.set("lat", state.me.lat);
    p.set("lon", state.me.lon);
    if (radius > 0) p.set("within", radius);
  }
  const fc =
    state.mode === "static"
      ? { features: staticEvents(), nearme: state.snapshot.nearme }
      : await getJSON("/api/events?" + p.toString());
  state.features = fc.features;
  const now = Date.now();
  state.features.forEach((f) => {
    const uid = f.properties.uid;
    if (!state.seen.has(uid)) state.seen.set(uid, now);
  });
  const counts = $("count");
  counts.innerHTML = `<strong>${fc.features.length}</strong> shown / ${fc.nearme.total} live`;
  if (state.mode === "static") setSnapshotAge(fc.nearme.generated_at);
  else setFeedFromStatuses(fc.nearme.statuses);
  render();
}

function setFeedFromStatuses(statuses) {
  if (!statuses || !statuses.length) return;
  const stale = statuses.some((s) => s.includes("STALE"));
  $("feed").textContent = `${stale ? "⚠ " : ""}${statuses.length} feed(s) ok`;
}

/* -------------------------------------------------------------- render */
function isFresh(uid) {
  const t = state.seen.get(uid);
  return t && Date.now() - t < 90000;
}

function popupHTML(f) {
  const p = f.properties;
  const bits = [];
  bits.push(`<h3>${esc(p.kind)}</h3>`);
  bits.push(
    `<div class="meta">${p.source === "chp" ? "CHP incident" : "Caltrans closure"}` +
      (p.distance_mi != null ? ` · ${p.distance_mi} mi` : "") +
      (p.direction ? ` · ${esc(p.direction)}` : "") +
      ` · <span class="lvl" style="--c:${p.color}">${LEVEL_LABEL[p.level]}</span></div>`
  );
  bits.push(
    `<div class="trust ${p.authoritative ? "auth" : "inf"}">${
      p.authoritative
        ? "Stated by Caltrans Lane Closure System"
        : "Inferred from CHP CAD text — quotes below are the evidence"
    }</div>`
  );
  if (p.location) bits.push(`<div class="sec"><span class="lbl">where</span><br>${esc(p.location)}</div>`);
  if (p.trace_mi) bits.push(`<div class="sec"><span class="lbl">roadway affected</span> ~${p.trace_mi} mi`);
  if (p.lanes_closed != null)
    bits.push(`<div class="sec"><span class="lbl">lanes</span> ${p.lanes_closed} of ${p.lanes_total} closed`);
  if (p.reason) bits.push(`<div class="sec"><span class="lbl">reason</span> ${esc(p.reason)}`);
  if (p.expected_end)
    bits.push(
      `<div class="sec"><span class="lbl">until</span> ${esc(p.expected_end.replace("T", " "))}` +
        ` <span class="badge">planned</span></div>`
    );
  if (p.routes && p.routes.length)
    bits.push(`<div class="sec"><span class="lbl">routes</span> ${esc(p.routes.join(", "))}</div>`);
  if (p.reported_by)
    bits.push(`<div class="sec"><span class="lbl">who reported</span> ${esc(p.reported_by)}</div>`);
  if (p.evidence && p.evidence.length)
    bits.push(
      `<div class="sec"><span class="lbl">CAD quote</span>${p.evidence
        .map((e) => `<blockquote>${esc(e)}</blockquote>`)
        .join("")}</div>`
    );
  if (p.notes && p.notes.length)
    bits.push(
      `<div class="sec"><span class="lbl">CAD log</span>${p.notes
        .map(
          (n) =>
            `<div class="log">${esc((n.when || "").replace("T", " "))} ${
              n.unit ? "[" + esc(n.unit) + "]" : ""
            } ${esc(n.text)}</div>`
        )
        .join("")}</div>`
    );
  if (p.ids && p.ids.length)
    bits.push(`<div class="sec"><span class="lbl">id</span> ${esc(p.ids.join(", "))}</div>`);
  return `<div class="iw">${bits.join("")}</div>`;
}

function filtered() {
  const keep = new Set();
  if ($("srcChp").checked) keep.add("chp");
  if ($("srcCa").checked) keep.add("caltrans");
  return state.features.filter((f) => keep.has(f.properties.source));
}

function render() {
  state.layers.markers.clearLayers();
  state.layers.traces.clearLayers();
  state.byUid.clear();

  for (const f of filtered()) {
    const p = f.properties;
    const showMe = p.level !== "none";
    if (f.geometry.type === "Point") {
      const [lon, lat] = f.geometry.coordinates;
      const m = L.circleMarker([lat, lon], {
        radius: MARK_R[p.level] || 6,
        color: p.color,
        weight: p.authoritative ? 1.5 : 1,
        fillColor: p.color,
        fillOpacity: showMe ? 0.85 : 0.35,
        dashArray: p.source === "chp" ? null : "2,3",
        opacity: 0.9,
      });
      m.bindPopup(popupHTML(f), { maxWidth: 400 });
      m.on("click", () => highlightRow(p.uid));
      state.layers.markers.addLayer(m);
      state.byUid.set(p.uid, m);
    }
    if (p.trace) {
      const line = L.polyline(
        p.trace.map(([lon, lat]) => [lat, lon]),
        { color: p.color, weight: p.level === "full" ? 5 : 3, opacity: 0.75, dashArray: "6,6" }
      );
      line.bindPopup(popupHTML(f), { maxWidth: 400 });
      state.layers.traces.addLayer(line);
      if (f.geometry.type === "LineString") state.byUid.set(p.uid, line);
    }
  }
  renderList();
}

function renderList() {
  const ul = $("list");
  ul.innerHTML = "";
  const pts = filtered().filter((f) => f.properties.distance_mi != null);
  const rows = (pts.length ? pts : filtered()).slice().sort((a, b) => {
    const pa = a.properties, pb = b.properties;
    if (pa.distance_mi != null && pb.distance_mi != null) return pa.distance_mi - pb.distance_mi;
    const ra = LEVEL_ORDER.indexOf(pa.level), rb = LEVEL_ORDER.indexOf(pb.level);
    if (ra !== rb) return ra - rb;
    return String(pb.updated || "").localeCompare(String(pa.updated || ""));
  });
  $("panelNote").textContent = state.me
    ? `from ${state.me.label}`
    : "enable “Near me”, or search a place";
  rows.slice(0, 120).forEach((f) => {
    const p = f.properties;
    const li = document.createElement("li");
    if (isFresh(p.uid)) li.className = "fresh";
    li.dataset.uid = p.uid;
    const dist = p.distance_mi != null ? `${p.distance_mi} mi` : "";
    li.innerHTML =
      `<div class="row1"><span class="dist">${dist}</span>` +
      `<span class="lvl" style="--c:${p.color}">${LEVEL_LABEL[p.level]}</span>` +
      `<span class="kind">${esc(p.kind)}</span>` +
      (p.authoritative ? ` <span class="badge">LCS</span>` : "") +
      `</div><div class="row2">${esc(p.location || "")}${
        p.expected_end ? " · until " + esc(p.expected_end.split("T")[0]) : ""
      }</div>`;
    li.onclick = () => {
      const m = state.byUid.get(p.uid);
      if (!m) return;
      map.flyTo(m.getLatLng ? m.getLatLng() : m.getBounds().getCenter(), Math.max(map.getZoom(), 11));
      m.openPopup();
    };
    ul.appendChild(li);
  });
  if (!rows.length)
    ul.innerHTML = `<li style="cursor:default;color:#7d8794">Nothing reported here for these filters. Zoom out or re-enable severities.</li>`;
}

function highlightRow(uid) {
  document.querySelectorAll("#list li").forEach((li) => {
    if (li.dataset.uid === uid) {
      li.style.background = "#2a313b";
      li.scrollIntoView({ block: "nearest" });
      setTimeout(() => (li.style.background = ""), 1200);
    }
  });
}

/* ------------------------------------------------------------- places */
function drawMe() {
  state.layers.me.clearLayers();
  if (!state.me) return;
  const radius = Number($("radius").value);
  L.circle([state.me.lat, state.me.lon], {
    radius: radius > 0 ? radius * 1609.34 : 0,
    color: "#4da3ff",
    weight: 1,
    fillColor: "#4da3ff",
    fillOpacity: 0.06,
  }).addTo(state.layers.me);
  L.circleMarker([state.me.lat, state.me.lon], {
    radius: 6,
    color: "#ffffff",
    weight: 2,
    fillColor: "#4da3ff",
    fillOpacity: 1,
  })
    .bindTooltip(state.me.label)
    .addTo(state.layers.me);
}

function setMe(lat, lon, label) {
  state.me = { lat, lon, label };
  drawMe();
  loadEvents();
}

function nearMe() {
  if (!navigator.geolocation) return flash("This browser has no geolocation.");
  flash("locating…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude: lat, longitude: lon } = pos.coords;
      setMe(lat, lon, "you");
      map.flyTo([lat, lon], 9);
    },
    (err) => flash("geolocation refused: " + err.message),
    { enableHighAccuracy: true, timeout: 8000 }
  );
}

function flash(msg) {
  $("count").textContent = msg;
}

/* --------------------------------------------------------------- wiring */
$("searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  flash("searching…");
  try {
    const g = await geocode(q);
    setMe(g.lat, g.lon, g.label.split(",")[0]);
    map.flyTo([g.lat, g.lon], 10);
  } catch (err) {
    flash("search failed");
    $("feed").textContent = err.message;
  }
});

$("meBtn").onclick = nearMe;
$("radius").onchange = () => {
  drawMe();
  loadEvents();
};
$("srcChp").onchange = $("srcCa").onchange = render;
$("refreshBtn").onclick = async () => {
  flash("refreshing…");
  if (state.mode === "static") {
    await loadSnapshot();
    setSnapshotAge(state.snapshot.nearme.generated_at);
  } else {
    await getJSON("/api/refresh");
    await loadMeta();
  }
  await loadEvents();
};

let moveTimer = null;
map.on("moveend", () => {
  clearTimeout(moveTimer);
  moveTimer = setTimeout(loadEvents, 450);
});

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "n") nearMe();
  if (e.key === "/") {
    e.preventDefault();
    $("q").focus();
  }
});

async function poll() {
  try {
    if (state.mode === "static") {
      await loadSnapshot();
    } else {
      await loadMeta();
    }
    await loadEvents();
  } catch (err) {
    $("feed").textContent = "poll failed: " + err.message;
  }
  state.pollTimer = setTimeout(poll, (state.meta?.refresh_seconds || 90) * 1000);
}

/* Boot: the /api probe decides which mode we are in. On Pages the request
   404s (HTML body), which getJSON turns into an exception. */
(async function boot() {
  try {
    state.meta = await getJSON("/api/meta");
    state.mode = "server";
    $("attrib").textContent = state.meta.attribution;
    if (!$("chips").childElementCount) buildChips(state.meta.levels);
    setFeed(state.meta.feeds, state.meta.error);
  } catch (err) {
    try {
      await loadSnapshot();
    } catch (err2) {
      flash("cannot reach the local API or nearme.json");
      $("feed").textContent = err2.message;
      return;
    }
    state.mode = "static";
    state.meta = state.snapshot.nearme;
    $("attrib").textContent = state.meta.attribution;
    if (!$("chips").childElementCount) buildChips(state.meta.levels);
    setSnapshotAge(state.meta.generated_at);
  }
  poll();
})();
