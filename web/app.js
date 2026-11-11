/* Groundskeeper UI (formerly Yarbo panel). Never commands the robot: it GETs from this server and PUTs only the panel's own settings.
   All DOM is built with createElement/textContent. */
"use strict";

// Signed out (the session ended, or the password changed elsewhere): any API call answering 401 goes to sign-in.
function toSignIn() { location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.hash)}`; }
(function watchSignIn() {
  const raw = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const r = await raw(...args);
    if (r.status === 401 && r.headers.get("X-GK-Auth") === "required") toSignIn();   // the gate's 401, not another refusal
    return r;
  };
})();

const $ = (sel, el = document) => el.querySelector(sel);
// The map's element, held by reference: Live's camera layout moves it into a card that is not always on the page.
const MAP_EL = document.getElementById("map");
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const SVG_NS = "http://www.w3.org/2000/svg";
// Every colour comes from the tokens in style.css (one palette; docs/dev/ui-design.md). Status colours follow
// the theme; map colours are the same in both (they sit on the aerial photo).
const tok = (name, fallback) => { try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch { return fallback; } };
const GREEN = tok("--ok", "#8BD49A"), AMBER = tok("--warn", "#F0B955"), CORAL = tok("--bad", "#F08A6E"), SKY = tok("--info", "#79B8F0"),
      MUTED = tok("--muted", "#A3B1AA");
const MAP = {
  area: tok("--area", "#E8EEEA"), nogo: tok("--nogo", "#E5484D"), novis: tok("--novis", "#79B8F0"), fence: tok("--fence", "#E8D44D"),
  fixed: tok("--trk-fixed", "#8BD49A"), float: tok("--trk-float", "#F0B955"), single: tok("--trk-single", "#F2784B"),
  gap: tok("--gap", "#FF5FA2"), offplan: tok("--offplan", "#FF5FA2"), edge: tok("--edge", "#B79CF0"),
  robot: tok("--robot", "#79B8F0"), held: tok("--robot-held", "#F0B955"), error: tok("--robot-error", "#F08A6E"),
  onmap: tok("--onmap", "#F2F5F3"), road: tok("--road", "#69736E"), camera: tok("--camera", "#E8EEEA"),
  body3d: tok("--robot-body", "#2A2D30"), frame3d: tok("--robot-frame", "#4A5055"), track3d: tok("--robot-track", "#17191A"),
  trim3d: tok("--robot-trim", "#F2C01E"), stop3d: tok("--robot-stop", "#D8322B"), light3d: tok("--robot-light", "#F4F7FA"),
  sidewalk: tok("--sidewalk", "#C9D4CE"), route: tok("--route", "#C3C9CC"), ink: tok("--map-ink", "#0E1311"),
};
// Neutral data (neither good nor bad) is drawn in these, never in status colours.
const SERIES = [tok("--series-1", "#79B8F0"), tok("--series-2", "#9AA7B4"), tok("--series-3", "#B79CF0"), tok("--series-4", "#4FD1C5")];
const STATE_COLOR = { Dry: GREEN, Damp: AMBER, Wet: SKY };

/* ------------------------------------------------------------- DOM helpers */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}
function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}
function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === "") continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}
/** replaceChildren that skips null/false, which the DOM would otherwise print as text. Keyboard focus inside `el`
 *  survives (see keepFocus), so every redraw through fill() keeps it. */
function fill(el, ...children) {
  const kids = children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
  keepFocus(el, () => el.replaceChildren(...kids));
}

/** Runs a redraw of `root` and gives keyboard focus back afterwards: a redraw replaces the focused element, which would
 *  drop focus to the page. The element is found again by its data-key (as settings.js does), else by `to` (the data-key
 *  to land on when the focused control no longer exists, e.g. Open -> Close). A control with no key is found again only
 *  if it was the only one of its kind, text and link before the redraw and still is; otherwise focus is not moved (a wrong guess
 *  could press the wrong button next). A disabled target cannot take focus, so it counts as gone, and neither can one
 *  marked data-nofocus (an armed emergency button). Focus outside root is
 *  never touched, and a redraw inside another keepFocus (a fill within a redraw) leaves the restoring to the outer one. */
const FOCUSABLE = "a[href], button, summary, input, select, textarea, [tabindex='0']";
let keepingFocus = false;
function keepFocus(root, redraw, to) {
  const was = document.activeElement, inside = !keepingFocus && !!was && was !== document.body && root.contains(was);
  if (!inside) { redraw(); return; }
  const sig = (e) => `${e.tagName}|${(e.textContent || "").trim()}|${e.getAttribute("href") || ""}`;
  const key = was.dataset.key || null, mine = sig(was);
  // data-nofocus: never hand focus back (an armed two-tap control: a stray Enter or Space would be the second tap).
  const usable = (e) => e && !e.hasAttribute("disabled") && !e.hasAttribute("data-nofocus");
  const alike = () => (key ? [] : [...root.querySelectorAll(FOCUSABLE)].filter((e) => usable(e) && sig(e) === mine));
  // An unkeyed control is the same one only if it was the only one like it before the redraw too: when one of two
  // "Delete" buttons goes away, the one left belongs to another item, and focusing it would let the next Enter delete it.
  const before = alike().length;
  keepingFocus = true;
  try { redraw(); } finally { keepingFocus = false; }
  const byKey = (k) => k && [...root.querySelectorAll("[data-key]")].find((e) => e.dataset.key === k && usable(e));
  const same = before === 1 ? alike() : [];
  const el = byKey(key) || byKey(to) || (same.length === 1 ? same[0] : null);
  if (el) el.focus({ preventScroll: true });
}

const num = (v, digits = 0) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(digits));
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");
const dur = (s) => {
  if (s === null || s === undefined) return "—";
  s = Math.round(s);
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
};
const dot = (color) => h("span", { class: "dot", style: `background:${color}` });
const STACKED = window.matchMedia("(max-width: 900px), (orientation: landscape) and (max-height: 500px) and (pointer: coarse)");   // style.css: narrow screens, or a phone on its side
const chip = (color, ...content) => h("span", { class: "chip" }, color ? dot(color) : null, ...content);
// A chip that goes where you can act on it.
const chipLink = (href, color, ...content) => h("a", { class: "chip link", href }, color ? dot(color) : null, ...content);
// Something that needs you (an error, a guard, a run waiting): tinted in its colour so it stands out from the status.
const alertChip = (href, color, ...content) => h("a", { class: "chip link alert", href, style: `--c:${color}` }, dot(color), ...content);
// Chips that belong together drawn as one pill with dividers; title names the group for screen readers.
const chipGroup = (cls, title, ...chips) => {
  const kids = chips.flat().filter(Boolean);
  return kids.length ? h("span", { class: `chipgroup ${cls}`, role: "group", "aria-label": title }, ...kids) : null;
};
const titled = (el, title) => { el.title = title; return el; };
// A connection's label: the word always, the detail only where there is room (style.css hides .more when narrow).
const connLabel = (word, more) => h("span", null, word, more ? h("span", { class: "more" }, more) : null);
const mono = (text) => h("span", { class: "mono" }, text);
const kv = (k, ...v) => h("div", { class: "kv" }, h("span", { class: "muted" }, k), h("span", null, ...v));
const card = (title, ...content) => h("div", { class: "card" }, h("p", { class: "label" }, title), ...content);
const colored = (color, text) => h("span", { style: `color:${color}` }, text);

const state = {
  status: null,
  map: null,
  mapData: null,
  zoneMarkers: [],
  layers: { imagery: true, zones: true, labels: true, track: true, trouble: false, cameras: true, signal: false },
  ground3d: readPref("ground3d", false, (v) => typeof v === "boolean"),           // the map's 3D view, per browser
  ground3dX: readPref("ground3dX", 2, (v) => [1, 1.5, 2, 3].includes(v)),         // drawn this much taller
  ground3dMeta: null,     // /api/elevation/ground once read: version, bounds, relief, sources
  ground3dCompass: null,
  mapReady: false,
  trackColor: readPref("trackColor", "rtk", (v) => v === "rtk" || v === "edge"),   // live track: RTK fix | edge passes
  signalMetric: readPref("signalMetric", "halow", (v) => ["halow", "rtk", "sats", "wifi"].includes(v)),
  signalNet: readPref("signalNet", "", (v) => typeof v === "string"),      // the Wi-Fi network on the Signal map
  signalAt: 0,
  plansCache: [],
  view: "live",
  selectedRun: "recent",
  run: null,              // { id, info, fields, rows, coverage, range }
  metric: "rtk",
  replayIndex: 0,
  replayTimer: null,
  replayOn: false,        // once Play or the slider is used, the run's track is drawn only up to the replay point
  replaySpeed: readPref("replaySpeed", 60, (v) => [1, 10, 60, 300, 0].includes(v)),   // x real time; 0 = the whole run in ~30 s
  replayT: null,          // the robot time a replay at a real speed has reached (between two samples)
  dataCache: null,
  fitted: false,
  imagery: null,          // { yard: {url, coordinates}, alignment, ref }
  align: null,            // working copy while aligning
  alignHandlers: null,
  healthHours: 24,
  live: { rows: [], last: 0, busy: false },   // the live map's track: kept here, only new points are fetched
  runLive: { busy: false },
  pulse: { marker: null },
};
function readPref(key, fallback, ok) {
  try { const v = JSON.parse(localStorage.getItem(`yarbo.map.${key}`)); return ok(v) ? v : fallback; } catch { return fallback; }
}
function writePref(key, v) { try { localStorage.setItem(`yarbo.map.${key}`, JSON.stringify(v)); } catch { /* per-browser nicety */ } }
const EDGE = MAP.edge;
const OFFPLAN = MAP.offplan;
// Problem areas: one colour per cause (stuck and errors are the urgent ones).
const PROBLEM = {
  stuck: { color: tok("--prob-stuck", "#F08A6E"), label: "Stuck / no progress" },
  error: { color: tok("--prob-error", "#E46FB8"), label: "Robot error" },
  steep: { color: tok("--prob-steep", "#F0B955"), label: "Steep ground" },
  position: { color: tok("--prob-position", "#9AA7FF"), label: "Weak positioning" },
  effort: { color: tok("--prob-effort", "#D9D46A"), label: "Hard going" },
};
const problemColorExpr = () => ["match", ["get", "cause"], ...Object.entries(PROBLEM).flatMap(([k, v]) => [k, v.color]), MUTED];
const TRACK_GAP_S = 5;   // a longer gap between samples is a break in the line (lost link, parked), not a straight jump

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) {
    let why = "";                                  // the server's own reason ({detail}) when it gives one
    try { const d = await r.json(); why = d.detail || d.error || ""; } catch { /* not JSON */ }
    throw Object.assign(new Error(typeof why === "string" && why ? why : `${url}: ${r.status}`), { status: r.status });
  }
  return r.json();
}
async function putJSON(url, body) {
  const r = await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

/* ------------------------------------------------------------------ map */
const EMPTY = { type: "FeatureCollection", features: [] };

// The imagery layers (world, near the yard, the yard itself) are added once the server says what they are.
function baseStyle() {
  return {
    version: 8,
    sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#0E1311" } }],
  };
}

const firstLayer = (...ids) => ids.find((id) => state.map.getLayer(id));
const CONTENT_LAYERS = ["areas-fill", "swath-line", "track-line", "trouble-fill"];

/* Around the yard, two server-proxied tile layers: the worldwide one when zoomed out, and the chosen imagery
   source from zoom 12 in (only near the yard). Rebuilt when the server reports a different layer. */
function syncContextLayers(ctx) {
  const map = state.map;
  const w = ctx && ctx.world, s = ctx && ctx.source;
  const wantWorld = w ? `${w.max_zoom}|${w.attribution}` : null;
  if (state.ctxWorld !== wantWorld) {
    if (map.getLayer("world")) map.removeLayer("world");
    if (map.getSource("world")) map.removeSource("world");
    if (w) {
      map.addSource("world", { type: "raster", tiles: ["/tiles/world/{z}/{x}/{y}.jpg"], tileSize: 256, maxzoom: w.max_zoom,
                               attribution: w.attribution });
      map.addLayer({ id: "world", type: "raster", source: "world", paint: { "raster-opacity": 0.85, "raster-fade-duration": 0 } },
                   firstLayer("ctx", "yard", ...CONTENT_LAYERS));
    }
    state.ctxWorld = wantWorld;
  }
  const wantSrc = s ? `${s.version}|${s.min_zoom}|${s.max_zoom}` : null;
  if (state.ctxSrc !== wantSrc) {
    if (map.getLayer("ctx")) map.removeLayer("ctx");
    if (map.getSource("ctx")) map.removeSource("ctx");
    if (s) {
      map.addSource("ctx", { type: "raster", tiles: [`/tiles/src/{z}/{x}/{y}.jpg?v=${s.version}`], tileSize: 256,
                             minzoom: s.min_zoom, maxzoom: s.max_zoom });
      map.addLayer({ id: "ctx", type: "raster", source: "ctx", paint: { "raster-fade-duration": 0 } },
                   firstLayer("yard", ...CONTENT_LAYERS));
    }
    state.ctxSrc = wantSrc;
  }
}

function edgeColorExpr() {
  return ["match", ["get", "v"], 1, EDGE, 0, MAP.fixed, MUTED];
}
function liveTrackExpr() { return state.trackColor === "edge" ? edgeColorExpr() : rtkColorExpr(); }
function rtkColorExpr() {
  return ["match", ["get", "v"], 4, MAP.fixed, 5, MAP.float, 1, MAP.single, 2, MAP.single, MUTED];
}

// A hatched fill for no-go zones (diagonal stripes in `color`), made once per colour.
function hatch(map, color) {
  const id = `hatch-${color}`;
  if (map.hasImage(id)) return id;
  const c = document.createElement("canvas");
  c.width = c.height = 16;
  const g = c.getContext("2d");
  g.fillStyle = color;
  g.globalAlpha = 0.18;
  g.fillRect(0, 0, 16, 16);
  g.globalAlpha = 0.85;
  g.strokeStyle = color;
  g.lineWidth = 2.5;
  for (const o of [-16, 0, 16]) { g.beginPath(); g.moveTo(o, 16); g.lineTo(o + 16, 0); g.stroke(); }
  map.addImage(id, g.getImageData(0, 0, 16, 16), { pixelRatio: 2 });
  return id;
}

async function initMap() {
  // Open near the yard when the map is already known, instead of flashing a continent first.
  let start = { center: [-96, 39], zoom: 3 };
  try {
    const b = (await getJSON("/api/map")).bounds;
    if (b) start = { bounds: [[b[0], b[1]], [b[2], b[3]]], fitBoundsOptions: { padding: 50 } };
  } catch { /* the robot's map is not fetched yet; loadZones fits later */ }
  const map = new maplibregl.Map({ container: MAP_EL, style: baseStyle(), maxZoom: 22,
                                   antialias: true,          // smooth edges on the 3D robot (drawn into the map's canvas)
                                   attributionControl: { compact: true }, ...start });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  state.map = map;
  // #map moves into a card in Live's camera layout; there it always shows the whole yard, so a new size refits it
  // (the card is often laid out after the map moved in, when a fit could not work yet).
  new ResizeObserver(() => { map.resize(); if (MAP_EL.closest(".minimap")) fitYard(); }).observe(MAP_EL);
  if (MAP_EL.closest(".minimap")) lockMiniMap(true);              // Camera was the saved layout: the map starts in the card
  map.on("movestart", (e) => {
    if (e.originalEvent || !STACKED.matches) return;
    const box = $(".mapwrap").getBoundingClientRect();
    if (box.bottom < box.height / 2) $(".mapwrap").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  map.on("load", () => {
    for (const id of ["swath", "trouble", "signal", "gaps", "track", "replay", "planned", "offplan"]) map.addSource(id, { type: "geojson", data: EMPTY });
    map.addLayer({ id: "swath-line", type: "line", source: "swath", layout: { "line-cap": "round", "line-join": "round" },
                   paint: { "line-color": MAP.fixed, "line-opacity": 0.22, "line-width": 4 } });
    map.addLayer({ id: "gaps-fill", type: "fill", source: "gaps", paint: { "fill-color": MAP.gap, "fill-opacity": 0.25 } });
    map.addLayer({ id: "gaps-line", type: "line", source: "gaps", paint: { "line-color": MAP.gap, "line-width": 2, "line-dasharray": [2, 1.5] } });
    map.addLayer({ id: "track-line", type: "line", source: "track", layout: { "line-cap": "round", "line-join": "round" },
                   paint: { "line-width": 3.5, "line-opacity": 0.95, "line-color": liveTrackExpr() } });
    // Problem areas sit over the track while switched on (they are the point of looking).
    map.addLayer({ id: "trouble-fill", type: "fill", source: "trouble",
                   paint: { "fill-color": problemColorExpr(), "fill-opacity": ["interpolate", ["linear"], ["get", "severity"], 0.25, 0.35, 1, 0.75] } });
    map.addLayer({ id: "trouble-line", type: "line", source: "trouble",
                   paint: { "line-color": problemColorExpr(), "line-width": 1.5, "line-opacity": 0.9 } });
    map.addSource("trouble-events", { type: "geojson", data: EMPTY });
    map.on("click", "trouble-fill", (e) => problemPopup(e));
    map.on("mouseenter", "trouble-fill", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "trouble-fill", () => { map.getCanvas().style.cursor = ""; });
    map.addLayer({ id: "trouble-events", type: "circle", source: "trouble-events",
                   paint: { "circle-radius": 6, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 2.5,
                            "circle-stroke-color": ["match", ["get", "kind"], "error", PROBLEM.error.color, PROBLEM.stuck.color] } });
    // The signal grid sits over the areas and the track while it is switched on; the robot stays on top.
    map.addLayer({ id: "signal-fill", type: "fill", source: "signal",
                   paint: { "fill-color": ["interpolate", ["linear"], ["get", "t"], 0, CORAL, 0.5, AMBER, 1, GREEN], "fill-opacity": 0.7 } });
    addRobotLayers();
    // Planned vs driven: the robot's plan as a thin dashed line under the track, the stretches off it in coral on top.
    map.addLayer({ id: "planned-line", type: "line", source: "planned", layout: { "line-join": "round" },
                   paint: { "line-color": "#F2F5F3", "line-width": ["interpolate", ["linear"], ["zoom"], 18, 0.8, 22, 1.8],
                            "line-opacity": 0.75, "line-dasharray": [2, 2] } }, "track-line");
    map.addLayer({ id: "offplan-line", type: "line", source: "offplan", layout: { "line-cap": "round", "line-join": "round" },
                   paint: { "line-color": OFFPLAN, "line-width": ["interpolate", ["linear"], ["zoom"], 18, 3, 22, 7], "line-opacity": 0.9 } });
    map.addLayer({ id: "replay", type: "circle", source: "replay",
                   paint: { "circle-radius": 8, "circle-color": "#F2F5F3", "circle-stroke-color": "#0E1311", "circle-stroke-width": 3 } });
    opsMapInit();
    studioMapInit();
    loadZones();
    loadImagery();
    if (state.run) { drawRunTrack(); drawCoverage(); } else showRecentTrack({ full: true });
    if (state.status) styleRobot(state.status);
    state.mapReady = true;
    sync3d();
    // MapLibre opens the compact attribution expanded, once, when the first source with a credit arrives (that can be
    // long after load); on a phone it would sit over the legend until the first drag.
    const fold = () => {
      const el = $(".maplibregl-ctrl-attrib");
      if (!el?.classList.contains("maplibregl-compact-show")) return;
      if (STACKED.matches) el.classList.remove("maplibregl-compact-show");
      state.map.off("sourcedata", fold);
    };
    state.map.on("sourcedata", fold);
    fold();
  });
}

async function loadImagery() {
  try {
    const info = await getJSON("/api/imagery");
    syncContextLayers(info.context);
    if (!info.yard) { setTimeout(loadImagery, 10000); return; }
    state.imagery = info;
    const map = state.map;
    const src = map.getSource("yard");
    if (!src) {
      map.addSource("yard", { type: "image", url: info.yard.url, coordinates: alignedCorners(info.alignment) });
      const before = firstLayer(...CONTENT_LAYERS);
      map.addLayer({ id: "yard", type: "raster", source: "yard",
                     paint: { "raster-fade-duration": 0, "raster-opacity": info.alignment.opacity ?? 1 } }, before);
    } else {
      src.updateImage({ url: info.yard.url, coordinates: alignedCorners(info.alignment) });
      paint("yard", "raster-opacity", info.alignment.opacity ?? 1);
    }
    imageryCredit(info.yard);
    applyLayerVisibility();
  } catch (e) {
    setTimeout(loadImagery, 10000);
  }
}

// Which imagery is under the map, how sharp it is, and the credit its terms ask for.
function imageryCredit(yard) {
  let el = $("#imgcredit");
  if (!el) { el = h("div", { id: "imgcredit", class: "imgcredit" }); $(".mapwrap").append(el); }
  const src = yard && yard.source;
  if (!src) { el.textContent = yard?.attribution || ""; return; }
  const cm = src.m_per_px * 100;
  fill(el, `${src.attribution} · ${cm < 10 ? cm.toFixed(1) : Math.round(cm)} cm/px · `,
       h("button", { type: "button", class: "linkbtn", title: "Line the aerial photo up with the robot's map",
                     onclick: () => { if (state.align) return; if (state.view === "live") { robot3dFollow(false); startAlign(); } else beginAlign(); } }, "Align"));
}

async function loadZones() {
  let data;
  try {
    data = await getJSON("/api/map");
  } catch (e) {
    setTimeout(loadZones, 5000);
    return;
  }
  state.mapData = data;
  const nfar = (data.far || []).length;
  if (nfar) toast(`${nfar} map shape${nfar > 1 ? "s" : ""} far from the yard left out of the view: see Map Studio.`);
  const map = state.map;
  map.addSource("zones", { type: "geojson", data: data.geojson });
  const kind = (k) => ["==", ["get", "kind"], k];
  // Pathways look like roads, as in the app: a grey band about a robot wide (in metres, so it grows with the zoom like
  // the ground does), a dark edge, and a dashed centre line.
  const lat = data.bounds ? (data.bounds[1] + data.bounds[3]) / 2 : 43;
  const px22 = (m) => m / ((156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** 22);
  const wide = (m, least) => ["interpolate", ["exponential", 2], ["zoom"], 16, least, 19, Math.max(least + 1, px22(m) / 8), 22, px22(m)];
  const roads = ["in", ["get", "kind"], ["literal", ["pathways", "deadends"]]];
  const layers = [
    { id: "areas-fill", type: "fill", filter: kind("areas"), paint: { "fill-color": MAP.area, "fill-opacity": 0.08 } },
    { id: "areas-line", type: "line", filter: kind("areas"), paint: { "line-color": MAP.area, "line-width": 2.5 } },
    { id: "snow-fill", type: "fill", filter: kind("snowpiles"), paint: { "fill-color": "#E6ECE8", "fill-opacity": 0.18 } },
    { id: "snow-line", type: "line", filter: kind("snowpiles"), paint: { "line-color": "#E6ECE8", "line-width": 1.5, "line-dasharray": [2, 2] } },
    { id: "nogo-fill", type: "fill", filter: kind("nogozones"), paint: { "fill-pattern": hatch(map, MAP.nogo), "fill-opacity": 0.9 } },
    { id: "nogo-line", type: "line", filter: kind("nogozones"), paint: { "line-color": MAP.nogo, "line-width": 2.5 } },
    { id: "novis-line", type: "line", filter: kind("novisionzones"), paint: { "line-color": MAP.novis, "line-width": 2.5, "line-dasharray": [2, 1.5] } },
    { id: "fence-line", type: "line", filter: kind("elec_fence"), paint: { "line-color": MAP.fence, "line-width": 2.5 } },
    { id: "sidewalks", type: "line", filter: kind("sidewalks"), paint: { "line-color": "#C9D4CE", "line-width": 4, "line-opacity": 0.8 } },
    { id: "paths-edge", type: "line", filter: roads, layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#0B100E", "line-opacity": 0.7, "line-width": wide(1.15, 5) } },
    { id: "paths-road", type: "line", filter: roads, layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": MAP.road, "line-opacity": 0.95, "line-width": wide(0.9, 3.5) } },
    { id: "paths", type: "line", filter: roads, layout: { "line-join": "round" },
      paint: { "line-color": "#F2F5F3", "line-width": ["interpolate", ["linear"], ["zoom"], 17, 1, 22, 2.5], "line-dasharray": [2, 2] } },
    { id: "charging", type: "circle", filter: kind("charging"),
      paint: { "circle-radius": 6, "circle-color": "#0E1311", "circle-stroke-color": GREEN, "circle-stroke-width": 2.5 } },
  ];
  // Area fill sits under the run layers; every outline sits above them.
  map.addLayer({ ...layers[0], source: "zones" }, "swath-line");
  for (const l of layers.slice(1)) map.addLayer({ ...l, source: "zones" }, "track-line");
  if (map.getLayer("yard")) map.moveLayer("yard", "areas-fill");
  raiseGroundShade();
  addZoneLabels(data.geojson);
  applyLayerVisibility();
  if (!state.fitted || MAP_EL.closest(".minimap")) state.fitted = fitYard();   // the small map always shows the whole yard
  raiseCameraLayers();
  renderPlans();
  if (state.view === "studio") renderStudioPanel();      // the shape list names a pathway's dock end from this map
}

// Fit the map to the yard. A hidden map (Settings, Health…) has no size, so a fit then is wasted: report false
// and let showView fit it once it is visible.
function fitYard(duration = 0) {
  const b = state.mapData && state.mapData.bounds;
  const el = MAP_EL;
  if (!b || !state.map || !el.offsetWidth) return false;
  state.map.resize();
  state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: el.offsetHeight < 320 ? 16 : 50, duration });
  return true;
}

function centroid(ring) {
  const pts = ring.slice(0, -1);
  let x = 0, y = 0;
  for (const [px, py] of pts) { x += px; y += py; }
  return [x / pts.length, y / pts.length];
}

// The point halfway along a line, by length (most pathways have two or three points, so the middle vertex is an end).
function lineMid(c) {
  if (c.length < 2) return c[0];
  const k = Math.cos((c[0][1] * Math.PI) / 180), seg = [];
  for (let i = 1; i < c.length; i++) seg.push(Math.hypot((c[i][0] - c[i - 1][0]) * k, c[i][1] - c[i - 1][1]));
  let left = seg.reduce((a, b) => a + b, 0) / 2;
  for (let i = 1; i < c.length; i++) {
    if (left <= seg[i - 1] || i === c.length - 1) {
      const t = seg[i - 1] ? Math.min(1, left / seg[i - 1]) : 0;
      return [c[i - 1][0] + (c[i][0] - c[i - 1][0]) * t, c[i - 1][1] + (c[i][1] - c[i - 1][1]) * t];
    }
    left -= seg[i - 1];
  }
  return c[c.length - 1];
}

function addZoneLabels(fc) {
  state.zoneMarkers.forEach((m) => m.remove());
  state.zoneMarkers = [];
  for (const f of fc.features) {
    const p = f.properties;
    let at = null;
    if (f.geometry.type === "Polygon" && ["areas", "nogozones", "novisionzones"].includes(p.kind)) {
      at = centroid(f.geometry.coordinates[0]);
    } else if (f.geometry.type === "LineString" && (p.kind === "sidewalks" || p.kind === "pathways")) {
      at = lineMid(f.geometry.coordinates);
    }
    if (!at || !p.name) continue;
    // A pathway is a short line: its name sits just above the middle, so it never hides the line itself.
    const path = p.kind === "pathways";
    const el = h("div", { class: `zlabel${p.kind === "nogozones" ? " nogo" : path ? " path" : ""}` }, p.name);
    state.zoneMarkers.push(new maplibregl.Marker({ element: el, ...(path ? { anchor: "bottom", offset: [0, -8] } : {}) })
      .setLngLat(at).addTo(state.map));
  }
  applyLayerVisibility();
}

// The robot is sky blue (amber while held mid-plan) so it never blends into the green areas and track; while it
// works, a ring pulses out from it to draw the eye.
// The ring is an HTML marker animated in CSS, so it costs nothing on the map canvas.
// An arrowhead inside the dot points where the robot is heading (#19): CombinedOdom.phi, which matched the driven
// direction on run 4 (telemetry.heading_deg). It turns with the map, so it stays true when the map is rotated.
function addRobotLayers() {
  const map = state.map;
  map.addSource("robot", { type: "geojson", data: EMPTY });
  const el = h("div", { class: "robotpulse", "aria-hidden": "true" });
  state.pulse.marker = new maplibregl.Marker({ element: el, anchor: "center" });
  map.addLayer({ id: "robot-halo", type: "circle", source: "robot",
                 paint: { "circle-radius": 15, "circle-color": MAP.robot, "circle-opacity": 0.25 } });
  map.addLayer({ id: "robot", type: "circle", source: "robot",
                 paint: { "circle-radius": 7.5, "circle-color": MAP.robot, "circle-stroke-color": MAP.onmap, "circle-stroke-width": 2.5 } });
  map.addLayer({ id: "robot-heading", type: "symbol", source: "robot", filter: ["has", "heading"],
                 layout: { "icon-image": headingArrow(map), "icon-rotate": ["get", "heading"], "icon-rotation-alignment": "map",
                           "icon-allow-overlap": true, "icon-ignore-placement": true } });
}

// The heading arrowhead, pointing up (north before icon-rotate), made once.
function headingArrow(map) {
  const id = "robot-heading";
  if (map.hasImage(id)) return id;
  const c = document.createElement("canvas");
  c.width = c.height = 20;
  const g = c.getContext("2d");
  g.fillStyle = MAP.onmap;
  g.beginPath(); g.moveTo(10, 3); g.lineTo(16, 15); g.lineTo(10, 12); g.lineTo(4, 15); g.closePath(); g.fill();
  map.addImage(id, g.getImageData(0, 0, 20, 20), { pixelRatio: 2 });
  return id;
}

function styleRobot(s) {
  const act = s && s.activity;
  const color = s && s.state === "error" ? MAP.error : HELD.has(act) ? MAP.held : MAP.robot;
  for (const layer of ["robot", "robot-halo"]) paint(layer, "circle-color", color);
  robot3dColour(color);
  const el = state.pulse.marker && state.pulse.marker.getElement();
  if (!el) return;
  el.style.setProperty("--pulse", color);
  el.classList.toggle("on", BUSY.has(act) && !(s && s.lost_contact));   // stale data must not look alive
}

function stopPulse() {
  const el = state.pulse.marker && state.pulse.marker.getElement();
  if (el) el.classList.remove("on");
}

function setRobot(pos) {
  const src = state.map && state.map.getSource("robot");
  if (!src || !pos || pos.lon === null || pos.lon === undefined) return;
  const props = typeof pos.heading === "number" ? { heading: pos.heading } : {};
  src.setData({ type: "FeatureCollection", features: [{ type: "Feature", properties: props,
    geometry: { type: "Point", coordinates: [pos.lon, pos.lat] } }] });
  const m = state.pulse.marker;
  if (m) { m.setLngLat([pos.lon, pos.lat]); if (!state.pulse.added) { m.addTo(state.map); state.pulse.added = true; } }
}

/** setPaintProperty that is a no-op until the layer exists (e.g. a run clicked before the map loaded). Not gated on
 * isStyleLoaded(): that is false while any source is loading, including right after every setData, so colour changes
 * that followed a track update were silently dropped. */
function paint(layer, prop, value) {
  const map = state.map;
  try { if (map && map.getLayer(layer)) map.setPaintProperty(layer, prop, value); } catch { /* style not parsed yet */ }
}

function setSource(id, data) {
  const src = state.map && state.map.getSource(id);
  if (src) src.setData(data || EMPTY);
}

function applyLayerVisibility() {
  const map = state.map;
  if (!map) return;
  const set = (id, on) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  set("yard", state.layers.imagery);
  set("world", state.layers.imagery);
  set("ctx", state.layers.imagery);
  for (const id of ["areas-fill", "areas-line", "snow-fill", "snow-line", "nogo-fill", "nogo-line", "novis-line",
                    "fence-line", "sidewalks", "paths-edge", "paths-road", "paths", "charging"]) set(id, state.layers.zones);
  // Map Studio shows the shapes: the run layers step aside there (their tools are hidden too).
  const studioOn = state.view === "studio";
  const on = (key) => state.layers[key] && !(studioOn && ["track", "trouble", "signal"].includes(key));
  for (const id of ["track-line", "swath-line", "gaps-fill", "gaps-line", "replay", "planned-line", "offplan-line"]) set(id, on("track"));
  set("trouble-fill", on("trouble"));
  set("trouble-line", on("trouble"));
  set("trouble-events", on("trouble"));
  const pbox = $("#problembox");
  if (pbox) pbox.hidden = !state.layers.trouble;
  set("signal-fill", on("signal"));
  const box = $("#signalbox");
  if (box) box.hidden = !state.layers.signal;
  set("cameras-fill", camerasVisible());
  set("cameras-line", camerasVisible());
  state.zoneMarkers.forEach((m) => { m.getElement().style.display = state.layers.labels ? "" : "none"; });
  applyCamVisibility();
}

// One Cameras tool. In Map Studio it shows coverage only when asked (coverage crowds the shapes being edited), and
// remembers that separately from the other views.
function camerasVisible() {
  return state.view === "studio" ? typeof studio !== "undefined" && !!studio.camerasShown : state.layers.cameras;
}

function syncToolButtons() {
  for (const b of $$(".tool[data-layer]")) {
    const on = !!(b.dataset.layer === "cameras" ? camerasVisible() : state.layers[b.dataset.layer]);
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  }
}

function fitTo(coords) {
  if (!coords.length) return;
  const xs = coords.map((c) => c[0]), ys = coords.map((c) => c[1]);
  state.map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: 60 });
}

// The phone's More menu (style.css shows it only at 700 px or less). The tools in it are the same buttons as on a
// wide screen, so they keep their handlers and on state. Toggles leave it open; Fit yard, which moves the map, and
// a tap outside or Escape close it.
function bindToolMore() {
  const row = $(".maptools"), btn = $("#toolmorebtn");
  const show = (open) => { row.classList.toggle("open", open); btn.setAttribute("aria-expanded", String(open)); };
  btn.addEventListener("click", () => show(!row.classList.contains("open")));
  $("#recenter").addEventListener("click", () => show(false));
  document.addEventListener("pointerdown", (e) => { if (!row.contains(e.target)) show(false); });
  window.addEventListener("hashchange", () => show(false));   // a link or Back changes the view without a tap here
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && row.classList.contains("open")) { show(false); btn.focus(); }
  });
}

function bindMapTools() {
  for (const b of $$(".tool[data-layer]")) {
    b.addEventListener("click", () => {
      const key = b.dataset.layer;
      if (key === "cameras" && state.view === "studio") {
        studio.camerasShown = !studio.camerasShown;
        syncToolButtons();
        applyLayerVisibility();
        return;
      }
      state.layers[key] = !state.layers[key];
      syncToolButtons();
      if (key === "trouble" && state.layers.trouble) loadProblems();
      if (key === "trouble" && state.view === "live" && state.status) renderLive(state.status);
      if (key === "signal" && state.layers.signal) loadSignal();
      applyLayerVisibility();
    });
  }
  bindToolMore();
  $("#recenter").addEventListener("click", () => { robot3dFollow(false); fitYard(400); });
  $("#follow3d").addEventListener("click", () => robot3dFollow(!R3D.follow.on));
  $("#ground3d").addEventListener("click", () => {
    state.ground3d = !state.ground3d;
    writePref("ground3d", state.ground3d);
    sync3d();
    if (state.ground3d) refreshGround();
  });
  $("#ground3dsel").value = String(state.ground3dX);
  $("#ground3dsel").addEventListener("change", (e) => {
    state.ground3dX = Number(e.target.value);
    writePref("ground3dX", state.ground3dX);
    if (state.map && state.map.getTerrain()) state.map.setTerrain({ source: "ground3d", exaggeration: state.ground3dX });
  });
  // One control for the track's colour. The live track knows RTK fix and edge passes; a selected run knows more.
  $("#trackmetric").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "rtk" || v === "edge") { state.trackColor = v; writePref("trackColor", v); }
    state.metric = v;
    if (state.run) { drawRunTrack(); renderRuns(); } else showRecentTrack({ redraw: true });
    renderTrackLegend();
  });
  renderColourControl();
  renderTrackLegend();
  $("#signalmetric").value = state.signalMetric;
  $("#signalmetric").addEventListener("change", (e) => { state.signalMetric = e.target.value; writePref("signalMetric", e.target.value); loadSignal(); });
  $("#signalnet").addEventListener("change", (e) => { state.signalNet = e.target.value; writePref("signalNet", e.target.value); loadSignal(); });
}

function renderColourControl() {
  const sel = $("#trackmetric");
  if (!sel) return;
  const cur = state.run ? state.metric : state.trackColor;
  fill(sel, (state.run ? Object.keys(METRICS) : ["rtk", "edge"]).map((k) => h("option", { value: k, selected: k === cur || null, title: METRICS[k].label }, METRICS[k].short)));
  sel.value = cur;
}

// The map's legend says what the track's colour means: categories, or a ramp with the run's range.
function renderTrackLegend() {
  const el = $("#legend-track");
  if (!el) return;
  const m = METRICS[state.run ? state.metric : state.trackColor];
  if (m.categorical) {
    fill(el, m.legend.map(([t, c]) => h("span", null, h("i", { class: "sw line", style: `background:${c}` }), t)));
    return;
  }
  const { min, max } = (state.run && state.run.range) || {};
  const end = (v) => `${num(v, m.digits)} ${m.unit}`.trim();
  fill(el, h("span", null, `${m.label}:`, " ", end(m.invert ? max : min), h("i", { class: "sw ramp" }), end(m.invert ? min : max)));
}

/* ------------------------------------------------------------ 3D ground */
// The 3D shading sits on the photo (loadZones moves the photo just under the areas), under the areas.
function raiseGroundShade() {
  const map = state.map;
  if (map && map.getLayer("ground3d-shade") && map.getLayer("areas-fill")) map.moveLayer("ground3d-shade", "areas-fill");
}

// MapLibre terrain from /api/elevation/ground (app/elevation.py: the robot's own heights over the lawns, public
// lidar around them, as one ground). Everything on the map lies on it: photo, zones, track, replay, the robot.
// It stays flat in Map Studio (shapes are drawn on a flat map) and in the camera layout's small map.
const want3d = () => state.ground3d && state.view !== "studio" && !MAP_EL.closest(".minimap");

function syncGround3dTools() {
  const b = $("#ground3d");
  if (!b) return;
  b.classList.toggle("on", state.ground3d);
  b.setAttribute("aria-pressed", String(state.ground3d));
  $("#ground3dx").hidden = !state.ground3d;
  $("#follow3d").hidden = !state.ground3d;
  const m = state.ground3dMeta;
  if (m) b.title = `Tilt the map and raise the ground (${num(m.relief_m, 1)} m of rise here): ` +
    `${m.sources.includes("robot") ? `the robot's own heights over ${Math.round(m.robot_share * 100)} % of it` : "no robot heights yet"}` +
    `${m.sources.includes("lidar") ? ", public lidar (USGS 3DEP) around them" : ""}. Right-drag (two fingers on a phone) turns and tilts.`;
}

const groundTiles = (meta) => `/api/elevation/ground/{z}/{x}/{y}.png?v=${meta.version}`;

// The ground changes when a mow adds heights. A page that already shows one asks again (when a run ends, when 3D is
// switched back on) and, if it is a new one, draws all of it from the new tiles instead of a mix of the two.
async function refreshGround() {
  const map = state.map;
  if (!map || !state.ground3dMeta || !map.getSource("ground3d")) return;
  let meta;
  try { meta = await getJSON("/api/elevation/ground"); } catch { return; }      // the ground on show stays
  if (meta.version === state.ground3dMeta.version || !map.getSource("ground3d")) return;
  const old = state.ground3dMeta;
  if (meta.minzoom !== old.minzoom || meta.maxzoom !== old.maxzoom || String(meta.bounds) !== String(old.bounds)) {
    // A ground over a different area (the robot's map grew): a source's bounds and zooms are fixed when it is
    // added, so take both off and let sync3d add them again from the new ground.
    if (map.getTerrain()) map.setTerrain(null);
    if (map.getLayer("ground3d-shade")) map.removeLayer("ground3d-shade");
    for (const id of ["ground3d", "ground3d-shade"]) if (map.getSource(id)) map.removeSource(id);
    state.ground3dMeta = null;
    return sync3d(false);
  }
  state.ground3dMeta = meta;
  for (const id of ["ground3d", "ground3d-shade"]) map.getSource(id).setTiles([groundTiles(meta)]);
  syncGround3dTools();
}

async function sync3d(animate = true) {
  const map = state.map;
  syncGround3dTools();
  if (!map || !state.mapReady) return;
  const on = want3d();
  if (on && !map.getSource("ground3d")) {
    let meta;
    try { meta = await getJSON("/api/elevation/ground"); } catch (e) {
      toast(`3D view unavailable: ${e.message}`);
      state.ground3d = false;
      writePref("ground3d", false);
      syncGround3dTools();
      return;
    }
    if (map.getSource("ground3d") || !want3d()) return sync3d(animate);     // a second call got there first, or it was switched off
    state.ground3dMeta = meta;
    const dem = { type: "raster-dem", tiles: [groundTiles(meta)], tileSize: 256,
                  encoding: meta.encoding, minzoom: meta.minzoom, maxzoom: meta.maxzoom, bounds: meta.bounds };
    map.addSource("ground3d", dem);
    map.addSource("ground3d-shade", { ...dem });                 // MapLibre wants its own source for the shading
    map.addLayer({ id: "ground3d-shade", type: "hillshade", source: "ground3d-shade", layout: { visibility: "none" },
                   // Lit from 335° on the map, not the screen, so turning the view does not move the light, and the
                   // robot model (web/robot3d.js) is lit from the same side.
                   paint: { "hillshade-illumination-anchor": "map", "hillshade-illumination-direction": 335,
                            "hillshade-exaggeration": 0.3, "hillshade-shadow-color": "#0E1311", "hillshade-accent-color": "#0E1311",
                            "hillshade-highlight-color": "#FFFFFF" } }, firstLayer(...CONTENT_LAYERS));
    raiseGroundShade();
    syncGround3dTools();
  }
  if (!map.getSource("ground3d")) return;
  if (on) {
    map.setMaxPitch(80);
    map.setTerrain({ source: "ground3d", exaggeration: state.ground3dX });
    map.setLayoutProperty("ground3d-shade", "visibility", "visible");
    if (!state.ground3dCompass) {
      state.ground3dCompass = new maplibregl.NavigationControl({ showZoom: false, showCompass: true, visualizePitch: true });
      map.addControl(state.ground3dCompass, "top-left");
    }
    if (map.getPitch() < 20) map[animate ? "easeTo" : "jumpTo"]({ pitch: 60, ...(animate ? { duration: 700 } : {}) });
  } else if (map.getTerrain()) {
    map.setTerrain(null);
    map.setLayoutProperty("ground3d-shade", "visibility", "none");
    if (state.ground3dCompass) { map.removeControl(state.ground3dCompass); state.ground3dCompass = null; }
    map[animate ? "easeTo" : "jumpTo"]({ pitch: 0, bearing: 0, ...(animate ? { duration: 500 } : {}) });
  }
  robot3dSync();
}

/** Signal across the yard: every run's samples averaged into 2 m cells (HaLow, RTK fix share or satellites), or one
    Wi-Fi network from the survey in 4 m cells. */
async function loadSignal() {
  state.signalAt = Date.now();
  const wifi = state.signalMetric === "wifi";
  const seq = (state.signalSeq = (state.signalSeq || 0) + 1);       // a slower, older answer must not win
  try {
    const net = wifi && state.signalNet ? `&network=${encodeURIComponent(state.signalNet)}` : "";
    const d = await getJSON(`/api/signal?metric=${state.signalMetric}&days=30${net}`);
    if (seq !== state.signalSeq) return;
    const pick = $("#signalnet"), nets = d.networks || [];
    pick.hidden = !wifi || !nets.length;
    if (wifi) {
      if (d.network) state.signalNet = d.network;     // the one shown (this browser's saved pick stays as it was)
      fill(pick, ...nets.map((n) => h("option", { value: n.name, selected: n.name === d.network || null },
        `${n.name} · ${n.avg} %${n.saved ? " · saved" : ""}`)));
    }
    const span = Math.max(1e-9, d.hi - d.lo);
    setSource("signal", { type: "FeatureCollection", features: d.cells.map((c) => ({
      type: "Feature", properties: { t: Math.max(0, Math.min(1, (c.value - d.lo) / span)), v: c.value, n: c.n },
      geometry: { type: "Polygon", coordinates: [c.ring] } })) });
    fill($("#signalscale"), h("span", null, `${d.lo}${d.unit ? ` ${d.unit}` : ""}`), h("i", { class: "ramp sig" }),
      h("span", null, `${d.hi}${d.unit ? ` ${d.unit}` : ""}`),
      h("span", { class: "muted" }, d.cells.length ? `· ${d.cells.length} cells, last 30 days`
        : wifi && nets.length ? "· too few scans per spot yet"
        : wifi ? h("span", null, "· no Wi-Fi survey yet (", h("a", { href: "#settings/robot" }, "Settings → Robot"), ")")
        : "· no runs recorded yet"));
    state.signalFailed = false;
  } catch (e) {
    if (seq !== state.signalSeq) return;
    if (!state.signalFailed) toast(`Signal map unavailable: ${e.message}`);
    state.signalFailed = true;
  }
}

/** Problem areas for the last 60 days of runs, or for the selected run. */
async function loadProblems() {
  const runId = state.run ? state.run.id : null;
  const seq = (state.problemsSeq = (state.problemsSeq || 0) + 1);   // a slower, older answer must not win
  state.problemsAt = Date.now();                                     // set before the fetch: no request storm
  try {
    const d = await getJSON(`/api/problems?days=60${runId !== null ? `&run_id=${encodeURIComponent(runId)}` : ""}`);
    if (seq !== state.problemsSeq) return;
    state.problems = { ...d, runId };
    state.problemsFailed = false;
    setSource("trouble", { type: "FeatureCollection", features: d.cells.map((c) => ({
      type: "Feature", properties: { cause: c.cause, severity: c.severity, i: c.i, j: c.j },
      geometry: { type: "Polygon", coordinates: [c.ring] } })) });
    setSource("trouble-events", { type: "FeatureCollection", features: d.events.filter((e) => e.place).map((e) => ({
      type: "Feature", properties: { kind: e.kind, what: e.what }, geometry: { type: "Point", coordinates: [e.lon, e.lat] } })) });
    const counts = {};
    (d.spots || []).forEach((p) => { counts[p.cause] = (counts[p.cause] || 0) + 1; });
    fill($("#problemscale"), ...Object.entries(PROBLEM).filter(([k]) => counts[k]).map(([k, v]) =>
      h("span", null, h("i", { class: "sw", style: `background:${v.color}` }), `${v.label} ${counts[k]}`)),
      h("span", { class: "muted" }, d.spots.length ? (runId !== null ? "· this run" : "· last 60 days") : "· nothing found"));
    if (state.view === "live" && state.status) renderLive(state.status);
  } catch (e) {
    if (seq !== state.problemsSeq) return;
    if (!state.problemsFailed) toast(`Problem areas unavailable: ${e.message}`);
    state.problemsFailed = true;
    state.problems = { cells: [], spots: [], events: [], runId, error: e.message };
    setSource("trouble", EMPTY);
    setSource("trouble-events", EMPTY);
    if (state.view === "live" && state.status) renderLive(state.status);
  }
}

function problemLines(evidence, stats) {
  const lines = Object.entries(evidence).map(([k, v]) => h("div", null, h("b", { style: `color:${PROBLEM[k].color}` }, PROBLEM[k].label), " ", v));
  if (stats) {
    lines.push(h("div", { class: "muted" }, `Here: steepest ${stats.max_tilt}°, ${stats.over12_pct} % at 12° or more · ` +
      `no RTK fix ${stats.nofix_pct} %` + (stats.dop2_pct ? ` · heading DOP ≥ 2 ${stats.dop2_pct} %` : "") +
      (stats.dr_pct ? ` · dead reckoning ${stats.dr_pct} %` : "") +
      (stats.halow_dbm !== null && stats.halow_dbm !== undefined ? ` · HaLow ${stats.halow_dbm} dBm` : "") +
      (stats.late_pct ? ` · corrections late ${stats.late_pct} %` : "")));
  }
  return lines;
}

function problemPopup(e) {
  const f = e.features && e.features[0];
  const cell = f && state.problems && state.problems.cells.find((c) => c.i === f.properties.i && c.j === f.properties.j);
  if (!cell) return;
  new maplibregl.Popup({ closeButton: true, maxWidth: "320px", className: "problempop" })
    .setLngLat(e.lngLat).setDOMContent(h("div", { class: "small" }, ...problemLines(cell.evidence, cell.stats))).addTo(state.map);
}

function problemsCard() {
  const d = state.problems;
  if (!d) return card("Problem areas", h("p", { class: "muted small" }, "Working them out…"));
  if (d.error) return card("Problem areas", h("p", { class: "small", style: `color:${AMBER}` }, `Could not load them: ${d.error}`),
    h("button", { type: "button", class: "btn slim", onclick: loadProblems }, "Try again"));
  if (!d.spots.length) return card("Problem areas", h("p", { class: "muted small" }, "Nothing stands out in the recorded runs."));
  return h("div", { class: "card" },
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, d.runId !== null ? "Problems in this run" : "Problem areas"),
      h("span", { class: "muted small" }, `${d.spots.length} spot${d.spots.length === 1 ? "" : "s"}`)),
    h("ol", { class: "problemlist" }, d.spots.slice(0, 12).map((p) =>
      h("li", null,
        h("button", { type: "button", class: "linkish", title: "Show on the map",
                      onclick: () => state.map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(state.map.getZoom(), 20) }) },
          h("i", { class: "sw", style: `background:${PROBLEM[p.cause].color}` }), h("b", null, PROBLEM[p.cause].label),
          p.cells > 1 ? h("span", { class: "muted" }, ` · ${p.area_m2} m²`) : null),
        h("div", { class: "small" }, p.evidence),
        Object.keys(p.also || {}).length ? h("div", { class: "small muted" }, "Also: ", Object.values(p.also).join(" · ")) : null))));
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 5000);
}

/* The clipboard API needs a secure context, and a panel opened at http://<lan address> is not one: there
   navigator.clipboard is missing, so fall back to a selected textarea and the old copy command. */
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* not a secure context: fall back */ }
  const ta = h("textarea", { style: "position:fixed;left:-9999px;top:0", "aria-hidden": "true", readonly: "" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);   // iOS selects nothing with select() alone
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* nothing more to try */ }
  ta.remove();
  return ok;
}
/* Map shapes far from the rest of the yard (geo.yard_bounds) are left out of the map's view, the imagery and the
   terrain. Say which, so the stray can be fixed or deleted in the Yarbo app. */
const FAR_KIND = { areas: "area", nogozones: "no-go zone", novisionzones: "no-vision zone", elec_fence: "fence",
                   pathways: "pathway", sidewalks: "sidewalk", deadends: "dead end", charging: "charging point",
                   snowpiles: "snow pile" };
function farNote(far) {
  if (!far || !far.length) return null;
  const what = far.map((f) => `${f.name || FAR_KIND[f.kind] || f.kind} (${f.km.toLocaleString()} km away)`).join(", ");
  const why = "A shape on the robot's map far from the rest of your yard is left out of the map's view and of where "
    + "imagery is fetched. Fix or delete it in the Yarbo app; Map Studio lists it.";
  return h("p", { class: "small", style: `color:${AMBER}`, title: why },
    h("b", null, "Left out, far from the yard: "), what, ". ", h("a", { href: "#studio" }, "Map Studio"));
}

const copyToast = async (text) =>
  toast(await copyText(text) ? "Copied." : "Copy did not work here: select it by hand.");

/* ------------------------------------------------------ imagery alignment */
const M_PER_DEG = 111320;

/* Alignment maps the imagery's own position to where it is drawn: scale, then rotate (deg, clockwise), around the
   image centre, then shift east/north (m). All maths is in metres around the image centre. */
function alignFrame() {
  const base = state.imagery.yard.coordinates;
  const cLon = base.reduce((s, c) => s + c[0], 0) / 4, cLat = base.reduce((s, c) => s + c[1], 0) / 4;
  return { cLon, cLat, kx: M_PER_DEG * Math.cos((cLat * Math.PI) / 180) };
}
const llToM = (f, [lon, lat]) => [(lon - f.cLon) * f.kx, (lat - f.cLat) * M_PER_DEG];
const mToLl = (f, [e, n]) => [f.cLon + e / f.kx, f.cLat + n / M_PER_DEG];

function alignApply(a, [e, n]) {
  const th = (-(a.rotate_deg || 0) * Math.PI) / 180, s = a.scale || 1;
  return [s * (e * Math.cos(th) - n * Math.sin(th)) + (a.east_m || 0), s * (e * Math.sin(th) + n * Math.cos(th)) + (a.north_m || 0)];
}

function alignInverse(a, [E, N]) {
  const th = (-(a.rotate_deg || 0) * Math.PI) / 180, s = a.scale || 1;
  const x = (E - (a.east_m || 0)) / s, y = (N - (a.north_m || 0)) / s;
  return [x * Math.cos(th) + y * Math.sin(th), -x * Math.sin(th) + y * Math.cos(th)];
}

/** Image corners after the alignment. */
function alignedCorners(al) {
  if (!state.imagery || !state.imagery.yard) return null;
  const f = alignFrame(), a = al || { east_m: 0, north_m: 0, rotate_deg: 0, scale: 1 };
  return state.imagery.yard.coordinates.map((c) => mToLl(f, alignApply(a, llToM(f, c))));
}

/** Least-squares fit from point pairs (photo spot -> robot spot): shift for one pair; shift and rotation (and scale,
    if allowed) for two or more. Returns the alignment and each pair's leftover error in metres. */
function fitAlignment(pairs, allowScale, current) {
  const f = alignFrame();
  const P = pairs.map((p) => ({ r: llToM(f, p.raw), t: llToM(f, p.target) }));
  let a;
  if (P.length === 1) {
    const [E, N] = alignApply({ ...current, east_m: 0, north_m: 0 }, P[0].r);
    a = { ...current, east_m: P[0].t[0] - E, north_m: P[0].t[1] - N };
  } else {
    const mean = (k, i) => P.reduce((s, p) => s + p[k][i], 0) / P.length;
    const cr = [mean("r", 0), mean("r", 1)], ct = [mean("t", 0), mean("t", 1)];
    let A = 0, B = 0, S = 0;
    for (const p of P) {
      const xr = p.r[0] - cr[0], yr = p.r[1] - cr[1], xt = p.t[0] - ct[0], yt = p.t[1] - ct[1];
      A += xr * xt + yr * yt;
      B += xr * yt - yr * xt;
      S += xr * xr + yr * yr;
    }
    const th = Math.atan2(B, A), s = allowScale && S > 0 ? Math.hypot(A, B) / S : 1;
    const rc = [s * (cr[0] * Math.cos(th) - cr[1] * Math.sin(th)), s * (cr[0] * Math.sin(th) + cr[1] * Math.cos(th))];
    a = { ...current, east_m: ct[0] - rc[0], north_m: ct[1] - rc[1], rotate_deg: (-th * 180) / Math.PI, scale: s };
  }
  const errors = P.map((p) => { const q = alignApply(a, p.r); return Math.hypot(q[0] - p.t[0], q[1] - p.t[1]); });
  return { a, errors };
}

function applyAlignment(al) {
  const src = state.map.getSource("yard");
  if (!src) return;
  src.setCoordinates(alignedCorners(al));
  paint("yard", "raster-opacity", al.opacity ?? 1);
  const out = $("#alignvals");
  if (out) out.textContent = `east ${num(al.east_m, 2)} m · north ${num(al.north_m, 2)} m · rotate ${num(al.rotate_deg, 2)}°` +
                             ((al.scale ?? 1) !== 1 ? ` · scale ${num(((al.scale ?? 1) - 1) * 100, 2)}%` : "");
  drawAlignPairs();
}

/* ------------------------------------------- aligning by point pairs */
// Robot-map spots snap to the nearest zone corner, path point or charging point within 14 px.
function snapToMap(lngLat) {
  const map = state.map, p = map.project(lngLat);
  let best = null;
  for (const f of state.mapData?.geojson?.features || []) {
    const g = f.geometry;
    const pts = g.type === "Polygon" ? g.coordinates[0] : g.type === "LineString" ? g.coordinates : g.type === "Point" ? [g.coordinates] : [];
    for (const c of pts) {
      const q = map.project(c), d = Math.hypot(q.x - p.x, q.y - p.y);
      if (d <= 14 && (!best || d < best.d)) best = { d, c, name: f.properties.name || f.properties.kind };
    }
  }
  return best ? { target: best.c, label: `${best.name} corner` } : { target: [lngLat.lng, lngLat.lat], label: "map point" };
}

function alignStatus(text) { const el = $("#alignstatus"); if (el) el.textContent = text; }

function pickPair(useRobot) {
  if (useRobot) {
    const r = state.status?.robot;
    if (!r?.position?.lon) { toast("The robot's position is not known yet."); return; }
    const fixed = r.rtk?.status === 4;
    state.alignPick = { stage: "photo", target: [r.position.lon, r.position.lat], label: `robot (${r.rtk?.label || "no fix"})` };
    alignStatus(`${fixed ? "" : "The robot is not RTK-fixed, so this point is only as good as its fix. "}Now click where the robot is in the photo.`);
  } else {
    state.alignPick = { stage: "robot" };
    alignStatus("Click a spot on the robot's map (it snaps to zone corners).");
  }
  state.map.getCanvas().style.cursor = "crosshair";
}

function onAlignClick(e) {
  const pick = state.alignPick;
  if (!pick) return;
  if (pick.stage === "robot") {
    Object.assign(pick, snapToMap(e.lngLat), { stage: "photo" });
    alignStatus(`Robot spot: ${pick.label}. Now click the same spot in the photo.`);
    drawAlignPairs();
    return;
  }
  const f = alignFrame();
  const raw = mToLl(f, alignInverse(state.align, llToM(f, [e.lngLat.lng, e.lngLat.lat])));
  state.alignPairs.push({ target: pick.target, raw, label: pick.label });
  state.alignPick = null;
  state.map.getCanvas().style.cursor = "";
  refitAlignment();
}

function refitAlignment() {
  const pairs = state.alignPairs;
  if (!pairs.length) { alignStatus(""); renderAlignPairs([]); return; }
  const allowScale = !!$("#alignscale")?.checked && pairs.length >= 3;
  const { a, errors } = fitAlignment(pairs, allowScale, state.align);
  Object.assign(state.align, a);
  applyAlignment(state.align);
  const rms = Math.sqrt(errors.reduce((s, x) => s + x * x, 0) / errors.length);
  alignStatus(pairs.length === 1 ? "One pair fixes the shift. Add a second, far from the first, to fix the rotation."
                                 : `Fit from ${pairs.length} pairs: typical error ${num(rms * 100, 0)} cm.`);
  renderAlignPairs(errors);
}

function renderAlignPairs(errors) {
  const el = $("#alignpairs");
  if (!el) return;
  fill(el, state.alignPairs.map((p, i) => h("div", { class: "vrow" },
    h("span", { class: "small" }, `${i + 1}. ${p.label}`),
    h("span", { class: "row gap" },
      errors[i] != null && state.alignPairs.length > 1 ? h("span", { class: "muted tiny" }, `${num(errors[i] * 100, 0)} cm off`) : null,
      h("button", { type: "button", class: "btn slim", "aria-label": `Remove pair ${i + 1}`,
                    onclick: () => { state.alignPairs.splice(i, 1); refitAlignment(); drawAlignPairs(); } }, "×")))));
}

// Green dots: robot spots. White rings: the matching photo spots, drawn where the current alignment puts them.
function drawAlignPairs() {
  (state.alignMarkers || []).forEach((m) => m.remove());
  state.alignMarkers = [];
  if (!state.align || !state.alignPairs) return;
  const f = alignFrame();
  const mark = (c, cls) => state.alignMarkers.push(new maplibregl.Marker({ element: h("div", { class: `alignmark ${cls}` }) }).setLngLat(c).addTo(state.map));
  for (const p of state.alignPairs) {
    mark(p.target, "robot");
    mark(mToLl(f, alignApply(state.align, llToM(f, p.raw))), "photo");
  }
  if (state.alignPick?.target) mark(state.alignPick.target, "robot");
}

function startAlign() {
  if (!state.imagery || !state.map.getSource("yard")) { toast("The yard imagery is not loaded yet."); return; }
  state.align = { scale: 1, ...state.imagery.alignment };
  syncLiveMode();
  state.alignPairs = [];
  state.alignPick = null;
  const step = () => Number($("#alignstep").value);
  const nudge = (de, dn) => { state.align.east_m += de * step(); state.align.north_m += dn * step(); applyAlignment(state.align); };
  const rot = (d) => { state.align.rotate_deg += d; applyAlignment(state.align); };
  const panel = h("div", { class: "alignpanel", role: "dialog", "aria-label": "Align imagery" },
    h("p", { class: "label" }, "Align imagery"),
    h("p", { class: "muted small" }, "Drag the map to slide the imagery under the zones, or nudge it. The robot's own map never moves."),
    h("div", { class: "row gap", style: "flex-wrap:wrap" },
      h("label", { for: "alignstep", class: "muted small" }, "Step"),
      h("select", { id: "alignstep", class: "field slim" },
        h("option", { value: "0.1" }, "0.1 m"), h("option", { value: "0.25", selected: true }, "0.25 m"), h("option", { value: "1" }, "1 m"))),
    h("div", { class: "nudge" },
      h("button", { type: "button", class: "btn", "aria-label": "Move imagery north", onclick: () => nudge(0, 1) }, "↑"),
      h("button", { type: "button", class: "btn", "aria-label": "Move imagery west", onclick: () => nudge(-1, 0) }, "←"),
      h("button", { type: "button", class: "btn", "aria-label": "Move imagery east", onclick: () => nudge(1, 0) }, "→"),
      h("button", { type: "button", class: "btn", "aria-label": "Move imagery south", onclick: () => nudge(0, -1) }, "↓"),
      h("button", { type: "button", class: "btn", "aria-label": "Rotate imagery anticlockwise", onclick: () => rot(-0.25) }, "⟲"),
      h("button", { type: "button", class: "btn", "aria-label": "Rotate imagery clockwise", onclick: () => rot(0.25) }, "⟳")),
    h("p", { class: "label", style: "margin-top:6px" }, "Align by points (most exact)"),
    h("p", { class: "muted small" }, "Pick a spot on the robot's map, then the same spot in the photo. Use things on the ground: driveway and path corners, the dock. Two pairs far apart fix the shift and rotation."),
    h("div", { id: "alignpairs" }),
    h("div", { class: "row gap", style: "flex-wrap:wrap" },
      h("button", { type: "button", class: "btn slim", onclick: () => pickPair(false) }, "Add a point pair"),
      h("button", { type: "button", class: "btn slim", onclick: () => pickPair(true) }, "Use the robot's position"),
      h("label", { class: "row gap small" }, h("input", { type: "checkbox", id: "alignscale", onchange: refitAlignment }), "Fit scale (3+ pairs)")),
    h("p", { class: "small", id: "alignstatus" }),
    h("label", { for: "alignop", class: "muted small" }, "Imagery opacity"),
    h("input", { id: "alignop", type: "range", min: "0.2", max: "1", step: "0.05", value: String(state.align.opacity ?? 1),
                 oninput: (e) => { state.align.opacity = Number(e.target.value); applyAlignment(state.align); } }),
    h("p", { class: "mono small", id: "alignvals" }),
    h("div", { class: "row gap" },
      h("button", { type: "button", class: "btn pri", onclick: () => endAlign(true) }, "Save"),
      h("button", { type: "button", class: "btn", onclick: () => { state.align = { east_m: 0, north_m: 0, rotate_deg: 0, scale: 1, opacity: 1 }; state.alignPairs = []; refitAlignment(); applyAlignment(state.align); } }, "Reset"),
      h("button", { type: "button", class: "btn", onclick: () => endAlign(false) }, "Cancel")));
  $(".mapwrap").append(panel);
  applyAlignment(state.align);
  // Drag-to-slide: while aligning, a drag moves the imagery instead of panning the map.
  const map = state.map;
  map.dragPan.disable();
  let from = null, base = null;
  state.alignHandlers = {
    click: onAlignClick,
    down: (e) => { if (state.alignPick) return; from = e.lngLat; base = { ...state.align }; map.getCanvas().style.cursor = "grabbing"; },
    move: (e) => {
      if (!from) return;
      const kx = M_PER_DEG * Math.cos((from.lat * Math.PI) / 180);
      state.align.east_m = base.east_m + (e.lngLat.lng - from.lng) * kx;
      state.align.north_m = base.north_m + (e.lngLat.lat - from.lat) * M_PER_DEG;
      applyAlignment(state.align);
    },
    up: () => { from = null; map.getCanvas().style.cursor = ""; },
  };
  map.on("mousedown", state.alignHandlers.down);
  map.on("mousemove", state.alignHandlers.move);
  map.on("mouseup", state.alignHandlers.up);
  map.on("click", state.alignHandlers.click);
}

async function endAlign(save) {
  const map = state.map;
  if (save) {
    try {
      state.imagery.alignment = await putJSON("/api/settings/imagery_alignment", state.align);
      toast("Imagery alignment saved.");
    } catch (e) { toast(`Could not save: ${e.message}`); return; }
  }
  applyAlignment(state.imagery.alignment);
  map.off("mousedown", state.alignHandlers.down);
  map.off("mousemove", state.alignHandlers.move);
  map.off("mouseup", state.alignHandlers.up);
  map.off("click", state.alignHandlers.click);
  map.dragPan.enable();
  state.align = null;
  state.alignPairs = null;
  state.alignPick = null;
  drawAlignPairs();
  $(".alignpanel")?.remove();
  syncLiveMode();
  const back = state.alignReturn;                   // started from the setup guide or Settings: go back there
  state.alignReturn = null;
  if (back) location.hash = back;
}

// Aligning happens on Live's map. It starts from Live's Align tool, Settings → Map imagery or the setup guide; from
// the last two the page goes back there when it is saved or cancelled.
function beginAlign() {
  const back = state.view !== "live" ? location.hash || null : null;
  if (back) location.hash = "#live";
  setTimeout(() => {
    if (state.align) return;
    startAlign();
    state.alignReturn = state.align ? back : null;     // only once aligning really started (the imagery was loaded)
  }, 150);
}

/* ------------------------------------------------------------- status */
// Busy = sky, held mid-plan (paused, charging break) = amber, at rest = green.
const BUSY = new Set(["mowing", "transit", "planning", "moving", "returning", "docking"]);
const HELD = new Set(["paused", "charging_break"]);
function stateColor(s) {
  if (!s) return MUTED;
  if (s.state === "error") return CORAL;
  if (s.state === "waiting for data") return MUTED;
  if (HELD.has(s.activity)) return AMBER;
  if (BUSY.has(s.activity)) return SKY;
  return GREEN;
}

// Live's first card: what the robot is doing, and while a run goes, how it goes.
function statusCard(s) {
  const r = s.robot, run = s.run, p = (run && run.plan) || {};
  const held = HELD.has(s.activity);
  return h("div", { class: "card" },
    h("div", { class: "row gap", style: "align-items:baseline;flex-wrap:wrap" },
      h("span", { class: "big", style: `color:${stateColor(s)}` }, s.pause_reason ? `paused: ${s.pause_reason}` : s.state),
      h("span", { class: "muted" }, `${num(r.battery)}% · ${r.head}`)),
    r.error_code ? kv("Error code", colored(CORAL, String(r.error_code))) : null,
    run ? [
      kv("Run started", when(run.started)),
      held && run.held_since ? kv(s.activity === "paused" ? "Paused since" : "Docked since", when(run.held_since)) : null,
      kv("Distance", `${num(run.distance_m)} m`),
      s.stalled ? kv("Not moving for", colored(CORAL, dur(s.stalled_s))) : null,
      s.rtk_wait ? kv("Waiting for an RTK fix", colored(AMBER, dur(s.stalled_s))) : null,
      p.progress_pct !== null && p.progress_pct !== undefined
        ? kv("Plan done", `${num(p.progress_pct)}%${p.left_s ? ` · about ${dur(p.left_s)} left` : ""}`) : null,
      h("p", { class: "small", style: "margin:8px 0 0" }, h("a", { href: `#runs/${encodeURIComponent(run.id)}` }, "Open this run"))] : null);
}
function rtkColor(status) {
  if (status === 4) return GREEN;
  if (status === 5) return AMBER;
  if (status === null || status === undefined) return MUTED;
  return CORAL;
}

// Crossing into the stacked layout moves the connections between their two places.
STACKED.addEventListener("change", () => { if (state.status) renderChips(state.status); });
// A link is fine while its dot or bars are green: the colour each chip already picks (compared as the browser spells it).
const GREEN_CSS = dot(GREEN).style.backgroundColor;
const isGreen = (el) => { const m = el.querySelector(".dot, .sigbars"); return !!m && (m.style.backgroundColor || m.style.color) === GREEN_CSS; };
function renderChips(s, streamOk = true) {
  const r = s.robot, local = s.transport.local;
  const docked = s.activity === "docked", rtk = gpsChip(r.rtk, s.activity);
  const links = [
    cloudChip(s),
    titled(chipLink("#settings/robot", local.connected ? GREEN : CORAL, connLabel("Broker", local.connected ? "" : " down")),
           local.connected ? "Local broker: connected" : "Local broker: not connected"),
    halowChip(r.halow_dbm),
    wifiChip(s.wifi),
    rtk,
    streamOk ? null : chip(AMBER, "Reconnecting…")].filter(Boolean);
  // A phone's bar has no room to spare: while every link is fine one chip says so (its tooltip lists them); the
  // moment one is not, they are all spelled out again. The RTK chip is grey on the dock, where that is normal, but
  // corrections that stopped are amber there too and keep it spelled out.
  const linksOk = STACKED.matches && links.every((c) => isGreen(c) || (c === rtk && docked && !correctionsLate(r.rtk)));
  /* Left to right: what needs you (tinted), the switches; then, pushed right, the connections as one pill
     (cloud, local broker, RTK: short words while all is well, the problem spelled out when not), and at the far right
     the robot itself, its state and battery, in the slot that stays in view on a phone. */
  const st = s.pause_reason ? `paused: ${s.pause_reason}` : s.state;
  fill($("#chips"),
    s.demo ? titled(chip(SKY, ...(STACKED.matches ? [h("b", null, "Demo")] : [h("b", null, "Demo data"), " · synthetic robot"])), "Demo data · synthetic robot") : null,
    s.plan_error || s.recharge_error ? alertChip("#health", CORAL, h("b", null, "Robot error"), ` ${s.plan_error || s.recharge_error}`) : null,
    s.stalled ? alertChip("#live", CORAL, h("b", null, "Not moving"), ` for ${dur(s.stalled_s)}`) : null,
    s.rtk_wait ? alertChip("#live", AMBER, h("b", null, "Waiting for an RTK fix"), ` for ${dur(s.stalled_s)}`) : null,
    s.lost_contact ? alertChip("#health", CORAL, h("b", null, "No data"), ` for ${dur(s.data_age_s)}`) : null,
    opsAlerts(s),
    chipGroup("switches", "Switches", opsSwitches(s)),
    linksOk ? chipGroup("conn linksok", "Connections", titled(chipLink("#settings/robot", GREEN, "Links ok"), links.map((c) => c.title).join("\n")))
            : chipGroup("conn", "Connections", links));
  // On a phone the connections get a row of their own instead of scrolling out of sight behind the robot pill.
  if (STACKED.matches) for (const c of links) c.classList.toggle("notok", !isGreen(c));
  fill($("#conn"), STACKED.matches ? $("#chips > .chipgroup.conn") : null);
  fill($("#battery"), chipGroup("robot", "Robot",
    titled(chipLink("#live", stateColor(s), st), `Robot: ${st}`),
    batteryChip(r.battery)));
}

/* Signal bars for the links (Robot → Health → Network has the numbers). HaLow levels follow the robot's own: weak
   below -77 dBm, 4G fallback at -82 (app/network.py). Wi-Fi is the scan's 0-100 %. */
function sigBars(count, color) {
  return h("span", { class: "sigbars", style: `color:${color}`, "aria-hidden": "true" },
    [1, 2, 3, 4].map((i) => h("i", { class: i <= count ? "on" : "" })));
}
// GPS: the fix type decides (fixed 4, float 2, single/DGPS 1, none 0); a fix with heading DOP at the warn level
// (1.5, docs/dev/robot-states.md) shows 3.
function gpsBars(rtk) {
  const st = rtk && rtk.status;
  if (st === 4) return typeof rtk.heading_dop === "number" && rtk.heading_dop >= 1.5 ? 3 : 4;
  return st === 5 ? 2 : st === 1 || st === 2 ? 1 : 0;
}
/* Corrections late (rtcm_age above 10 s, app/problems.py RTCM_LATE_S): the base station's corrections are not
   arriving, so the fix falls to DGPS whatever the sky. Says so, in amber even on the dock: corrections that were
   flowing and stopped are worth knowing about (none at all, -1, is normal there). */
const correctionsLate = (rtk) => !!rtk && typeof rtk.rtcm_age === "number" && rtk.rtcm_age > 10;
function gpsChip(rtk, activity) {
  const late = correctionsLate(rtk);
  const color = late ? AMBER : activity === "docked" ? MUTED : rtkColor(rtk.status);   // no fix on the dock is normal
  const text = late ? " no corrections" : ` ${rtk.label.replace(/^RTK /, "").toLowerCase()}`;
  return titled(chipLink("#health", null, sigBars(gpsBars(rtk), color), connLabel("RTK", text)),
                `Positioning: ${rtk.label} · ${num(rtk.sats)} satellites · heading DOP ${num(rtk.heading_dop, 2)}` +
                (late ? ` · no RTK corrections for ${dur(rtk.rtcm_age)}: they are not arriving from the base station` : ""));
}
function halowChip(dbm) {
  if (typeof dbm !== "number" || !dbm) return null;
  const n = dbm >= -60 ? 4 : dbm >= -70 ? 3 : dbm > -77 ? 2 : dbm > -82 ? 1 : 0;
  const color = dbm <= -82 ? CORAL : dbm < -77 ? AMBER : GREEN;
  return titled(chipLink("#health", null, sigBars(n, color), connLabel("HaLow", ` ${num(dbm)} dBm`)),
                `HaLow: ${num(dbm)} dBm${dbm <= -82 ? " (the robot switches to 4G here)" : dbm < -77 ? " (weak)" : ""}`);
}
function wifiChip(w) {
  if (!w || !w.connected) return null;                 // not on Wi-Fi, or not asked yet: nothing to show
  const v = w.signal;
  const known = typeof v === "number";
  const n = !known ? 0 : v >= 75 ? 4 : v >= 50 ? 3 : v >= 25 ? 2 : v > 0 ? 1 : 0;
  return titled(chipLink("#health", null, sigBars(n, known ? (v >= 50 ? GREEN : v >= 25 ? AMBER : CORAL) : MUTED),
                         connLabel("Wi-Fi", known ? ` ${num(v)}%` : "")),
                known ? `Wi-Fi: ${num(v)}% (from the latest scan)` : "Wi-Fi: connected; scan in Robot → Health for its signal");
}

/* The battery, always at the far right of the top bar (it stays in view on a phone, where the chips scroll). The icon
   fills with the charge and its colour runs from the theme's bad (empty) through warn (half) to ok (full). */
function hexRgb(c) {
  const m = String(c).trim().match(/^#?([0-9a-f]{6})$/i);
  return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null;
}
function batteryColor(pct) {
  const a = hexRgb(pct < 50 ? CORAL : AMBER), b = hexRgb(pct < 50 ? AMBER : GREEN);
  if (!a || !b) return pct < 20 ? CORAL : pct < 50 ? AMBER : GREEN;
  const t = pct < 50 ? pct / 50 : (pct - 50) / 50;
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(", ")})`;
}
function batteryChip(level) {
  const known = typeof level === "number" && isFinite(level);
  const pct = known ? Math.max(0, Math.min(100, level)) : 0;
  const icon = svg("svg", { class: "batticon", viewBox: "0 0 26 14", "aria-hidden": "true" },
    svg("rect", { class: "battbody", x: 0.75, y: 0.75, width: 21.5, height: 12.5, rx: 3 }),
    svg("rect", { class: "battnub", x: 23, y: 4.5, width: 2.25, height: 5, rx: 1 }),
    known && pct > 0 ? svg("rect", { x: 3, y: 3, width: Math.max(1.5, 17 * pct / 100), height: 8, rx: 1.5, fill: batteryColor(pct) }) : null);
  return h("a", { class: "chip link battchip", href: "#health", title: known ? `Battery ${num(level)}%` : "Battery unknown",
                  "aria-label": known ? `Battery ${num(level)}%` : "Battery unknown" },
    icon, h("b", { class: "mono" }, known ? `${num(level)}%` : "—"));
}

/* -------------------------------------------------------------- charts */
/** series: [{points: [[ts, v]], color, label}]. One chart for the whole panel: the latest value in the header, a value
 *  scale (low, middle, high) with light gridlines, times along the foot, and a hover / touch readout of every series
 *  at that time. Neutral data uses SERIES colours; status colours only where the value is good or bad. */
function chart(title, series, unit = "", digits = 1, height = 90) {
  const live = series.filter((s) => s.points && s.points.length);
  const all = live.flatMap((s) => s.points);
  if (all.length < 2) return h("div", { class: "chart" }, kv(title, "collecting…"));
  const W = 400, H = height, pad = 6;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const sx = (x) => ((x - x0) / Math.max(1, x1 - x0)) * W;
  const sy = (y) => H - pad - ((y - y0) / (y1 - y0)) * (H - 2 * pad);
  const fmt = (v) => `${num(v, digits)}${unit ? ` ${unit}` : ""}`;
  const grid = [y0, (y0 + y1) / 2, y1].map((v) => svg("line", { x1: 0, x2: W, y1: sy(v), y2: sy(v), class: "gridline",
                                                                "vector-effect": "non-scaling-stroke" }));
  const lines = live.filter((s) => s.points.length > 1).map((s) =>
    svg("polyline", { points: s.points.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" "),
                      fill: "none", stroke: s.color, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
  const cross = svg("line", { x1: 0, x2: 0, y1: 0, y2: H, class: "crosshair", "vector-effect": "non-scaling-stroke", visibility: "hidden" });
  const tip = h("div", { class: "charttip", hidden: true });
  const ticks = h("div", { class: "yticks", "aria-hidden": "true" },
    [y1, (y0 + y1) / 2, y0].map((v) => h("span", { style: `top:${(sy(v) / H) * 100}%` }, num(v, digits))));
  // The value of each series at time t: its nearest sample.
  const at = (pts, t) => {
    let lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid; else hi = mid; }
    return Math.abs(pts[lo][0] - t) <= Math.abs(pts[hi][0] - t) ? pts[lo] : pts[hi];
  };
  const plot = h("div", { class: "plot", style: `height:${height}px` },
    svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
                 "aria-label": `${title}: ${fmt(y0)} to ${fmt(y1)}`, style: `height:${height}px` }, grid, lines, cross),
    ticks, tip);
  const show = (e) => {
    const box = plot.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width));
    const t = x0 + f * (x1 - x0);
    cross.setAttribute("x1", f * W); cross.setAttribute("x2", f * W); cross.setAttribute("visibility", "visible");
    const rows = live.map((s) => { const p = at(s.points, t); return h("div", null, h("i", { style: `background:${s.color}` }),
      s.label ? `${s.label} ` : "", h("b", null, fmt(p[1]))); });
    fill(tip, h("div", { class: "muted" }, when(at(live[0].points, t)[0])), rows);
    tip.hidden = false;
    tip.style.left = f > 0.6 ? "" : `${f * 100 + 2}%`;
    tip.style.right = f > 0.6 ? `${(1 - f) * 100 + 2}%` : "";
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; };
  plot.addEventListener("pointermove", show);
  plot.addEventListener("pointerdown", show);
  plot.addEventListener("pointerleave", hide);
  const latest = live.map((s) => s.points[s.points.length - 1][1]);
  const legend = live.length > 1
    ? h("div", { class: "chartlegend" }, live.map((s) => h("span", null, h("i", { style: `background:${s.color}` }), s.label)))
    : null;
  return h("div", { class: "chart" },
    h("div", { class: "kv" }, h("span", { class: "muted" }, title),
      h("span", { class: "mono" }, latest.map((v) => num(v, digits)).join(" · ") + (unit ? ` ${unit}` : ""))),
    plot,
    h("div", { class: "axis" }, h("span", null, when(x0)), h("span", null, when(x1))),
    legend);
}

async function series(metric, hours, weather = false) {
  try {
    return (await getJSON(`/api/${weather ? "weather/" : ""}series?metric=${metric}&hours=${hours}`)).points;
  } catch { return []; }
}

/* --------------------------------------------------------- live view */
let conditionsCache = { at: 0, data: null };
async function conditionsCard() {
  if (Date.now() - conditionsCache.at > 30000) {
    try { conditionsCache = { at: Date.now(), data: await getJSON("/api/conditions") }; } catch { /* keep */ }
  }
  const c = conditionsCache.data;
  if (!c || !c.state) {
    return card("Lawn conditions", h("p", { class: "muted" }, "Waiting for the first weather observation (one a minute, from the Tempest or Home Assistant)."));
  }
  const i = c.inputs;
  const summary = c.raining ? "raining now" : c.dry_by ? `dry by about ${clock(c.dry_by)}` : c.state === "Dry" ? "good to mow" : "";
  return h("div", { class: "card" },
    h("div", { class: "row gap", style: "justify-content:space-between" }, h("p", { class: "label", style: "margin:0" }, "Lawn conditions"),
      h("span", { class: "muted small" }, `Tempest · ${dur(i.obs_age_s)} ago`)),
    h("div", { class: "row gap", style: "align-items:baseline;flex-wrap:wrap;margin-top:6px" },
      h("span", { class: "big", style: `color:${STATE_COLOR[c.state]}` }, c.state),
      h("span", { class: "muted" }, summary)),
    h("ul", { class: "rules" }, c.rules.map((r) => h("li", null, dot(STATE_COLOR[r.state]),
      h("span", null, h("b", null, r.rule), " ", h("span", { class: "muted" }, r.detail))))),
    kv("Temperature · dew point", c.units === "us" ? `${num(i.temp_c == null ? null : i.temp_c * 9 / 5 + 32, 1)} °F · ${num(i.dew_c == null ? null : i.dew_c * 9 / 5 + 32, 1)} °F`
                                                 : `${num(i.temp_c, 1)} °C · ${num(i.dew_c, 1)} °C`),
    kv("Humidity · sun", `${num(i.rh)}% · ${num(i.solar)} W/m²`),
    kv(`Rain last hour · last ${num(c.config?.lookback_h ?? 6, 0)} h`, c.units === "us" ? `${num(i.rain_last_h_mm / 25.4, 2)} · ${num(i.rain_lookback_mm / 25.4, 2)} in`
                                                  : `${num(i.rain_last_h_mm, 2)} · ${num(i.rain_lookback_mm, 1)} mm`),
    i.soil_pct != null ? kv("Soil moisture", `${num(i.soil_pct, 0)} %`) : null);
}

async function renderLive(s) {
  const panel = $("#p-live");
  if (panel.hidden) return;
  const [extras, cond] = await Promise.all([liveExtras(s), conditionsCard()]);
  if (panel.hidden) return;
  fill(panel, extras, state.layers.trouble ? problemsCard() : null, cond);
}

// Plans live on the robot; Live's Control card shows them with their areas (named from the map).
function renderPlans() {
  getJSON("/api/plans").then((d) => {
    state.plansCache = d.plans || [];
    if (state.view === "live" && state.status) renderLive(state.status);
  }).catch(() => { /* the robot has not answered yet */ });
}

/* ----------------------------------------------------------- runs view */
function headName(t) {
  return { 0: "no head", 1: "Snow Blower", 2: "Blower", 3: "Mower", 4: "Smart Cover", 5: "Mower Pro" }[t] || "unknown head";
}

const METRICS = {
  rtk: { label: "RTK fix", short: "RTK fix", field: "rtk_status", categorical: true, expr: rtkColorExpr,
         legend: [["RTK fixed", MAP.fixed], ["Float", MAP.float], ["Single point", MAP.single], ["Other", MUTED]] },
  edge: { label: "Edge passes vs fill", short: "Edges", field: "edge", categorical: true, expr: edgeColorExpr,
          legend: [["Edge passes", EDGE], ["Fill", MAP.fixed], ["Unknown", MUTED]] },
  sats: { label: "Satellites", short: "Satellites", field: "sats", unit: "", digits: 0, invert: true },
  halow: { label: "HaLow signal", short: "HaLow", field: "halow", unit: "dBm", digits: 0, invert: true },
  battery: { label: "Battery", short: "Battery", field: "battery", unit: "%", digits: 0, invert: true },
  blade: { label: "Blade load (current)", short: "Blade load", field: "blade_cur", unit: "A", digits: 1 },
  slope: { label: "Slope (pitch)", short: "Slope", field: "pitch", unit: "°", digits: 1, abs: true },
  speed: { label: "Speed", short: "Speed", field: "speed", unit: "m/s", digits: 2 },
  slip: { label: "Wheel slip (experimental)", short: "Wheel slip", field: "slip", unit: "", digits: 2 },
};
const RAMP = ["#79B8F0", "#8BD49A", "#F0B955", "#F08A6E"];

function runButton(id, title, left, right) {
  return h("button", { type: "button", class: "run", "data-run": id, "aria-pressed": String(String(state.selectedRun) === String(id)),
                       onclick: () => openRun(id) },
    h("span", null, title),
    h("span", { class: "meta" }, h("span", null, left), h("span", null, right)));
}

// Runs: with no run picked, the overview (every run, totals, trends, what keeps coming back) fills the page; a run
// (#runs/<id>) shows its track on the map with the detail beside it.
function showRuns(sub) {
  state.runsSub = sub;
  if (sub) {
    if (String(state.selectedRun) !== sub || !state.run) selectRun(sub);
    else renderRuns();
  } else if (state.run || state.selectedRun !== "recent") selectRun("recent");
  else renderRuns();
}

async function renderRuns() {
  if (state.view !== "runs") return;
  return state.runsSub ? renderRunPanel() : renderRunsOverview();
}

async function renderRunPanel() {
  const panel = $("#p-runs");
  let runs = [];
  try { runs = (await getJSON("/api/runs")).runs; } catch { /* keep empty */ }
  if (state.view !== "runs" || !state.runsSub) return;
  const items = runs.map((r) => {
    const length = r.ended ? dur(r.working_s ?? (r.ended - r.started)) : "running";
    const fixed = r.rtk_fixed_pct === null ? "" : ` · RTK ${num(r.rtk_fixed_pct)}%`;
    return runButton(r.id, [h("b", null, when(r.started)), ` · ${headName(r.head_type)}`],
                     `${r.end_reason || "in progress"} · ${num(r.distance_m)} m${fixed}`, length);
  });
  const shown = state.run && String(state.run.id) === state.runsSub;
  fill(panel,
    h("div", { class: "runnav" }, h("a", { href: "#runs", class: "linkbtn" }, "← All runs")),
    shown ? runDetail() : h("p", { class: "muted pad" }, "Loading the run…"),
    h("div", { class: "runlist" }, h("p", { class: "label" }, "Runs"), items));
}

// On a phone the runs table keeps date, time, distance, coverage and problems; the rest is in the run itself.
const OPT_COLS = new Set(["Head", "Gaps", "Pace", "Battery", "RTK fixed", "Ended"]);
const kmOrM = (m) => (m === null || m === undefined ? "—" : m >= 1000 ? `${num(m / 1000, 1)} km` : `${num(m)} m`);

async function renderRunsOverview() {
  const panel = $("#p-runs");
  const ticket = state.runsTicket = (state.runsTicket || 0) + 1;
  const live = () => ticket === state.runsTicket && state.view === "runs" && !state.runsSub;
  let runs = null;
  try { runs = (await getJSON("/api/runs")).runs; } catch (e) { runs = { error: e.message }; }
  if (!live()) return;
  drawRunsOverview(panel, runs, state.insights);              // the run list at once; the insights fill in
  let d;
  try { d = await getJSON("/api/insights?days=365"); } catch (e) { d = { error: e.message }; }
  if (!live()) return;
  state.insights = d;
  drawRunsOverview(panel, runs, d);
}

function drawRunsOverview(panel, runs, d) {
  const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  const cell = (v, suffix = "", digits = 0) => (v === null || v === undefined ? "—" : `${num(v, digits)}${suffix}`);
  const ready = d && !d.error;
  const mows = ready ? d.mows : [], t = ready ? d.totals : null, few = mows.length < 2;
  const byId = Object.fromEntries(mows.map((m) => [String(m.id), m]));
  const list = Array.isArray(runs) ? runs : [];
  const pts = (key) => mows.filter((m) => m[key] !== null && m[key] !== undefined).map((m) => [m.started, m[key]]);
  const row = (r) => {
    const m = byId[String(r.id)], used = r.battery_start !== null && r.battery_end !== null ? r.battery_start - r.battery_end : null;
    return h("tr", { class: `click${m ? "" : " dim"}`, title: m ? "Open this run" : `Open this run (under ${ready ? d.min_run_m : 50} m, so not counted as a mow)`,
                     tabindex: "0", onclick: () => openRun(r.id), onkeydown: (e) => { if (e.key === "Enter") openRun(r.id); } },
      h("td", null, `${day(r.started)} ${clock(r.started)}`),
      h("td", { class: "opt" }, headName(r.head_type)),
      h("td", { class: "num", title: pausedFor(r) ? `${dur(r.ended - r.started)} start to end, ${pausedFor(r)} of it paused or charging` : null },
        r.ended ? dur(r.working_s ?? (r.ended - r.started)) : "running"),
      h("td", { class: "num" }, kmOrM(r.distance_m)),
      h("td", { class: "num" }, m ? cell(m.coverage_pct, "%") : "—"),
      h("td", { class: "num opt" }, m && m.gaps_m2 !== null ? `${num(m.gaps_m2, 1)} m² (${m.gap_count})` : "—"),
      h("td", { class: "num opt" }, m ? cell(m.m2_per_hour, " m²/h") : "—"),
      h("td", { class: "num opt" }, cell(m ? m.battery_used : used, "%")),
      h("td", { class: "num opt" }, cell(r.rtk_fixed_pct, "%")),
      h("td", { class: "narrowopt" }, m ? (m.problem_spots ? `${m.problem_spots}${m.stuck_spots ? ` (${m.stuck_spots} stuck)` : ""}` : "none") : "—"),
      h("td", { class: "muted opt" }, r.end_reason || (r.ended ? "—" : "in progress")));
  };
  const waiting = h("p", { class: "muted small" }, d && d.error ? `Unavailable: ${d.error}` : "Adding up the mows… (the first visit works out each run's coverage once)");
  fill(panel,
    h("div", { class: "healthbar" },
      h("p", { class: "muted small", style: "margin:0" },
        ready ? [`${t.mows} mow${t.mows === 1 ? "" : "s"} in the last year`, few ? " · trends appear from the second mow" : ""]
              : `${list.length} run${list.length === 1 ? "" : "s"} recorded`,
        " · a run is one plan; pauses and charging breaks stay inside it")),
    h("div", { class: "card", style: "margin:12px 12px 0" },
      h("p", { class: "label" }, "Every run"),
      runs && runs.error ? h("p", { class: "small", style: `color:${AMBER}` }, `Could not load the runs: ${runs.error}`)
      : !list.length ? h("p", { class: "muted" }, "No runs yet. A run starts when the wheels turn or the robot reports a plan, so the first mow or snow run will appear here with its track.")
      : h("div", { class: "tablewrap" }, h("table", { class: "tbl" },
          h("thead", null, h("tr", null, ["Date", "Head", "Time", "Distance", "Coverage", "Gaps", "Pace", "Battery", "RTK fixed", "Problems", "Ended"]
            .map((x) => h("th", { class: OPT_COLS.has(x) ? "opt" : x === "Problems" ? "narrowopt" : null }, x)))),
          h("tbody", null, list.map(row))))),
    !ready ? h("div", { class: "healthgrid" }, card("Totals", waiting))
    : !mows.length ? h("div", { class: "healthgrid" }, card("Totals",
        h("p", { class: "muted" }, `No mows yet. A run of at least ${d.min_run_m} m counts as a mow; totals and trends start with the first one.`)))
    : h("div", { class: "healthgrid" },
      h("div", { class: "card" },
        h("p", { class: "label" }, "Totals"),
        h("div", { class: "stats" },
          stat("Mows", String(t.mows), t.days_between ? `every ${num(t.days_between, 1)} days` : null),
          stat("Last mow", day(t.last_mow), `${num(t.days_since, 1)} days ago`),
          stat("Time mowing", `${num(t.hours, 1)} h`),
          stat("Distance", `${num(t.distance_km, 1)} km`),
          stat("Area mowed", `${num(t.covered_m2)} m²`, "summed over mows"),
          stat("Coverage", cell(t.coverage_pct, "%"), "average, of the known area"),
          stat("Pace", cell(t.m2_per_hour, " m²/h"), "average"),
          stat("Energy", cell(t.energy_per_100m2, "%", 2), "battery per 100 m²"),
          stat("RTK fixed", cell(t.rtk_fixed_pct, "%"), "average"),
          stat("Stuck spots", String(t.stuck_spots), "summed over mows"))),
      card("Trends",
        ...(few ? [h("p", { class: "muted small" }, "One mow so far; the lines start with the next one.")] : [
        chart("Coverage of the known area", [{ points: pts("coverage_pct"), color: GREEN }], "%", 0),
        chart("Pace", [{ points: pts("m2_per_hour"), color: SERIES[0] }], "m²/h", 0),
        chart("Battery per 100 m²", [{ points: pts("energy_per_100m2"), color: SERIES[1] }], "%", 2),
        chart("RTK fixed", [{ points: pts("rtk_fixed_pct"), color: GREEN }], "%", 0),
        chart("Problem spots", [{ points: pts("problem_spots"), color: CORAL, label: "all" },
                                { points: pts("stuck_spots"), color: PROBLEM.stuck.color, label: "stuck" }], "", 0)])),
      h("div", { class: "card" },
        h("p", { class: "label" }, "Keeps coming back ",
          h("span", { class: "tip", tabindex: "0", title: "Problem spots seen in two or more mows: the places worth fixing on the ground (a rut, a slope, a corner the robot cannot reach)." }, "ⓘ")),
        d.recurring.length
          ? h("ul", { class: "gaplist" }, d.recurring.map((p) => h("li", null,
              h("button", { type: "button", class: "btn slim", disabled: p.lon === null || null, onclick: () => showOnLiveMap(p) }, "Show"),
              h("span", null, h("b", { style: `color:${PROBLEM[p.cause].color}` }, PROBLEM[p.cause].label),
                h("span", { class: "muted" }, ` · in ${p.runs} mows`), h("br"), h("span", { class: "small" }, p.evidence)))))
          : h("p", { class: "muted small" }, few ? "Needs at least two mows to tell a one-off from a pattern." : "Nothing has come back so far."))));
}

// A run's length is its working time; a long break (a mow paused overnight, a charging stop) shows underneath (#62).
function pausedFor(r) {
  if (!r.ended || r.working_s === null || r.working_s === undefined) return null;
  const gap = r.ended - r.started - r.working_s;
  return gap >= 1800 ? dur(gap) : null;
}
function runDuration(info) {
  if (!info.ended) return stat("Duration", "running");
  const p = pausedFor(info);
  return stat("Duration", dur(info.working_s ?? (info.ended - info.started)),
              p ? `working · ${dur(info.ended - info.started)} start to end, ${p} paused or charging` : null);
}

function stat(label, value, sub) {
  if (value === "—") return null;              // nothing to say yet (a short run, coverage still being worked out)
  return h("div", { class: "stat" }, h("span", { class: "muted small" }, label), h("b", { class: "mono" }, value),
           sub ? h("span", { class: "muted tiny" }, sub) : null);
}

function replayText(row, idx) {
  if (!row) return "";
  const parts = [clock(row[idx.ts]), `RTK ${row[idx.rtk_status] ?? "—"}`, `sats ${row[idx.sats] ?? "—"}`,
                 `battery ${num(row[idx.battery])}%`, `HaLow ${num(row[idx.halow])} dBm`];
  if (row[idx.blade_cur] !== null && row[idx.blade_cur] !== undefined) parts.push(`blade ${num(row[idx.blade_cur], 1)} A`);
  return parts.join(" · ");
}

/** One mow recorded as two runs (a resume before plan tracking, or a long break): offer to fold it into the run
 * before it. Shown only when that run ended less than 3 h earlier, or carries the same plan session. */
function joinBar() {
  const j = state.run.join;
  if (!j || !(j.same_plan || j.gap_s < 3 * 3600)) return null;
  return h("div", { class: "row gap small", style: "align-items:center;margin:6px 0" },
    h("span", { class: "muted" }, j.same_plan ? "Same plan as the run before it." : `${dur(j.gap_s)} after the run before it.`),
    h("button", { type: "button", class: "btn slim",
      title: "Treat both as one run: the tracks, distance and samples are combined and coverage is worked out again.",
      onclick: async (e) => {
        if (!confirm("Join this run onto the run before it? This cannot be undone.")) return;
        e.target.disabled = true;
        try {
          await postJSON(`/api/runs/${state.run.id}/join-previous`, {});
          toast("Joined.");
          openRun(j.id, true);
        } catch (err) { toast(err.message); e.target.disabled = false; }
      } }, "Join with previous"));
}

function runDetail() {
  const { info, coverage: cov, rows, fields } = state.run;
  const idx = Object.fromEntries(fields.map((f, i) => [f, i]));
  const areas = (cov && cov.areas) || [];
  const covered = areas.reduce((s, a) => s + a.covered_m2, 0);
  const unknown = areas.reduce((s, a) => s + a.unknown_m2, 0);
  const gaps = areas.flatMap((a) => a.gaps.map((g) => ({ ...g, area: a.name })));
  const known = areas.reduce((s, a) => s + a.covered_m2 + (a.uncovered_m2 ?? a.gap_m2), 0);
  const used = info.battery_start !== null && info.battery_end !== null ? info.battery_start - info.battery_end : null;
  const replay = rows[Math.min(state.replayIndex, rows.length - 1)];
  return h("div", { class: "card rundetail" },
    h("div", { class: "row gap", style: "justify-content:space-between;flex-wrap:wrap" },
      h("p", { class: "label", style: "margin:0" }, `Run · ${when(info.started)}`),
      h("span", { class: "muted small" }, `${headName(info.head_type)} · ${info.end_reason || "in progress"}`)),
    state.run.endWhy ? whyList([state.run.endWhy]) : null,
    joinBar(),
    h("div", { class: "stats" },
      runDuration(info),
      stat("Distance", `${num(info.distance_m)} m`),
      stat("RTK fixed", `${num(info.rtk_fixed_pct)}%`),
      stat("Coverage", known ? `${num((100 * covered) / known)}%` : "—", "of the known area"),
      stat("Unknown", `${num(unknown)} m²`, "no RTK fix"),
      stat("Gaps", gaps.length ? `${gaps.length} · ${num(gaps.reduce((s, g) => s + g.area_m2, 0), 1)} m²` : "none"),
      stat("Battery used", used !== null ? `${num(used)}%` : "—"),
      stat("Energy", used !== null && covered ? `${num((100 * used) / covered, 2)}%` : "—", "per 100 m²"),
      stat("Problem spots", info && state.run.problems && !state.run.problems.error ? String(state.run.problems.spots.length) : "—",
           state.run.problems && !state.run.problems.error
             ? `${state.run.problems.spots.filter((p) => p.cause === "stuck").length} stuck` : "")),
    h("div", { class: "replay" },
      playButton(),
      h("input", { type: "range", id: "scrub", min: "0", max: String(Math.max(0, rows.length - 1)), value: String(state.replayIndex),
                   "aria-label": "Replay position", oninput: (e) => { state.replayOn = true; state.replayT = null; setReplay(Number(e.target.value)); } }),
      h("label", { class: "replayspeed",
                    title: "Replay speed, times real time. Stretches where the robot stands still go by quickly. Whole run plays it in about 30 s." },
        "Speed",
        h("select", { id: "replayspeed", class: "field slim", "aria-label": "Replay speed",
                      onchange: (e) => { state.replaySpeed = Number(e.target.value); writePref("replaySpeed", state.replaySpeed);
                                         if (state.replayTimer) { stopPlay(); togglePlay(); } } },
          [[1, "1×"], [10, "10×"], [60, "60×"], [300, "300×"], [0, "Whole run"]].map(([v, t]) =>
            h("option", { value: String(v), selected: v === state.replaySpeed || null }, t))))),
    h("p", { class: "mono small", id: "replayinfo" }, replayText(replay, idx)),
    areas.length ? h("div", null, h("p", { class: "label", style: "margin-top:10px" }, "Coverage by area"),
      areas.map((a) => kv(a.name, `${num(a.coverage_pct_of_known)}% · ${num(a.covered_m2)} m² · gaps ${num(a.gap_m2, 1)} m²`)),
      cov.earlier_runs && cov.earlier_runs.length && cov.covered_before_m2
        ? h("p", { class: "muted small" }, `Same plan as run ${cov.earlier_runs.join(", ")}: the ${num(cov.covered_before_m2)} m² `
            + "mowed there count as done, so they are neither gaps nor in this run's share.") : null) : null,
    ...runProblems(),
    plannedSection(),
    h("p", { class: "label", style: "margin-top:10px" }, "Gaps (fixed-RTK coverage only)"),
    gaps.length
      ? h("ul", { class: "gaplist" }, gaps.map((g) => h("li", null,
          h("button", { type: "button", class: "btn slim", onclick: () => state.map.flyTo({ center: g.centroid_lonlat, zoom: 21 }) }, "Show"),
          h("span", null, h("b", null, `${num(g.area_m2, 1)} m²`), ` in ${g.area}`, h("span", { class: "muted" }, ` · ${g.cause}`)))))
      : h("p", { class: "muted small" }, cov ? "No gaps over 1 m² where the position was known." : "Coverage is being computed…"),
    h("p", { class: "muted small" }, `Coverage uses a ${num(cov ? cov.deck_m : 0.53, 2)} m deck on a ${num(cov ? cov.cell_m : 0.25, 2)} m grid; stretches without an RTK fix count as unknown, never as gaps.`));
}

async function selectRun(id) {
  stopPlay();
  state.selectedRun = id;
  setSource("planned", EMPTY);
  setSource("offplan", EMPTY);
  if (id === "recent") {
    state.run = null;
    setSource("gaps", EMPTY);
    setSource("swath", EMPTY);
    setSource("replay", EMPTY);
    renderColourControl();
    renderTrackLegend();
    await showRecentTrack({ redraw: true });
    renderRuns();
    robot3dSync();
    if (state.layers.trouble) loadProblems();
    return;
  }
  const [detail, pts] = await Promise.all([getJSON(`/api/runs/${encodeURIComponent(id)}`),
                                           getJSON(`/api/runs/${encodeURIComponent(id)}/points`)]);
  if (String(state.selectedRun) !== String(id)) return;      // another run (or the last 24 h) was picked meanwhile
  if (state.layers.trouble) setTimeout(loadProblems, 0);
  if (state.metric === "rtk" || state.metric === "edge") state.metric = state.trackColor;   // follow the map's colour switch
  state.run = { id, info: detail.run, endWhy: detail.end_why, join: detail.join_previous, fields: pts.fields, rows: pts.rows, coverage: null, range: null };
  state.replayIndex = 0;
  state.replayOn = false;
  renderColourControl();
  drawRunTrack();
  robot3dSync();
  if (state.map) {                       // a run link opened cold: the map comes up after this and draws the run itself
    state.map.resize();                    // the map was hidden behind the overview a moment ago
    fitTo(pts.rows.map((r) => [r[1], r[2]]));
  }
  renderRuns();
  const run = state.run;
  getJSON(`/api/runs/${encodeURIComponent(id)}/planned`)              // a comparison made before, if any
    .then((d) => { run.planned = d; if (state.run === run) renderRuns(); })
    .catch(() => { /* not compared yet */ });
  getJSON(`/api/problems?run_id=${encodeURIComponent(id)}`)          // alongside the coverage, not after it
    .then((d) => { run.problems = d; })
    .catch((e) => { run.problems = { error: e.message }; })
    .finally(() => { if (state.run === run) renderRuns(); });
  try {
    state.run.coverage = await getJSON(`/api/runs/${encodeURIComponent(id)}/coverage`);
    drawCoverage();
    renderRuns();
  } catch (e) { toast(`Coverage unavailable: ${e.message}`); }
}

/** Show one problem spot: switch the Problem areas layer on (scoped to this run) and fly there. */
function showProblemSpot(p) {
  if (!state.layers.trouble) $('[data-layer="trouble"]').click();
  state.map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(state.map.getZoom(), 20.5) });
}

/* Planned vs driven: the robot plans the run's areas (its route preview; it only computes) and the track it drove is
   compared with that plan: how much follows it, the stretches off it, how much of the plan was driven. */
async function comparePlanned() {
  const run = state.run;
  run.plannedBusy = true;
  renderRuns();
  try {
    run.planned = await postJSON(`/api/runs/${encodeURIComponent(run.id)}/planned`, {});
    run.plannedShown = true;
    if (state.run === run) drawPlanned();
  } catch (e) { toast(`No comparison: ${e.message}`); }
  run.plannedBusy = false;
  if (state.run === run) renderRuns();
}

function drawPlanned() {
  const p = state.run && state.run.planned;
  const on = !!(p && state.run.plannedShown);
  setSource("planned", on ? p.route : EMPTY);
  setSource("offplan", on ? p.off_plan : EMPTY);
}

function plannedSection() {
  const run = state.run, p = run.planned;
  const head = h("p", { class: "label", style: "margin-top:10px" }, "Planned vs driven ",
    h("span", { class: "tip", tabindex: "0", title: "The robot plans this run's areas (the app's route preview: it only computes) and the mowing part of the track is compared with that plan. Driving between areas and samples without an RTK fix are left out." }, "ⓘ"));
  const ask = btn(run.plannedBusy ? "Asking the robot to plan it…" : p ? "Compare again" : "Compare with the robot's plan",
                  comparePlanned, "btn slim", { disabled: run.plannedBusy || null, title: "About 10 s per area" });
  if (!p) return [head, h("div", { class: "row gap" }, ask)];
  const stretches = [...(p.off_plan.features || [])].sort((a, b) => b.properties.length_m - a.properties.length_m);
  const show = () => { run.plannedShown = !run.plannedShown; drawPlanned(); renderRuns(); };
  return [head,
    h("p", { class: "small", style: "margin:0 0 4px" },
      h("b", null, `${num(p.on_plan_pct)}%`), " of the mowing track within 0.5 m of the plan · ",
      h("b", null, `${stretches.length}`), ` stretch${stretches.length === 1 ? "" : "es"} off it (`, colored(OFFPLAN, "pink"), " on the map) · ",
      h("b", null, `${num(p.plan_driven_pct)}%`), " of the plan driven"),
    h("p", { class: "muted small", style: "margin:0 0 6px" },
      `Planned ${num(p.planned_length_m / 1000, 1)} km${p.planned_time_s ? `, about ${dur(p.planned_time_s)}` : ""} · driven ${num((p.driven_m || 0) / 1000, 1)} km`
        + (p.done_before_m ? ` · ${num(p.done_before_m / 1000, 1)} km of the plan done before this run` : ""),
      p.duration_s ? ` in ${dur(p.duration_s)}` : "", ` · ${p.basis}`,
      p.changed_since && p.changed_since.length ? colored(AMBER, ` (changed since the run: ${p.changed_since.join(", ")})`) : ""),
    h("div", { class: "row gap", style: "margin-bottom:6px" },
      btn(run.plannedShown ? "Hide on the map" : "Show on the map", show, "btn slim"), ask),
    stretches.length ? h("ul", { class: "gaplist" }, stretches.slice(0, 5).map((f) => h("li", null,
      btn("Show", () => { if (!run.plannedShown) show(); fitTo(f.geometry.coordinates); }, "btn slim"),
      h("span", null, h("b", null, `${num(f.properties.length_m, 1)} m`),
        f.properties.kind === "across" ? " across the plan's lines" : ` off the plan, up to ${num(f.properties.max_off_m, 1)} m away`,
        h("span", { class: "muted" }, ` · ${clock(f.properties.from)}`))))) : null];
}

function runProblems() {
  const pr = state.run.problems;
  const head = h("p", { class: "label", style: "margin-top:10px" }, "Problems in this run ",
    h("span", { class: "tip", tabindex: "0", title: "Where the robot got stuck, hit an error, drove steep ground, had weak positioning or worked hard. The Problem areas map tool shows them." }, "ⓘ"));
  if (!pr) return [head, h("p", { class: "muted small" }, "Working them out…")];
  if (pr.error) return [head, h("p", { class: "small", style: `color:${AMBER}` }, `Could not load them: ${pr.error}`)];
  if (!pr.spots.length) return [head, h("p", { class: "muted small" }, "Nothing stood out.")];
  const counts = {};
  pr.spots.forEach((p) => { counts[p.cause] = (counts[p.cause] || 0) + 1; });
  const unplaced = pr.events.filter((e) => e.kind === "error" && !e.place);
  return [head,
    h("div", { class: "row gap wrap small", style: "margin-bottom:6px" }, Object.entries(PROBLEM).filter(([k]) => counts[k]).map(([k, v]) =>
      h("span", { class: "row gap", style: "align-items:center" }, h("i", { class: "sw", style: `background:${v.color};width:10px;height:10px;border-radius:2px` }), `${v.label} ${counts[k]}`))),
    h("ul", { class: "gaplist" }, pr.spots.slice(0, 8).map((p) => h("li", null,
      h("button", { type: "button", class: "btn slim", onclick: () => showProblemSpot(p) }, "Show"),
      h("span", null, h("b", { style: `color:${PROBLEM[p.cause].color}` }, PROBLEM[p.cause].label),
        p.cells > 1 ? h("span", { class: "muted" }, ` · ${p.area_m2} m²`) : null, h("br"),
        h("span", { class: "small" }, p.evidence),
        Object.keys(p.also || {}).length ? h("span", { class: "small muted" }, ` · also ${Object.values(p.also).join(" · ")}`) : null)))),
    pr.spots.length > 8 ? h("p", { class: "muted small" }, `${pr.spots.length - 8} more on the map.`) : null,
    unplaced.length ? h("p", { class: "muted small" }, "Not tied to a place: ", unplaced.map((e) => `${e.what} (${clock(e.ts)})`).join(" · ")) : null];
}

function drawRunTrack() {
  const run = state.run;
  if (!run) return;
  const idx = Object.fromEntries(run.fields.map((f, i) => [f, i]));
  const m = METRICS[state.metric];
  const val = (r) => {
    const v = r[idx[m.field]];
    return v === null || v === undefined ? null : m.abs ? Math.abs(v) : v;
  };
  const vals = run.rows.map(val).filter((v) => v !== null);
  const min = vals.length ? Math.min(...vals) : 0, max = vals.length ? Math.max(...vals) : 1;
  run.range = { min, max };
  // A ramp metric is drawn in 20 steps, so the track is a few dozen pieces of one colour (a replay redraws it often).
  const colour = (v) => m.categorical ? v
    : v === null ? -1 : Math.round(((m.invert ? max - v : v - min) / Math.max(1e-9, max - min)) * 20) / 20;
  run.pieces = trackPieces(run.rows, idx.ts, (r) => colour(val(r)));
  paint("track-line", "line-color", m.categorical ? m.expr()
    : ["case", ["<", ["get", "v"], 0], MUTED, ["interpolate", ["linear"], ["get", "v"], 0, RAMP[0], 0.33, RAMP[1], 0.66, RAMP[2], 1, RAMP[3]]]);
  // The swath: the same path drawn at the deck's width in metres.
  const lat = run.rows.length ? run.rows[0][2] : 40;
  const deck = (run.coverage && run.coverage.deck_m) || 0.53;
  const mpp22 = (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** 22;
  paint("swath-line", "line-width",
    ["interpolate", ["exponential", 2], ["zoom"], 15, deck / mpp22 / 128, 22, deck / mpp22]);
  setReplay(state.replayIndex);
  drawReplayTrack();
  renderTrackLegend();
}

// The track as pieces of one colour, broken where samples are more than TRACK_GAP_S apart; each keeps the row index
// of every point, so the part driven up to a replay point is quick to cut out.
function trackPieces(rows, tsAt, colourOf) {
  const pieces = [];
  let cur = null;
  for (let i = 1; i < rows.length; i++) {
    const a = rows[i - 1], b = rows[i];
    if (b[tsAt] - a[tsAt] > TRACK_GAP_S) { cur = null; continue; }
    const v = colourOf(b);
    if (!cur || cur.v !== v) { cur = { v, idx: [i - 1], coords: [[a[1], a[2]]] }; pieces.push(cur); }
    cur.idx.push(i);
    cur.coords.push([b[1], b[2]]);
  }
  return pieces;
}

function piecesUpTo(pieces, last) {
  const out = [];
  for (const p of pieces) {
    if (p.idx[0] >= last) break;                        // in row order: the rest start later
    const n = p.idx[p.idx.length - 1] <= last ? p.coords.length : p.idx.findIndex((i) => i > last);
    if (n >= 2) out.push({ type: "Feature", properties: { v: p.v }, geometry: { type: "LineString", coordinates: p.coords.slice(0, n) } });
  }
  return out;
}

// The run's track and swath: whole, or once a replay has started only as far as it has got.
function drawReplayTrack() {
  const run = state.run;
  if (!run || !run.pieces) return;
  const feats = piecesUpTo(run.pieces, state.replayOn ? state.replayIndex : Infinity);
  setSource("track", { type: "FeatureCollection", features: feats });
  setSource("swath", { type: "FeatureCollection", features: feats.map((f) => ({ ...f, properties: {} })) });
}

let replayDrawQueued = false;
function queueReplayTrack() {
  if (replayDrawQueued) return;
  replayDrawQueued = true;
  requestAnimationFrame(() => { replayDrawQueued = false; drawReplayTrack(); });
}

function playButton() {
  const playing = !!state.replayTimer;
  return h("button", { type: "button", class: "btn pri playbtn", id: "playbtn", "aria-label": playing ? "Pause replay" : "Play replay",
                       onclick: togglePlay },
    svg("svg", { viewBox: "0 0 24 24", width: "16", height: "16", "aria-hidden": "true" },
      svg("path", { fill: "currentColor", d: playing ? "M7 5h4v14H7zM13 5h4v14h-4z" : "M8 5v14l11-7z" })),
    playing ? "Pause" : "Play");
}

function drawCoverage() {
  const cov = state.run && state.run.coverage;
  const feats = [];
  for (const a of (cov && cov.areas) || []) {
    for (const g of a.gaps) {
      const ring = [...g.hull_lonlat, g.hull_lonlat[0]];
      feats.push({ type: "Feature", properties: { m2: g.area_m2 }, geometry: { type: "Polygon", coordinates: [ring] } });
    }
  }
  setSource("gaps", { type: "FeatureCollection", features: feats });
  drawRunTrack();
}

// The replay point: row i, or `frac` of the way on to the next row (a replay at a real speed moves between samples).
function setReplay(i, frac = 0) {
  const run = state.run;
  if (!run || !run.rows.length) return;
  state.replayIndex = Math.max(0, Math.min(i, run.rows.length - 1));
  const r = run.rows[state.replayIndex], n = frac > 0 ? run.rows[state.replayIndex + 1] : null;
  setSource("replay", { type: "FeatureCollection", features: [{ type: "Feature", properties: {},
    geometry: { type: "Point", coordinates: n ? [r[1] + (n[1] - r[1]) * frac, r[2] + (n[2] - r[2]) * frac] : [r[1], r[2]] } }] });
  robot3dReplay(r, n, frac);
  const idx = Object.fromEntries(run.fields.map((f, k) => [f, k]));
  const info = $("#replayinfo");
  if (info) info.textContent = replayText(r, idx);
  const scrub = $("#scrub");
  if (scrub && Number(scrub.value) !== state.replayIndex) scrub.value = String(state.replayIndex);
  if (state.replayOn) queueReplayTrack();
}

function togglePlay() {
  if (state.replayTimer) { stopPlay(); renderRuns(); return; }
  if (!state.run) return;
  if (!state.replayOn || state.replayIndex >= state.run.rows.length - 1) setReplay(0);     // a replay starts at the start
  state.replayOn = true;
  queueReplayTrack();
  state.replayT = null;                                  // picks up from the replay row
  const speed = state.replaySpeed;
  if (!speed) {                                          // the whole run in about 30 s
    const step = Math.max(1, Math.round((state.run?.rows.length || 0) / 600));
    state.replayTimer = setInterval(() => {
      if (!state.run || state.replayIndex >= state.run.rows.length - 1) { stopPlay(); renderRuns(); return; }
      setReplay(state.replayIndex + step);
    }, 50);
  } else {
    // Robot time runs at `speed`. Where the robot stands still (parked, charging) or the samples stop (a break in the
    // track) it runs far faster, and the robot waits at the last sample instead of sliding across the break.
    const spAt = state.run.fields.indexOf("speed");
    let last = performance.now();
    state.replayTimer = setInterval(() => {
      const rows = state.run && state.run.rows;
      if (!rows || state.replayIndex >= rows.length - 1) { stopPlay(); renderRuns(); return; }
      const now = performance.now(), dt = Math.min(1, (now - last) / 1000);      // a slow page keeps the pace
      last = now;
      let i = state.replayIndex, left = dt;
      if (state.replayT === null || state.replayT < rows[i][0]) state.replayT = rows[i][0];
      // Spend the tick sample by sample, each at its own rate, so a hurried stretch ends where the robot moves again.
      while (left > 0 && i < rows.length - 1) {
        const span = rows[i + 1][0] - rows[i][0];
        const rate = span > TRACK_GAP_S || !(Math.abs(rows[i][spAt] || 0) > 0.03) ? Math.max(speed * 20, 600) : speed;
        const need = (rows[i + 1][0] - state.replayT) / rate;
        if (need > left) { state.replayT += left * rate; break; }
        left -= Math.max(0, need);
        state.replayT = rows[++i][0];
      }
      const n = rows[i + 1];
      const frac = n && n[0] - rows[i][0] <= TRACK_GAP_S ? (state.replayT - rows[i][0]) / Math.max(1e-6, n[0] - rows[i][0]) : 0;
      setReplay(i, Math.max(0, Math.min(1, frac)));
    }, 50);
  }
  $("#playbtn")?.replaceWith(playButton());
}

function stopPlay() {
  clearInterval(state.replayTimer);
  state.replayTimer = null;
}

/** [ts, lon, lat, rtk, edge] rows -> LineStrings of constant colour (RTK status, or edge vs fill), broken where
 * samples are > TRACK_GAP_S apart. */
function trackFeatures(rows, col = state.trackColor === "edge" ? 4 : 3) {
  const feats = [];
  let line = [], rtk = null;
  const flush = () => {
    if (line.length >= 2) feats.push({ type: "Feature", properties: { v: rtk }, geometry: { type: "LineString", coordinates: line } });
  };
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i], prev = rows[i - 1];
    if (prev && r[0] - prev[0] > TRACK_GAP_S) { flush(); line = []; }
    else if (prev && r[col] !== rtk) { line.push([r[1], r[2]]); flush(); line = []; }   // colour changes at r
    line.push([r[1], r[2]]);
    rtk = r[col];
  }
  flush();
  return { type: "FeatureCollection", features: feats };
}

/** The last 24 h on the live map. Keeps what it already has and asks only for newer points, and redraws only when
 * something changed. full: start over (first load, after the stream dropped); redraw: draw even if nothing is new
 * (coming back from a selected run). A request made while one is in flight is remembered, not lost. */
async function showRecentTrack({ full = false, redraw = false } = {}) {
  const L = state.live;
  if (full) L.resetPending = true;
  if (redraw) L.redrawPending = true;
  if (L.busy) return;
  L.busy = true;
  const reset = L.resetPending;
  L.resetPending = false;
  try {
    if (reset) { L.rows = []; L.last = 0; }
    const d = await getJSON(`/api/track/points?since=${L.last}&hours=24`);
    relabel(L.rows, d.relabel, 4);
    for (const r of d.rows) L.rows.push(r);
    if (d.rows.length) L.last = d.rows[d.rows.length - 1][0];
    const cutoff = Date.now() / 1000 - 24 * 3600;
    const keep = L.rows.findIndex((r) => r[0] >= cutoff);
    const trimmed = keep !== 0 && L.rows.length > 0;
    if (keep === -1) L.rows.length = 0; else if (keep > 0) L.rows.splice(0, keep);
    if ((reset || d.rows.length || trimmed || L.redrawPending) && !state.run && state.selectedRun === "recent") {
      L.redrawPending = false;
      setSource("track", trackFeatures(L.rows));
      paint("track-line", "line-color", liveTrackExpr());
    }
  } catch (e) {
    if (reset) L.resetPending = true;
    console.warn("live track update failed; retrying on the next status", e);
  } finally {
    L.busy = false;
  }
}

/** Replace the provisional edge labels at the end of `rows` with the server's final ones ([[ts, edge]...]). */
function relabel(rows, fixes, col) {
  if (!fixes || !fixes.length || col < 0) return;
  const byTs = new Map(fixes);
  for (let i = rows.length - 1; i >= Math.max(0, rows.length - 64); i--) {
    if (byTs.has(rows[i][0])) rows[i][col] = byTs.get(rows[i][0]);
  }
}

/** A selected run that is still recording keeps growing on the map too. */
async function growSelectedRun() {
  const R = state.runLive, run = state.run;
  if (R.busy || !run) return;
  R.busy = true;
  try {
    const last = run.rows.length ? run.rows[run.rows.length - 1][0] : 0;
    const d = await getJSON(`/api/runs/${encodeURIComponent(run.id)}/points?since=${last}`);
    if (state.run === run && d.rows.length) {
      relabel(run.rows, d.relabel, run.fields.indexOf("edge"));
      for (const r of d.rows) run.rows.push(r);
      drawRunTrack();
      const scrub = $("#scrub");
      if (scrub) scrub.max = String(run.rows.length - 1);
    }
  } catch (e) {
    // 404: the recorder folded this run into the one before it (same plan session) - follow it.
    const live = state.status && state.status.run;
    if (e.status === 404 && state.run === run && live && String(live.id) !== String(run.id)) openRun(live.id, true);
    else console.warn("run track update failed", e);
  } finally {
    R.busy = false;
  }
}

/* --------------------------------------------------------- health view */
function openRun(id, replace = false) {
  const url = `#runs/${encodeURIComponent(id)}`;
  if (replace) location.replace(url); else location.hash = url;
}

/** Jump to the live map with the Problem areas layer on, at this spot. */
function showOnLiveMap(p) {
  location.hash = "#live";
  if (state.run) selectRun("recent");
  if (!state.layers.trouble) $('[data-layer="trouble"]').click();
  setTimeout(() => state.map.flyTo({ center: [p.lon, p.lat], zoom: 20.5 }), 300);
}

async function renderHealth() {
  const panel = $("#p-health");
  const hours = state.healthHours;
  fill(panel, h("p", { class: "muted pad" }, "Loading…"));
  const [hv, sats, dop, halow, battery, volt, cellmax, bladeL, bladeR, cpu] = await Promise.all([
    getJSON(`/api/health?hours=${hours}`).catch(() => null),
    series("sats", hours), series("heading_dop", hours), series("halow", hours), series("battery", hours),
    series("voltage_mv", hours), series("cell_temp_max", hours), series("blade_temp_l", hours), series("blade_temp_r", hours),
    series("cpu_temp", hours),
  ]);
  const s = state.status, r = s && s.robot;
  const temps = (r && r.cell_temps) || [], tmax = Math.max(30, ...temps);
  const cells = temps.length ? h("div", { class: "cells", role: "img", "aria-label": "Battery cell temperatures now" },
    temps.map((t) => h("div", { class: "cell" }, h("div", { style: `height:${Math.max(6, (t / tmax) * 48)}px` }), `${num(t)}°`))) : null;
  const rtcm = !r || r.rtk.rtcm_age === null || r.rtk.rtcm_age === undefined || r.rtk.rtcm_age < 0 ? "none received" : `${num(r.rtk.rtcm_age, 1)} s`;
  const shareColor = (k) => (k === "RTK fixed" ? MAP.fixed : k === "RTK float" ? MAP.float : MAP.single);
  const shareRows = hv ? Object.entries(hv.rtk_share_pct).map(([k, v]) =>
    h("div", { class: "sharerow" }, h("span", null, k),
      h("div", { class: "bar" }, h("div", { class: "fillbar", style: `width:${v}%;background:${shareColor(k)}` })), mono(`${num(v)}%`))) : [];
  const fwHistory = hv ? hv.firmware_history.slice(0, 12).map((f) => kv(f.component.replace("_", " "), `${f.version} · ${when(f.ts)}`)) : [];
  const codes = hv ? hv.error_codes_seen : [];
  fill(panel,
    h("div", { class: "healthbar" },
      h("p", { class: "muted small", style: "margin:0" }, "Everything the robot reports about itself: now, and over time."),
      h("div", { class: "seg", role: "group", "aria-label": "Time window" },
        [[24, "24 h"], [168, "7 days"], [720, "30 days"]].map(([v, t]) => h("button", { type: "button", class: v === hours ? "on" : "",
          "aria-pressed": String(v === hours), onclick: () => { state.healthHours = v; renderHealth(); } }, t)))),
    h("div", { class: "healthgrid" },
      card("Positioning",
        s ? kv("Now", colored(s.activity === "docked" ? MUTED : rtkColor(r.rtk.status), r.rtk.label)) : null,
        r ? kv("Satellites · heading DOP", `${num(r.rtk.sats)} · ${num(r.rtk.heading_dop, 2)}`) : null,
        r ? kv("Correction age", correctionsLate(r.rtk) ? colored(AMBER, `${rtcm}: not arriving`) : rtcm) : null,
        hv && hv.correction_gaps && hv.correction_gaps.length ? [
          h("p", { class: "muted small" }, `Correction gaps: ${hv.correction_gap_count}` +
            (hv.correction_gap_count > 6 ? ", the latest 6" : "")),
          hv.correction_gaps.slice(0, 6).map((g) => kv(when(g.start),
            `${g.end === null ? "still going, " : ""}${dur(g.seconds)}` +
            (g.fixes.filter((f) => f !== "RTK fixed").length ? ` · ${g.fixes.filter((f) => f !== "RTK fixed").join(", ")}` : "") +
            (typeof g.halow === "number" ? ` · HaLow ${num(g.halow)} dBm` : ""))),
        ] : null,
        h("p", { class: "muted small" }, "Share of samples by fix type"),
        shareRows.length ? shareRows : h("p", { class: "muted small" }, "collecting…"),
        chart("Satellites", [{ points: sats, color: SERIES[0] }], "", 0),
        chart("Heading DOP", [{ points: dop, color: SERIES[2] }], "", 2)),
      card("Link",
        s ? kv("HaLow now", `${num(s.robot.halow_dbm)} dBm`) : null,
        chart("HaLow signal", [{ points: halow, color: SERIES[0] }], "dBm", 0),
        s ? kv("Local broker", s.transport.local.host ? `${s.transport.local.host} · ${s.transport.local.connected ? "connected" : "down"}` : "not set up") : null,
        s ? kv("Messages · read requests", `${s.transport.local.received} · ${s.transport.local.read_requests_sent}`) : null,
        hv ? kv("Home Assistant", hv.ha.enabled ? (hv.ha.error ? colored(CORAL, hv.ha.error) : "publishing") : "not configured") : null),
      networkCard(),
      card("Power",
        r ? kv("Charge · health", `${num(r.battery)}% · ${num(r.battery_health)}%`) : null,
        r ? kv("Voltage · current", `${num(r.voltage_v, 1)} V · ${num(r.current_a, 2)} A`) : null,
        cells,
        chart("Battery", [{ points: battery, color: SERIES[0] }], "%", 0),
        chart("Voltage", [{ points: volt.map((p) => [p[0], p[1] / 1000]), color: SERIES[1] }], "V", 1),
        chart("Hottest cell", [{ points: cellmax, color: SERIES[2] }], "°C", 0)),
      card("Deck and compute",
        chart("Blade motors", [{ points: bladeL, color: SERIES[0], label: "left" }, { points: bladeR, color: SERIES[2], label: "right" }], "°C", 0),
        chart("CPU temperature", [{ points: cpu, color: SERIES[1] }], "°C", 0)),
      card("Firmware",
        s ? Object.entries(s.firmware || {}).map(([k, v]) => kv(k.replace("_", " "), v)) : null,
        fwHistory.length ? h("details", { class: "allchecks" }, h("summary", { class: "muted small" }, `History (${fwHistory.length})`), fwHistory) : null),
      card("Errors",
        r ? kv("Now", r.error_code ? colored(CORAL, String(r.error_code)) : "none") : null,
        codes.length ? codes.map((c) => kv(`code ${c.code}`, `${c.samples} samples · last ${when(c.last)}`))
                     : h("p", { class: "muted small" }, "No error codes recorded."),
        h("details", null, h("summary", { class: "muted small" }, "Plan and recharge status dictionary"),
          hv ? Object.entries(hv.status_dictionary).map(([k, v]) => kv(k, `${v.meaning}: ${v.action}`)) : null))));
}

/* ------------------------------------------------------- health: network */
// Link strengths and the robot's Wi-Fi (app/network.py). The card draws itself into its own box, so a scan (up to a
// minute on a weak link) or a join does not reload the whole Health view.
const net = { box: null, d: null, busy: null, joining: null };
const HALOW_COLOR = { good: GREEN, weak: AMBER, fallback: CORAL };
const netAgo = (s) => (s === null || s === undefined ? "" : s < 90 ? ` · ${s} s ago` : ` · ${Math.round(s / 60)} min ago`);

function networkCard() {
  net.box = h("div", { class: "card" }, h("p", { class: "label" }, "Network"), h("p", { class: "muted small" }, "Loading…"));
  getJSON("/api/network").then((d) => { net.d = d; drawNetwork(); })
    .catch((e) => fill(net.box, h("p", { class: "label" }, "Network"), h("p", { class: "muted small" }, e.message)));
  return net.box;
}

async function netAct(what, url, body) {
  net.busy = what;
  drawNetwork();
  try {
    const d = await postJSON(url, body);
    net.d = d;
    if (d.ok === false) toast(d.error || "not confirmed");
    else if (what !== "scan") toast(what === "join" ? `The robot joined ${body.name}.` : `${body.name} forgotten.`);
    net.joining = null;
  } catch (e) {
    toast(e.message);
    if (net.joining) net.joining.pw = "";      // never resend a password the field no longer shows
  }
  net.busy = null;
  drawNetwork();
}

function drawNetwork() {
  const d = net.d;
  if (!d || !net.box) return;
  const hl = d.halow, lte = d.lte, w = d.wifi, block = d.write_block;
  const lteText = lte.reported ? Object.entries(lte.fields).filter(([, v]) => v).map(([k, v]) => `${k.replace("lte_", "")} ${v}`).join(" · ")
                               : "not reported";
  const rows = (w.networks || []).map((n) => {
    const here = n.name === w.connected;
    const joining = net.joining && net.joining.name === n.name;
    const open = !n.security;                  // an open network's join was never captured
    return h("div", null,
      h("div", { class: "sharerow netrow" },
        h("span", { title: `${n.name} · ${n.security || "open"}${n.saved ? " · saved on the robot" : ""}` },
          here ? h("b", null, n.name) : n.name, n.saved && !here ? h("span", { class: "muted" }, " · saved") : null),
        h("div", { class: "bar" }, h("div", { class: "fillbar", style: `width:${n.signal ?? 0}%;background:${here ? GREEN : SKY}` })),
        mono(n.signal === null ? "—" : `${num(n.signal)}%`),
        h("span", { class: "row gap" },
          here ? h("span", { class: "muted small" }, "connected") : h("button", { type: "button", class: "btn slim",
            disabled: block || open || net.busy || null,
            title: block || (open ? "Open networks can't be joined from here yet" : "Join this network"),
            onclick: () => { net.joining = { name: n.name, pw: "" }; drawNetwork(); } }, "Join"),
          n.saved || here ? h("button", { type: "button", class: "btn slim", disabled: block || net.busy || null,
            title: block || (here ? "Forget it: the robot leaves this network now" : "Forget it on the robot"),
            onclick: () => { if (confirm(`Forget ${n.name} on the robot?${here ? " It leaves this network now." : ""} Joining again needs the password.`))
              netAct("forget", "/api/network/wifi/forget", { name: n.name }); } }, "Forget") : null)),
      joining ? h("div", { class: "row gap", style: "margin:4px 0 8px" },
        h("input", { class: "field slim netpw", type: "password", autocomplete: "off", placeholder: "Password",
          value: net.joining.pw, "aria-label": `Password for ${n.name}`, oninput: (e) => { net.joining.pw = e.target.value; } }),
        h("button", { type: "button", class: "btn slim pri", disabled: block || net.busy || null,
          onclick: () => netAct("join", "/api/network/wifi/join", { name: n.name, password: net.joining.pw }) },
          net.busy === "join" ? "Joining…" : "Join"),
        h("button", { type: "button", class: "btn slim", onclick: () => { net.joining = null; drawNetwork(); } }, "Cancel")) : null);
  });
  fill(net.box,
    h("p", { class: "label" }, "Network"),
    kv("HaLow", hl.dbm === null ? "no data" : colored(HALOW_COLOR[hl.level] || MUTED, `${num(hl.dbm)} dBm`),
       h("span", { class: "muted", title: `Below ${hl.weak_dbm} dBm is weak; at ${hl.fallback_dbm} dBm the robot switches to 4G` },
         hl.level === "fallback" ? " · 4G level" : hl.level === "weak" ? " · weak" : "")),
    kv("4G", h("span", { title: "As the robot reports it; units not confirmed" }, lteText)),
    kv("Wi-Fi", w.connected ? `${w.connected}${w.signal === null || w.signal === undefined ? "" : ` · ${num(w.signal)}%`}` : "not connected or not asked yet",
       h("span", { class: "muted" }, netAgo(w.connected_age_s))),
    h("div", { class: "row gap", style: "justify-content:space-between;margin:8px 0 4px" },
      h("span", { class: "muted small" }, w.networks ? `Nearby${netAgo(w.scan_age_s)}` : "No scan yet"),
      h("button", { type: "button", class: "btn slim", disabled: net.busy || null,
        title: "A fresh scan on the robot (about 4 s; up to a minute on a weak link)",
        onclick: () => netAct("scan", "/api/network/scan") }, net.busy === "scan" ? "Scanning…" : "Scan")),
    rows,
    block && (w.networks || []).length ? h("p", { class: "muted tiny", style: "margin:6px 0 0" }, `Join and Forget: ${block}`) : null);
}

/* -------------------------------------------------------------- events */
// One log, newest first, by day: the panel's events and every command it sent or refused (the control log, which
// used to be a separate list in Settings → Safety).
const EVENT_GROUPS = [["all", "All"], ["commands", "Commands"], ["robot", "Robot and runs"], ["map", "Map"], ["panel", "Panel"]];
const GROUP_COLOR = { commands: SKY, robot: GREEN, map: AMBER, panel: MUTED };
function eventGroup(kind) {
  if (/^(recorder_started|settings_changed|control_switched|map_write_switched|wifi_survey_switched|demo_seeded|test|imagery_source)$/.test(kind)) return "panel";
  if (kind === "new_command_seen") return "commands";        // another client (the app) sent something new
  if (/^(map_|backup_|pathway_settings|app_edit)/.test(kind)) return "map";
  return "robot";
}

function commandDetail(l) {
  const args = l.args && l.args !== "{}" ? ` ${l.args}` : "";
  const r = l.result || {};
  const outcome = l.sent ? (r.ok === false ? `sent, robot said: ${r.msg || r.error || `state ${r.state}`}` : "sent")
    : `refused: ${r.refused || r.error || "—"}`;
  return `${l.actor}${args} · ${outcome}`;
}

async function renderEvents(sub) {
  if (sub && EVENT_GROUPS.some(([id]) => id === sub)) state.eventsGroup = sub;
  const panel = $("#p-events");
  if (!state.eventsRows) fill(panel, h("p", { class: "muted pad" }, "Loading…"));
  const [ev, log] = await Promise.all([
    getJSON("/api/events?limit=500").then((d) => d.events).catch(() => null),
    getJSON("/api/control/log?limit=300").then((d) => d.log).catch(() => null)]);
  if (state.view !== "events") return;
  state.eventsError = ev === null ? "the events could not be read" : log === null ? "the command log could not be read" : null;
  state.eventsRows = [
    ...(ev || []).map((e) => ({ ts: e.ts, group: eventGroup(e.kind), kind: e.kind.replace(/_/g, " "), detail: e.detail || "" })),
    ...(log || []).map((l) => ({ ts: l.ts, group: "commands", kind: l.cmd, detail: commandDetail(l),
                                 cls: !l.sent ? "refused" : /^(get_|read_)/.test(l.cmd) ? "read" : "" })),
  ].sort((a, b) => b.ts - a.ts);
  state.eventsShown = 300;
  drawEvents();
}

function drawEvents() {
  const panel = $("#p-events"), rows = state.eventsRows || [], g = state.eventsGroup || "all";
  const count = (id) => (id === "all" ? rows.length : rows.filter((r) => r.group === id).length);
  fill(panel,
    h("div", { class: "evtools" },
      h("div", { class: "seg", role: "group", "aria-label": "Show" }, EVENT_GROUPS.map(([id, label]) => h("button", {
        type: "button", class: id === g ? "on" : "", "aria-pressed": String(id === g),
        onclick: () => { state.eventsGroup = id; history.replaceState(null, "", id === "all" ? "#events" : `#events/${id}`); drawEvents(); } },
        label, h("span", { class: "n" }, String(count(id)))))),
      h("div", { class: "row gap" },
        h("input", { class: "field slim", type: "search", placeholder: "Search the log", "aria-label": "Search the log", value: state.eventsQuery || "",
                     oninput: (e) => { state.eventsQuery = e.target.value; state.eventsShown = 300; drawEventList(); } }),
        h("button", { type: "button", class: "btn slim", onclick: () => renderEvents() }, "Refresh"))),
    state.eventsError ? h("p", { class: "small pad", style: `color:${AMBER};margin:0` }, `Partly shown: ${state.eventsError}.`) : null,
    h("ul", { class: "evlist", id: "evlist" }));
  drawEventList();
}

function drawEventList() {
  const el = $("#evlist");
  if (!el) return;
  const g = state.eventsGroup || "all", q = (state.eventsQuery || "").trim().toLowerCase();
  const rows = (state.eventsRows || []).filter((r) => (g === "all" || r.group === g)
    && (!q || r.kind.toLowerCase().includes(q) || r.detail.toLowerCase().includes(q)));
  const dayOf = (ts) => {
    const d = new Date(ts * 1000), today = new Date();
    const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
    return days === 0 ? "Today" : days === 1 ? "Yesterday" : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  };
  const items = [];
  let last = null;
  for (const r of rows.slice(0, state.eventsShown || 300)) {
    const day = dayOf(r.ts);
    if (day !== last) { items.push(h("li", { class: "evday" }, day)); last = day; }
    items.push(h("li", { class: `ev ${r.cls || ""}` },
      h("span", { class: "t" }, clock(r.ts)),
      h("span", { class: "k" }, dot(GROUP_COLOR[r.group]), r.kind),
      h("span", { class: "d" }, r.detail)));
  }
  fill(el, items.length ? items : h("li", { class: "muted", style: "padding:14px 4px" }, q ? "Nothing matches." : "Nothing logged yet."),
    rows.length > (state.eventsShown || 300) ? h("li", { style: "padding:12px 4px" },
      h("button", { type: "button", class: "btn slim", onclick: () => { state.eventsShown += 300; drawEventList(); } },
        `Show more (${rows.length - state.eventsShown} older)`)) : null);
}

/* ---------------------------------------------------------------- data */
const matches = (s, f) => String(s ?? "").toLowerCase().includes(f);

function highlighted(text, f) {
  const s = String(text);
  const i = f ? s.toLowerCase().indexOf(f) : -1;
  if (i < 0) return [s];
  return [s.slice(0, i), h("span", { class: "hit" }, s.slice(i, i + f.length)), s.slice(i + f.length)];
}

function treeNode(value, key, filter, depth = 0) {
  const keyEl = key === undefined ? null : h("span", { class: "k" }, ...highlighted(key, filter));
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    const children = entries.map(([ck, cv]) => treeNode(cv, ck, filter, depth + 1)).filter(Boolean);
    if (filter && !children.length && !matches(key, filter)) return null;
    const label = Array.isArray(value) ? `[${value.length}]` : `{${entries.length}}`;
    return h("details", { open: depth < 1 || !!filter },
      h("summary", null, keyEl, " ", h("span", { class: "muted" }, label)), children);
  }
  const text = typeof value === "string" ? `"${value}"` : String(value);
  if (filter && !matches(key, filter) && !matches(text, filter)) return null;
  return h("div", { class: "leaf" }, keyEl, h("span", { class: "v" }, ...highlighted(text, filter)));
}

/* --------------------------------------------------------------- views */
const VIEWS = { live: "Live", runs: "Runs", studio: "Map Studio", schedule: "Schedule", maintenance: "Maintenance", health: "Health",
                events: "Events", settings: "Settings", help: "Help" };
const WIDE = new Set(["health", "schedule", "maintenance", "events", "settings", "help"]);
const MOVED = { insights: "runs", data: "settings/data" };            // old addresses keep working
// Views shown as tabs under one nav item: the item is the group, the tabs are the views.
const GROUPS = { robot: { title: "Robot", tabs: [["health", "Health"], ["maintenance", "Maintenance"], ["events", "Events"]] } };
const groupOf = (name) => Object.keys(GROUPS).find((g) => GROUPS[g].tabs.some(([v]) => v === name)) || null;
const isWide = (name, sub) => WIDE.has(name) || (name === "runs" && !sub);

// A hash is a view, or a view and a sub-page: #settings/cameras.
function showView(hash) {
  let [name, sub] = String(hash || "").split("/");
  if (MOVED[name]) { const to = MOVED[name]; history.replaceState(null, "", `#${to}`); [name, sub] = to.split("/"); }
  if (GROUPS[name]) { name = state.groupTab?.[name] || GROUPS[name].tabs[0][0]; history.replaceState(null, "", `#${name}`); }
  if (!VIEWS[name]) name = "live";
  const group = groupOf(name);
  if (group) (state.groupTab ||= {})[group] = name;
  sub = sub ? decodeURIComponent(sub) : null;
  const wide = isWide(name, sub);
  if (state.view !== name) window.scrollTo(0, 0);
  state.view = name;
  for (const a of $$(".nav[data-view]")) {
    if (a.dataset.view === (group || name)) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  // On a phone the nav is a scrolling row: keep the current view's item in sight.
  const side = $(".side"), cur = $(".nav[aria-current]");
  if (cur && side.scrollWidth > side.clientWidth) side.scrollTo({ left: cur.offsetLeft - (side.clientWidth - cur.offsetWidth) / 2, behavior: "smooth" });
  for (const key of Object.keys(VIEWS)) $(`#p-${key}`).hidden = key !== name;
  $(".shell").classList.toggle("wide", wide);
  $(".shell").dataset.view = name;
  $("#viewtitle").textContent = group ? GROUPS[group].title : VIEWS[name];
  document.title = `${VIEWS[name]} · Groundskeeper`;
  const tabs = $("#subtabs");
  tabs.hidden = !group;
  fill(tabs, group ? GROUPS[group].tabs.map(([v, label]) =>
    h("a", { class: "subtab", href: `#${v}`, "aria-current": v === name ? "page" : null }, label)) : []);
  if (state.status) renderChips(state.status);
  if (name === "runs") showRuns(sub);
  if (name === "events") renderEvents(sub);
  if (name === "health") renderHealth();
  if (name === "schedule") renderSchedule();
  if (name === "maintenance") renderMaintenance();
  if (name === "settings") renderSettings(sub);
  if (name === "help") renderHelp(sub);
  syncHelpLink(name);
  if (name === "live") {
    if (state.run) selectRun("recent");          // Live is now: the last 24 h, never an old run
    if (state.status) renderLive(state.status);
  }
  syncLiveMode();
  if (!wide && !state.fitted) requestAnimationFrame(() => { state.fitted = fitYard(); });
  studioViewChanged(name);
  syncToolButtons();
  sync3d();
}

function bindViews() {
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
  showView(location.hash.slice(1));
}

/* -------------------------------------------------------------- stream */
// After a redeploy the stream reconnects and reports a new UI version: the page reloads itself, but not in the middle
// of something (a half-typed field, an open confirmation, drawing, aligning, a Studio tool). Then it says so and waits.
const UI_VERSION = document.querySelector('meta[name="ui-version"]')?.content || null;
function reloadIfOutdated(s) {
  if (!UI_VERSION || !s.ui || s.ui === UI_VERSION) return;
  let tried = null;
  try { tried = sessionStorage.getItem("yarbo.reloadedFor"); } catch { /* no storage: reload anyway, once per load */ }
  if (tried === s.ui) return;                       // reloaded for this version already and still old: do not loop
  const el = document.activeElement;
  const busy = state.align || ops.drawing || ops.confirm || ops.editing
    || (typeof studio !== "undefined" && (studio.mode !== "select" || studio.renaming || studio.busy))
    || (el && (el.isContentEditable || (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) && !["checkbox", "radio", "range", "button"].includes(el.type))));
  if (busy) {
    if (!state.reloadNoted) { state.reloadNoted = true; toast("The panel was updated; it reloads once you finish this."); }
    return;
  }
  try { sessionStorage.setItem("yarbo.reloadedFor", s.ui); } catch { /* see above */ }
  location.reload();
}

function connectStream() {
  const es = new EventSource("/api/stream");
  let lastRender = 0, lastTrack = 0, lastRunGrow = 0, liveSel = null, wasRun = false;
  es.addEventListener("pose", (ev) => robot3dPoses(JSON.parse(ev.data)));
  es.addEventListener("status", (ev) => {
    const s = JSON.parse(ev.data);
    reloadIfOutdated(s);
    state.status = s;
    if (wasRun && !s.run) refreshGround();                 // the run's heights are in: the 3D ground may be a new one
    wasRun = !!s.run;
    renderChips(s, true);
    setRobot(s.robot.position);
    robot3dLive(s.robot);
    styleRobot(s);
    $("#rc-sn").textContent = s.sn;
    $("#rc-head").textContent = `Core · ${s.robot.head}`;
    if (Date.now() - lastRender > 5000) {
      lastRender = Date.now();
      if (state.view === "live") renderLive(s);
    }
    if (state.layers.trouble && s.run && !document.hidden && Date.now() - (state.problemsAt || 0) > 120000) loadProblems();
    if (state.layers.signal && s.run && !document.hidden && state.view !== "studio" && Date.now() - state.signalAt > 60000) loadSignal();
    // The track follows whichever way it is shown: the last 24 h every 3 s, or a selected run that is still recording
    // every 10 s (before, picking a run froze the live line until a reload). When the selected run stops being the
    // live one (it ended, or was folded into the run before it) it gets one last catch-up.
    if (state.layers.track) {
      const now = Date.now();
      if (!state.run && state.selectedRun === "recent") {
        if (now - lastTrack > 3000) { lastTrack = now; showRecentTrack(); }
      } else if (state.run) {
        const id = String(state.run.id), live = !!s.run && String(s.run.id) === id;
        if (live && now - lastRunGrow > 10000) { lastRunGrow = now; growSelectedRun(); }
        if (!live && liveSel === id) growSelectedRun();
        liveSel = live ? id : null;
      }
    }
  });
  es.onerror = () => {
    fetch("/api/auth/status", { cache: "no-store" }).then((r) => r.json())                 // signed out meanwhile?
      .then((a) => { if (!a.disabled && !a.signed_in) toSignIn(); }).catch(() => null);
    state.live.resetPending = true;          // start the live track over once the stream is back
    stopPulse();
    if (state.status) renderChips(state.status, false);
  };
}

// Rows that scroll sideways (the nav and the chips on a phone, the map's tools) fade at an edge with more behind it.
function edgeFade(el) {
  if (!el) return;
  const update = () => {
    const max = el.scrollWidth - el.clientWidth;
    el.classList.toggle("fade-l", max > 2 && el.scrollLeft > 2);
    el.classList.toggle("fade-r", max > 2 && el.scrollLeft < max - 2);
  };
  el.addEventListener("scroll", update, { passive: true });
  new ResizeObserver(update).observe(el);
  new MutationObserver(update).observe(el, { childList: true, subtree: true });
  update();
}

// Touch screens never show a title tooltip, and an ⓘ holds nothing else: a tap on one shows its text in the toast.
document.addEventListener("click", (e) => {
  const tip = e.target instanceof Element && e.target.closest(".tip[title]");
  if (tip && window.matchMedia("(hover: none)").matches) toast(tip.title);
});

// ops.js loads after this file; start once every script has run.
window.addEventListener("DOMContentLoaded", () => {
  for (const el of [$(".side"), $("#chips"), $("#conn"), $(".maptools")]) edgeFade(el);
  bindViews();
  bindMapTools();
  initMap();
  connectStream();
});
