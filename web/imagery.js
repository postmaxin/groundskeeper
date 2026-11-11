/* Settings → Map imagery: the picker, drawn into the section settings.js provides (#imgpicker). Loaded after ops.js
   and uses the app's helpers (h, svg, fill, card, toast, getJSON, postJSON, loadImagery, colours). Every card shows the sample of the yard the server's last check fetched
   from that source; the browser never contacts an imagery source itself. */
"use strict";

const pick = { data: null, checking: false, editingKey: null };

async function send(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                               body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { /* not json */ }
  if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${text}`);
  return data;
}

const fmtRes = (m) => { const cm = m * 100; return cm < 10 ? `${cm.toFixed(1)} cm` : cm < 100 ? `${Math.round(cm)} cm` : `${+m.toFixed(1)} m`; };
/* The number on every card is metres of ground per pixel; people rarely know what 15 cm looks like, so each
   step of the scale says what you can make out at that sharpness. Smaller is sharper. */
const RES_SCALE = [
  [0.10, "Very sharp", "sharp", "7–10 cm: single pavers, sprinkler heads, the mower itself; the lawn edge is crisp"],
  [0.20, "Sharp", "sharp", "15–20 cm: beds, paths and the lawn edge are clear; fine for drawing no-go zones"],
  [0.35, "Good", null, "25–30 cm: edges are soft but you can still place zones to within a step"],
  [0.70, "Coarse", "coarse", "50–60 cm: a lawn edge is one blurry pixel; enough to recognise the yard, not to draw on"],
  [Infinity, "Too coarse", "coarse", "1 m and up: whole beds blur together; look for a better source"],
];
const sharpness = (m) => RES_SCALE.find(([max]) => m <= max);
const resTitle = (m) => `${fmtRes(m)} of ground per pixel (smaller is sharper). ${sharpness(m)[3]}.`;
function resScale() {
  return h("details", { class: "imgnote", open: true, style: "display:block;margin-top:10px" },
    h("summary", { class: "small" }, h("b", null, "How sharp is sharp? "), h("span", { class: "muted" }, "The cm number on each card is how much ground one pixel covers; smaller is sharper.")),
    h("dl", { class: "resscale" }, RES_SCALE.map(([, name, , what]) => [h("dt", null, name), h("dd", null, what)])),
    h("p", { class: "small", style: "margin-top:8px" }, "Where to find sharper: county and state GIS services are often at 7–15 cm where national programmes stop at 30–60 cm. ",
      "Use ", h("b", null, "Browse the catalog"), " to see every source's sharpest; a keyed service (Esri, Mapbox) can be sharper than the free ones; ",
      "or let an ", h("a", { href: "#settings/assistant" }, "AI assistant"), " compare the samples and hunt down a county service for you. ",
      "Sharpest is not always best: a leaf-off (spring) flight shows the ground under trees that a leaf-on one hides."));
}
const ago = (ts) => {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
};
const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
const host = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } };
const regionName = (cc) => {
  if (cc === "XX") return "Worldwide, with your own key";
  try { return new Intl.DisplayNames(["en"], { type: "region" }).of(cc) || cc; } catch { return cc; }
};
// US postal codes, so a catalog search for "TX" finds Texas.
const US_CODES = { AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
  CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho",
  IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland",
  MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska",
  NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina",
  ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island",
  SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia",
  WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming" };
const US_CODE_OF = Object.fromEntries(Object.entries(US_CODES).map(([code, name]) => [name, code]));

function licenceBadge(c) {
  const l = c.license || {};
  if (c.auth) return ["Your key", "key"];
  if (l.commercial === false) return ["Non-commercial", "nc"];
  if (l.id === "public-domain") return ["Public domain", "free"];
  return [l.credit ? "Free · credit" : "Free", "free"];
}
const season = (c) => { const v = (c.vintage || "").toLowerCase(); return v.includes("leaf-off") ? "Leaf-off" : v.includes("leaf-on") ? "Leaf-on" : null; };
const badge = (text, kind) => h("span", { class: `badge${kind ? ` ${kind}` : ""}` }, text);
const lockIcon = () => svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "lockico" },
  svg("rect", { x: 5, y: 11, width: 14, height: 9, rx: 2 }), svg("path", { d: "M8 11V8a4 4 0 0 1 8 0v3" }));

/* An action on the picker: the button says what is happening, the result is a toast, then the map and the card
   are redrawn from the server. */
async function act(button, label, run, done) {
  if (button) { button.disabled = true; button.textContent = label; }
  try {
    const r = await run();
    if (r && r.warning) toast(r.warning);
    else if (done) toast(typeof done === "function" ? done(r) : done);
  } catch (e) { toast(e.message); }
  await loadImagery();
  await refreshPicker();
}

async function refreshPicker() {
  const el = $("#imgpicker");
  if (!el) return;
  if (state.view === "settings") { await renderSettings(); return; }   // the list and the alignment card follow too
  try { drawPicker(el, await getJSON("/api/imagery/sources")); } catch (e) { toast(e.message); }
}

function status(c) {
  const p = c.probe;
  if (pick.checking && c.covers !== false && (!c.auth || c.has_key)) return colored(AMBER, "Checking…");
  if (c.auth && !c.has_key) return colored(MUTED, "Not contacted");
  if (!p) return colored(MUTED, "Not checked yet");
  if (p.ok === true) return h("span", null, colored(GREEN, "Has imagery at your yard"),
    p.error ? h("span", { class: "muted", title: p.error }, " · last check failed") : null);
  if (p.error) return h("span", { style: `color:${AMBER}`, title: p.error }, "Could not check it; trying again within the hour");
  if (p.ok === false) return h("span", { style: `color:${CORAL}`, title: p.detail || "" }, "No imagery at your yard");
  return colored(MUTED, p.detail || "");
}

function thumb(c) {
  if (c.preview) {
    return h("button", { type: "button", class: "imgthumb", "aria-label": `Enlarge the ${c.name} sample`, onclick: () => openSample(c) },
      h("img", { src: c.preview, alt: `Your yard as ${c.name} shows it`, loading: "lazy" }),
      h("span", { class: "resbadge", title: resTitle(c.native_m) }, fmtRes(c.native_m)));
  }
  const locked = c.auth && !c.has_key;
  return h("div", { class: "imgthumb empty" }, locked ? lockIcon() : null,
    h("span", null, locked ? "Add a key to see your yard" : c.probe?.error ? "Could not check it yet"
      : c.probe?.ok === false ? "No imagery here" : "No sample yet"));
}

function keyArea(c) {
  if (pick.editingKey === c.id) {
    const input = h("input", { class: "field slim", type: "password", autocomplete: "off", id: `key-${c.id}`, placeholder: "paste it here" });
    setTimeout(() => input.focus(), 0);
    return h("div", { class: "keyedit" },
      h("label", { for: `key-${c.id}`, class: "muted small" }, `${c.auth.label[0].toUpperCase()}${c.auth.label.slice(1)}`,
        c.auth.signup ? [" · ", h("a", { href: c.auth.signup, target: "_blank", rel: "noopener" }, "get one")] : null),
      input,
      h("div", { class: "row gap" },
        h("button", { type: "button", class: "btn slim pri", onclick: (e) => {
          if (!input.value.trim()) { input.focus(); return; }
          pick.editingKey = null;
          act(e.target, "Checking…", () => send("PUT", "/api/imagery/key", { provider: c.id, key: input.value }),
              (r) => r.probe?.ok ? `Key saved on the server. ${c.name} has imagery at your yard.` : `Key saved on the server. ${c.name}: ${r.probe?.detail || "not checked"}.`);
        } }, "Save and check"),
        h("button", { type: "button", class: "btn slim", onclick: () => { pick.editingKey = null; drawPicker($("#imgpicker"), pick.data); } }, "Cancel")));
  }
  if (!c.has_key) {
    return h("div", { class: "imgactions" }, colored(MUTED, "Not contacted"),
      h("button", { type: "button", class: "btn slim", onclick: () => { pick.editingKey = c.id; drawPicker($("#imgpicker"), pick.data); } }, "Add key"));
  }
  return null;
}

function sourceCard(c, d) {
  const inUse = c.id === d.current;
  const usable = c.probe?.ok === true && (!c.auth || c.has_key);
  const sea = season(c);
  const [lic, licKind] = licenceBadge(c);
  return h("article", { class: `imgcard${inUse ? " on" : ""}` },
    thumb(c),
    h("div", { class: "imgbody" },
      h("div", { class: "imgname" }, c.name,
        c.origin !== "bundled" ? h("span", { class: "tag" }, c.origin === "override" ? "replaces a bundled one" : "added by you") : null),
      h("div", { class: "muted small clamp" }, [c.region, c.vintage].filter(Boolean).join(" · ")),
      h("div", { class: "badges", title: resTitle(c.native_m) }, badge(lic, licKind), c.auth ? badge(`up to ${fmtRes(c.native_m)}`) : null,
        badge(`${sharpness(c.native_m)[1]} · ${fmtRes(c.native_m)}/px`, sharpness(c.native_m)[2]), sea ? badge(sea) : null),
      c.auth && !c.has_key ? null : h("span", { class: "small" }, status(c)),
      c.auth ? keyArea(c) : null,
      !c.auth || c.has_key ? h("div", { class: "imgactions" },
        inUse ? h("span", { class: "inuse" }, "In use")
          : usable ? h("button", { type: "button", class: "btn slim", onclick: (e) => act(e.target, "Fetching…",
              () => send("PUT", "/api/imagery/source", { provider: c.id }), `Now showing ${c.name}. It stays until you change it.`) }, "Use")
          : h("span"),
        h("span", { class: "row gap" },
          c.preview ? h("button", { type: "button", class: "linkbtn", onclick: () => openSample(c) }, "Details") : null,
          c.auth && c.has_key ? h("button", { type: "button", class: "linkbtn muted", onclick: (e) => act(e.target, "Removing…",
            () => send("PUT", "/api/imagery/key", { provider: c.id, key: "" }), "Key removed.") }, "Remove key") : null,
          c.origin !== "bundled" ? h("button", { type: "button", class: "linkbtn", onclick: () => openWizard(d, c) }, "Export") : null,
          c.origin === "local" ? h("button", { type: "button", class: "linkbtn muted", onclick: (e) => {
            if (confirm(`Remove ${c.name} from this server?`)) act(e.target, "Removing…", () => send("DELETE", `/api/imagery/sources/${c.id}`), `${c.name} removed.`);
          } }, "Remove") : null)) : null));
}

function modeBox(d) {
  const auto = d.mode === "auto";
  const name = (id) => (d.sources.find((c) => c.id === id) || {}).name || id;
  const showing = name(d.current);
  const seg = (on, label, run, done) => h("button", { type: "button", class: on ? "on" : null, "aria-pressed": on ? "true" : "false",
    onclick: on ? null : (e) => act(e.target, "…", run, done) }, label);
  const choice = d.auto.choice;
  let text;
  if (auto && d.auto.failed && d.auto.failed !== d.current) {
    text = ["Automatic would use ", h("b", null, name(d.auto.failed)), ", the sharpest free source with imagery at your yard, but fetching it failed",
      d.auto.error ? ` (${d.auto.error})` : "", ". Still showing ", h("b", null, showing), "; it tries again a day later."];
  } else if (auto && choice && choice !== d.current) {
    text = ["Automatic is moving to ", h("b", null, name(choice)), ", the sharpest free source with imagery at your yard. Showing ", h("b", null, showing), " until then."];
  } else if (auto && choice) {
    text = ["Automatic picked ", h("b", null, showing), d.auto.at ? ` on ${day(d.auto.at)}` : "",
      ": the sharpest free source with real imagery at your yard. It looks again when an update adds sources for your area, and notes any switch in Events."];
  } else if (auto) {
    text = ["Automatic has not found a free source with imagery at your yard yet. Add a key below, or a source of your own."];
  } else {
    text = ["Kept on ", h("b", null, showing), " until you change it.",
      choice && choice !== d.current ? ` Automatic would use ${name(choice)}.` : choice ? " It is also what Automatic would pick." : ""];
  }
  return h("div", { class: "imgmode" },
    h("div", { class: "seg", role: "group", "aria-label": "How the source is chosen" },
      seg(auto, "Automatic", () => send("PUT", "/api/imagery/source", { mode: "auto" }),
          (r) => `Automatic: showing ${name(r.chosen || d.current)}.`),
      seg(!auto, "My choice", () => send("PUT", "/api/imagery/source", { provider: d.current }),
          `Kept on ${showing} until you change it.`)),
    h("p", null, ...text));
}

function worldToggle(ctx) {
  const w = ctx && ctx.world_layer;
  if (!w) return null;
  return h("label", { class: "row gap small imgworld" },
    h("input", { type: "checkbox", checked: ctx.world_on || null,
                 onchange: (e) => act(null, "", () => putJSON("/api/imagery/context", { world: e.target.checked }),
                                      e.target.checked ? "Satellite imagery around the yard is on." : "Satellite imagery around the yard is off.") }),
    h("span", null, `Show ${w.name} around the yard when zoomed out. `,
      h("span", { class: "muted" }, `${w.license?.commercial === false ? "Non-commercial licence. " : ""}It only ever sees zoomed-out tiles, never your yard.`)));
}

function drawPicker(el, d) {
  if (!el || !d) return;
  pick.data = d;
  const here = d.sources.filter((c) => c.covers !== false);
  const byRes = (a, b) => a.native_m - b.native_m;
  const free = here.filter((c) => !c.auth).sort(byRes), keyed = here.filter((c) => c.auth).sort(byRes);
  const cur = d.yard?.source;
  const checked = Math.max(0, ...free.map((c) => c.probe?.at || 0));
  fill(el,
    h("div", { class: "imghead" },
      cur ? h("p", null, "Showing ", h("b", null, cur.name), h("span", { class: "muted", title: resTitle(cur.m_per_px) }, ` · ${fmtRes(cur.m_per_px)} per pixel (${sharpness(cur.m_per_px)[1].toLowerCase()}) · fetched ${ago(cur.fetched)}`))
          : h("p", { class: "muted" }, "No imagery yet: it is fetched once the robot's map has been read."),
      h("span", { class: "row gap" },
        h("button", { type: "button", class: "btn slim pri", onclick: () => openBrowse(d) }, "Browse the catalog"),
        h("button", { type: "button", class: "btn slim pri", onclick: (e) => act(e.target, "Fetching…", () => postJSON("/api/imagery/refresh"), "Imagery fetched again.") }, "Fetch again"))),
    farNote(d.far),
    modeBox(d),
    (d.problems || []).length ? h("p", { class: "small", style: `color:${CORAL}` }, `Skipped catalog entries: ${d.problems.join(" · ")}`) : null,
    resScale(),
    h("div", { class: "imgsection" },
      h("h3", null, "Free at your yard"),
      h("p", { class: "muted small" }, "Sharpest first. ", pick.checking ? `Checking the ${d.counts.contacted} sources that cover your yard…`
        : checked ? `Checked ${ago(checked)} · ` : "Not checked yet · ",
        pick.checking ? null : h("button", { type: "button", class: "linkbtn", onclick: async () => {
          pick.checking = true; drawPicker(el, d);
          try { await postJSON("/api/imagery/probe"); } catch (e) { toast(e.message); }
          pick.checking = false; await loadImagery(); await refreshPicker();
        } }, "Check again"))),
    free.length ? h("div", { class: "imgcards" }, free.map((c) => sourceCard(c, d)))
      : h("p", { class: "muted small" }, "No free source in the catalog covers your yard. Add a key below, or a source of your own."),
    keyed.length ? [
      h("div", { class: "imgsection" }, h("h3", null, "With your own key"),
        h("p", { class: "muted small" }, "Worldwide services. Nothing goes to them until you add a key, and then only your yard. Keys stay on the server.")),
      h("div", { class: "imgcards" }, keyed.map((c) => sourceCard(c, d)))] : null,
    catalogSection(d),
    h("div", { class: "imgnote" },
      h("p", null, `The panel worked out on its own which sources cover your yard and contacted only those ${d.counts.contacted}. ${d.counts.elsewhere} sources for other regions were never contacted.`)),
    worldToggle(d.context),
    addSourceRow(d));
}

// The whole catalog, as its own section: what is in it, and a button to look through it (contacts nothing).
function catalogSection(d) {
  const all = d.sources.filter((c) => c.origin === "bundled" || c.origin === "override");
  const countries = new Set(all.map((c) => c.country).filter((cc) => cc && cc !== "XX"));
  const sharp = all.filter((c) => !c.auth && c.native_m <= 0.15).length;
  return h("div", { class: "imgsection catsection" },
    h("div", { class: "cathead" },
      h("div", null,
        h("h3", null, "The whole catalog"),
        h("p", { class: "muted small" }, `${all.length} sources in ${countries.size} countries, ${sharp} of them free at 15 cm or sharper. `,
          "Look up any place, see what each covers, how sharp it is and its licence. Browsing contacts none of them.")),
      h("button", { type: "button", class: "btn pri", onclick: () => openBrowse(d) }, `Browse the catalog (${all.length})`)));
}

/* ------------------------------------------------------------ dialogs */
function dialog(box, labelId) {
  const opener = document.activeElement;
  const shell = $(".shell");
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    if (shell) shell.removeAttribute("inert");
    const back = opener && opener.isConnected ? opener : $("#imgpicker");
    if (back && back.focus) back.focus();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const overlay = h("div", { class: "imgdialog", onclick: (e) => { if (e.target === overlay) close(); } });
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-labelledby", labelId);
  overlay.append(box);
  if (shell) shell.setAttribute("inert", "");
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  return close;
}

const drow = (k, v) => h("div", { class: "dkv" }, h("span", null, k), h("span", null, v));

function openSample(c) {
  const d = pick.data;
  const inUse = d && c.id === d.current;
  const usable = c.probe?.ok === true && (!c.auth || c.has_key);
  const lic = c.license || {};
  let close = () => {};
  const closeBtn = h("button", { type: "button", class: "btn slim", onclick: () => close() }, "Close");
  const box = h("div", { class: "imgdialogbox" },
    h("div", { class: "imgdialogimg" },
      c.preview ? h("img", { src: c.preview, alt: `A larger sample of your yard from ${c.name}` }) : h("div", { class: "imgthumb empty" }, "No sample yet"),
      h("p", { class: "muted tiny" }, `A 40 m square at the middle of your yard, from the coverage check, at ${fmtRes(c.probe?.m_per_px || c.native_m)} per pixel.`)),
    h("div", { class: "imgdialoginfo" },
      h("h3", { id: "imgdlg-title" }, c.name),
      drow("Provider", c.provider || "—"),
      drow("Resolution", `${fmtRes(c.native_m)} per pixel · ${sharpness(c.native_m)[1].toLowerCase()}`),
      drow("What that shows", sharpness(c.native_m)[3]),
      c.vintage ? drow("Season and date", c.vintage) : null,
      drow("Licence", lic.url ? h("a", { href: lic.url, target: "_blank", rel: "noopener" }, lic.name || lic.id) : lic.name || "Not stated"),
      drow("Credit shown on the map", c.attribution),
      c.verified ? drow("Checked by its contributor at", `${c.verified.place} · ${c.verified.date}`) : null,
      c.homepage ? drow("More", h("a", { href: c.homepage, target: "_blank", rel: "noopener" }, host(c.homepage))) : null,
      h("div", { class: "row gap wrap", style: "margin-top:14px" },
        inUse ? h("span", { class: "inuse" }, "In use")
          : usable ? h("button", { type: "button", class: "btn slim pri", onclick: (e) => { close(); act(null, "", () => send("PUT", "/api/imagery/source", { provider: c.id }), `Now showing ${c.name}. It stays until you change it.`); } }, "Use this source") : null,
        closeBtn)));
  close = dialog(box, "imgdlg-title");
  closeBtn.focus();
}

function openBrowse(d) {
  const ui = { q: "", filter: "all" };
  let close = () => {};
  const list = h("div", { class: "catrows" });
  const summary = h("p", { class: "muted small" });
  const all = d.sources.filter((c) => c.origin === "bundled" || c.origin === "override");
  // A source's own state or province is the part of its region after the last comma ("Ada County, Idaho" is Idaho);
  // a country-wide one serves the places the server lists (USGS NAIP: the lower 48 and DC).
  const own = (c) => (c.region || "").split(",").pop().trim();
  const national = (c) => own(c).startsWith(regionName(c.country));
  const serves = (c) => (c.places?.length ? c.places : [own(c)]);
  const places = new Map();                  // a place's name or US postal code, lower case → the place's name
  for (const c of all) for (const p of serves(c)) {
    places.set(p.toLowerCase(), p);
    if (c.country === "US" && US_CODE_OF[p]) places.set(US_CODE_OF[p].toLowerCase(), p);
  }
  const usStates = new Set(all.filter((c) => c.country === "US").flatMap(serves)
    .filter((p) => US_CODE_OF[p] && p !== "District of Columbia")).size;
  const placeLabel = (c, p) => [p, c.country === "US" && US_CODE_OF[p] ? h("span", { class: "muted tiny" }, US_CODE_OF[p]) : null];
  const draw = () => {
    const q = ui.q.trim().toLowerCase();
    const place = places.get(q);             // a whole state or province, by name or code: everything that serves it
    const text = (c) => `${regionName(c.country)} ${c.country} ${c.name} ${c.region} ${c.provider || ""} ${serves(c).join(" ")}`.toLowerCase();
    const rows = all.filter((c) => (ui.filter !== "free" || !c.auth) && (ui.filter !== "key" || c.auth)
      && (place ? serves(c).includes(place) || c.worldwide : !q || text(c).includes(q)));
    const groups = {};
    for (const c of rows) (groups[c.country] = groups[c.country] || []).push(c);
    // Within a country: the searched place first, then the whole country, then by state or province, the whole
    // state before its counties and cities, sharpest first.
    for (const g of Object.values(groups)) g.sort((a, b) => (own(b) === place) - (own(a) === place)
      || national(b) - national(a) || own(a).localeCompare(own(b))
      || (a.region || "").includes(",") - (b.region || "").includes(",") || a.native_m - b.native_m);
    const order = Object.keys(groups).sort((a, b) => (groups[b].some((c) => c.covers) - groups[a].some((c) => c.covers))
      || (a === "XX") - (b === "XX") || regionName(a).localeCompare(regionName(b)));
    const free = rows.filter((c) => !c.auth);
    summary.textContent = place
      ? `${rows.length} ${rows.length === 1 ? "source serves" : "sources serve"} ${place}`
        + (free.length ? `; the sharpest free one is ${fmtRes(Math.min(...free.map((c) => c.native_m)))}.` : ".")
        + " County and city sources cover only their own area."
      : rows.length === all.length ? `${all.length} sources in ${new Set(all.filter((c) => c.country !== "XX").map((c) => c.country)).size} countries${usStates === 50 ? " (every US state)" : usStates ? ` (${usStates} US states)` : ""}, plus worldwide services that need your own key. Search a state by name or code (Texas, TX) to see everything that serves it. Sources for other regions are never contacted from your yard.`
      : `${rows.length} of ${all.length} sources match.`;
    const row = (c) => h("div", { class: "catrow" },
      h("span", { class: "catname" }, h("span", null, c.name), h("span", { class: "muted tiny" }, c.region)),
      h("span", { class: "mono", title: resTitle(c.native_m) }, fmtRes(c.native_m)),
      h("span", null, badge(...licenceBadge(c))),
      h("span", { class: "small" }, c.verified ? c.verified.place : "—"),
      c.homepage ? h("a", { href: c.homepage, target: "_blank", rel: "noopener", class: "small ellipsis" }, host(c.homepage)) : h("span"));
    fill(list,
      h("div", { class: "catrow head" }, h("span", null, "Source"), h("span", { title: "Metres of ground per pixel at the service's best; smaller is sharper. 7–10 cm shows pavers and sprinkler heads, 30 cm a soft lawn edge, 60 cm a blur." }, "Sharpest"), h("span", null, "Licence"), h("span", null, "Checked at"), h("span", null, "More")),
      order.map((cc) => {
        const g = groups[cc];
        const split = new Set(g.map(own)).size > 1;          // a heading per state only where there are several
        return [
          h("div", { class: "catgroup" }, cc === "XX" ? null : h("span", { class: "cc" }, cc), h("b", null, regionName(cc)),
            g.some((c) => c.covers) ? colored(GREEN, "covers your yard") : null),
          g.map((c, i) => [split && (i === 0 || own(g[i - 1]) !== own(c)) ? h("div", { class: "catplace" }, placeLabel(c, own(c))) : null,
            row(c)]),
        ];
      }),
      rows.length ? null : h("p", { class: "muted pad" }, "Nothing matches. Search a state by name or code (Texas, TX) to see every source that covers it; the catalog may not have your county yet."));
    for (const b of filterBtns) { const on = b.dataset.f === ui.filter; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false"); }
  };
  const filterBtns = [["all", "All"], ["free", "Free"], ["key", "Needs a key"]].map(([f, label]) =>
    h("button", { type: "button", "data-f": f, onclick: () => { ui.filter = f; draw(); } }, label));
  const search = h("input", { class: "field", type: "search", placeholder: "State (Texas or TX), county, city or name", "aria-label": "Search the catalog",
                              oninput: (e) => { ui.q = e.target.value; draw(); } });
  const w = d.context?.world_layer;
  const box = h("div", { class: "imgdialogbox wide" },
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("h3", { id: "catdlg-title", style: "margin:0;font-size:18px" }, "Imagery catalog"),
      h("button", { type: "button", class: "btn slim", onclick: () => close() }, "Close")),
    h("p", { class: "muted small", style: "margin:0" }, "Every source this release knows, each checked by its contributor at a public landmark. Reading this list contacts none of them. ",
      "Sharpest is in cm of ground per pixel: 7–10 cm is very sharp (pavers, sprinkler heads), 15–20 cm sharp, 30 cm soft edges, 60 cm a blur."),
    h("div", { class: "row gap wrap" }, h("div", { style: "flex:1 1 260px" }, search),
      h("div", { class: "seg", role: "group", "aria-label": "Filter" }, filterBtns)),
    summary, list,
    w ? h("div", { class: "imgnote", style: "margin-top:0" }, h("p", null, h("b", null, `Zoomed-out layer: ${w.name}. `),
      `Worldwide, about ${fmtRes(w.native_m)}. ${w.license?.name || ""}. Shown only up to zoom ${w.max_zoom}, so it never sees a yard.`)) : null,
    h("div", { class: "row gap wrap" },
      h("button", { type: "button", class: "btn slim", onclick: () => { close(); openWizard(d); } }, "Not here? Add a source"),
      h("span", { class: "muted small" }, "Developers: one JSON file per source under catalog/sources/; catalog/README.md explains the rest.")));
  close = dialog(box, "catdlg-title");
  draw();
  search.focus();
}

/* ------------------------------------------------------- add a source */
/* Paste an address → the server asks it what it serves (no location) → review the draft → try it at the yard (the
   first request that carries the yard, made because the user asks) → save → optionally export it as a catalog file
   for a pull request. The form's fields come from the server's kinds, so a new protocol gets its form for free. */
const LICENCES = {
  "public-domain": { label: "Public domain", license: { id: "public-domain", name: "Public domain", commercial: true, credit: false } },
  "cc-by": { label: "Open data, CC BY 4.0", license: { id: "CC-BY-4.0", name: "Open data (CC BY 4.0); credit required", url: "https://creativecommons.org/licenses/by/4.0/", commercial: true, credit: true } },
  credit: { label: "Free, credit required", license: { id: "provider-terms", name: "Free, credit required", commercial: null, credit: true } },
  nc: { label: "Non-commercial use only", license: { id: "provider-terms", name: "Non-commercial use only", commercial: false, credit: true } },
  unknown: { label: "Not stated", license: { id: "unknown", name: "Not stated", commercial: null, credit: true } },
};
const licenceKey = (l) => !l ? "unknown" : l.id === "public-domain" ? "public-domain" : l.id === "CC-BY-4.0" ? "cc-by"
  : l.commercial === false ? "nc" : l.id === "unknown" ? "unknown" : "credit";

function download(name, text) {
  const href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = h("a", { href, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10000);
}

function addSourceRow(d) {
  return h("div", { class: "imgadd" },
    h("button", { type: "button", class: "btn slim", onclick: () => openWizard(d) }, "+ Add a source"),
    h("span", { class: "muted small" }, "For a county or state service the catalog does not have yet (often 7–15 cm where the free national ones stop at 30–60 cm). Paste its address; the panel works out the rest. ",
      "Not sure where to look? ", h("a", { href: "#help/imagery-finding" }, "Finding imagery"), " or an ", h("a", { href: "#settings/assistant" }, "AI assistant"), " can search for you."));
}

function openWizard(d, saved) {
  const st = { step: saved ? "export" : "paste", url: "", busy: "", error: "", disc: null, entry: saved ? { ...saved } : null,
               tried: null, saved: saved || null, outlines: null, exp: null,
               ex: { country: "", region: saved?.region || "", place: "", lon: "", lat: "", cov: "ref", ref: "", geojson: "", world: false } };
  const covering = d.sources.filter((c) => c.covers && c.country && c.country !== "XX" && c.origin === "bundled");
  st.ex.country = covering[0]?.country || "";
  st.ex.ref = (d.sources.find((c) => c.id === d.current && c.origin === "bundled" && c.coverage_ref) || covering.find((c) => c.coverage_ref) || {}).coverage_ref || "";
  let close = () => {};
  const box = h("div", { class: "imgdialogbox wide" });
  const kindOf = (name) => (d.kinds || []).find((k) => k.name === name);
  const busy = async (label, run) => {
    st.busy = label; st.error = ""; draw();
    try { await run(); } catch (e) { st.error = e.message; }
    st.busy = ""; draw();
  };
  const field = (label, input, hint, id) => h("div", { class: "formrow" },
    h("label", { class: "muted small", for: id }, label), input, hint ? h("span", { class: "tiny muted" }, hint) : null);
  const nav = (...buttons) => h("div", { class: "row gap wrap", style: "margin-top:6px" }, ...buttons);
  const button = (label, onclick, primary, disabled) => h("button", { type: "button", class: `btn slim${primary ? " pri" : ""}`, onclick, disabled: disabled || st.busy ? true : null }, label);

  function stepsBar() {
    const order = { paste: 1, review: 2, try: 3, saved: 4, export: 4 };
    const at = order[st.step];
    return h("ol", { class: "wizsteps", "aria-label": "Steps" },
      ["Paste", "Review", "Try at your yard", st.saved ? "Saved" : "Save"].map((label, i) =>
        h("li", { class: i + 1 === at ? "on" : i + 1 < at ? "done" : null, "aria-current": i + 1 === at ? "step" : null },
          h("span", { class: "mono" }, String(i + 1)), label)));
  }

  function pasteStep() {
    const lookUp = () => {
      if (st.busy) return;
      if (!st.url.trim()) { url.focus(); return; }
      busy("Asking it what it serves…", async () => {
        st.disc = await send("POST", "/api/imagery/discover", { url: st.url });
        st.entry = { ...st.disc.entry };
        st.tried = null;
        st.step = "review";
      });
    };
    const go = button("Look it up", lookUp, true, !st.url.trim());
    const url = h("input", { class: "field", type: "url", id: "wiz-url", value: st.url, autocomplete: "off",
      placeholder: "https://…/ImageServer, https://…/wms or https://…/{z}/{x}/{y}.jpg",
      oninput: (e) => { st.url = e.target.value; go.disabled = !st.url.trim() || !!st.busy; },
      onkeydown: (e) => { if (e.key === "Enter") lookUp(); } });
    setTimeout(() => url.focus(), 0);
    return [field("Service address", url, "An ArcGIS ImageServer or MapServer, a WMS, or a tile address with {z}, {x} and {y}. The panel asks only this address what it serves. It sends no location.", "wiz-url"),
      nav(go)];
  }

  function reviewStep() {
    const e = st.entry, disc = st.disc || { notes: [], guessed: [], choices: {} };
    const k = kindOf(e.kind);
    const guessed = new Set(disc.guessed || []);
    const set = (name, cast) => (ev) => {
      const v = ev.target.value;
      e[name] = cast ? cast(v) : v;
      st.tried = null;
      if (name === "url" && e.kind === "tiles") e.max_zoom = 21;      // a new address: look for its deepest zoom again
    };
    const input = (name, attrs) => h("input", { class: "field", id: `wiz-${name}`, value: e[name] ?? "", oninput: set(name, attrs?.type === "number" ? Number : null), ...attrs });
    const kindSelect = h("select", { class: "field", id: "wiz-kind", onchange: (ev) => { e.kind = ev.target.value; st.tried = null; draw(); } },
      (d.kinds || []).map((kk) => h("option", { value: kk.name, selected: kk.name === e.kind || null }, kk.label)));
    const kindFields = Object.entries(k?.fields || {}).map(([name, f]) => {
      const id = `wiz-${name}`;
      const layers = name === "layers" ? disc.choices?.layers : null;
      let el;
      if (layers && layers.length) {
        el = h("select", { class: "field", id, onchange: (ev) => {
          e.layers = ev.target.value;
          st.tried = null;
          const l = layers.find((x) => x.name === e.layers);
          if (l?.native_m) e.native_m = Math.round(l.native_m * 10000) / 10000;
          if (l?.bbox) disc.extent = l.bbox;
          draw();
        } }, layers.map((l) => h("option", { value: l.name, selected: l.name === e.layers || null }, l.title === l.name ? l.name : `${l.title} (${l.name})`)));
      } else if (f.choices.length) {
        el = h("select", { class: "field", id, onchange: (ev) => { e[name] = f.type === "integer" ? Number(ev.target.value) : ev.target.value; st.tried = null; } },
          f.choices.map((c) => h("option", { value: c, selected: String(c) === String(e[name] ?? f.default) || null }, String(c))));
      } else {
        el = input(name, f.type === "string" ? { placeholder: f.default ?? "" } : { type: "number", step: f.type === "integer" ? 1 : "any", placeholder: f.default ?? "" });
      }
      return field(f.label || name, el, f.help, id);
    });
    const lic = h("select", { class: "field", id: "wiz-licence", onchange: (ev) => { e.license = { ...LICENCES[ev.target.value].license }; } },
      Object.entries(LICENCES).map(([key, v]) => h("option", { value: key, selected: key === licenceKey(e.license) || null }, v.label)));
    return [
      h("div", { class: "badges" }, badge(`Found: ${disc.kind_label || k?.label || e.kind}`, "free"),
        disc.choices?.layers?.length > 1 ? badge(`${disc.choices.layers.length} layers`) : null),
      (disc.notes || []).length ? h("ul", { class: "wiznotes" }, disc.notes.map((n) => h("li", null, n))) : null,
      h("div", { class: "formgrid" },
        field("Name", input("name"), null, "wiz-name"),
        field("Type", kindSelect, null, "wiz-kind"),
        field("Metres per pixel", input("native_m", { type: "number", step: "any", min: "0.001" }),
          guessed.has("native_m") ? "A guess: the preview will tell." : null, "wiz-native_m"),
        field("Season and date", input("vintage", { placeholder: "e.g. spring 2024" }), null, "wiz-vintage"),
        field("Credit shown on the map", input("attribution"), guessed.has("attribution") ? "From the address: check what the provider asks for." : null, "wiz-attribution"),
        field("Licence", lic, "What the provider's terms say; \"not stated\" is fine for your own use.", "wiz-licence"),
        ...kindFields),
      field("Address", input("url", { class: "field mono" }), null, "wiz-url"),
      h("p", { class: "muted small", style: "margin:0" }, "Coverage: wherever you use it. A source you add is asked about your yard only."),
      nav(button("Back", () => { st.step = "paste"; draw(); }),
          button("Next: try it at my yard", () => { st.step = "try"; draw(); }, true))];
  }

  function tryStep() {
    const e = st.entry, t = st.tried;
    const inUse = d.sources.find((c) => c.id === d.current);
    const fetchIt = () => busy("Fetching a 40 m square at your yard…", async () => {
      st.tried = await send("POST", "/api/imagery/try", { entry: e });
      Object.assign(e, st.tried.found || {});
    });
    const save = (use) => busy("Saving…", async () => {
      const r = await send("POST", "/api/imagery/sources", { entry: e, use });
      st.saved = (r && r.id) ? { ...e, id: r.id } : st.saved;
      st.step = "saved";
      if (r.warning) toast(r.warning); else toast(use ? `Now showing ${e.name}. It stays until you change it.` : `${e.name} saved.`);
      if (use) await loadImagery();
      await refreshPicker();
    });
    return [
      h("div", { class: "wiznotice" }, "This is the first request that carries your yard's location. It goes to ", h("b", null, host(e.url)), " and nowhere else."),
      h("div", { class: "row gap wrap", style: "align-items:flex-start" },
        h("div", { class: "wizpreview" }, t?.ok ? h("img", { src: t.image, alt: `Your yard as ${e.name} shows it` })
          : h("div", { class: "imgthumb empty" }, st.busy ? st.busy : t ? "No image" : "No request sent yet.")),
        h("div", { style: "flex:1 1 260px;min-width:0" },
          t ? (t.ok ? [h("p", { class: "inuse", style: "margin:0 0 6px" }, "Has imagery at your yard"),
                drow("Resolution", `${fmtRes(e.native_m)} per pixel`),
                t.found?.max_zoom ? drow("Deepest zoom with tiles here", String(t.found.max_zoom)) : null,
                drow("Sample", `40 m · ${t.size[0]} px · ${t.seconds} s`),
                inUse ? drow("In use now", `${inUse.name}, ${fmtRes(inUse.native_m)}`) : null]
            : [h("p", { style: `color:${CORAL};margin:0 0 6px` }, `It did not work: ${t.detail}`),
               h("p", { class: "muted small" }, "Go back to change the address, the layer or the type.")]) : null,
          button(t ? "Fetch again" : "Fetch a preview", fetchIt, !t))),
      nav(button("Back", () => { st.step = "review"; draw(); }),
          button("Save", () => save(false), false, !t?.ok), button("Save and use", () => save(true), true, !t?.ok))];
  }

  function savedStep() {
    const s = st.saved;
    return [
      h("p", { style: "margin:0" }, h("b", { style: `color:${GREEN}` }, "Saved. "), `${s.name} is now in Map imagery, marked "added by you". It stays on this server.`),
      nav(button("Export for sharing", () => { st.step = "export"; draw(); }), button("Done", () => close(), true))];
  }

  function exportStep() {
    const ex = st.ex, s = st.saved;
    if (!st.outlines) {
      st.outlines = [];
      getJSON("/api/imagery/outlines").then((r) => { st.outlines = r.outlines; draw(); }).catch(() => {});
    }
    const inp = (name, attrs) => h("input", { class: "field", id: `exp-${name}`, value: ex[name], oninput: (e) => { ex[name] = e.target.value; }, ...attrs });
    const radio = (value, label, extra) => h("label", { class: "row gap small", style: "align-items:flex-start" },
      h("input", { type: "radio", name: "expcov", value, checked: ex.cov === value || null, onchange: () => { ex.cov = value; draw(); } }),
      h("span", null, label, extra || null));
    const extent = st.disc?.extent;
    const lookExtent = () => busy("Asking the service for its extent…", async () => {
      const r = await send("POST", `/api/imagery/sources/${s.id}/extent`);
      st.disc = { ...(st.disc || {}), extent: r.extent };
      if (!r.extent) throw new Error("The service does not say where it has imagery: choose a shared outline or paste one.");
    });
    const make = () => busy("Making the file…", async () => {
      if (String(ex.lon).trim() === "" || String(ex.lat).trim() === "") throw new Error("Give the landmark's longitude and latitude.");
      const coverage = ex.cov === "ref" ? { ref: ex.ref } : ex.cov === "extent" ? { extent } : ex.cov === "geojson" ? { geojson: JSON.parse(ex.geojson || "null") } : { world: true };
      st.exp = await send("POST", `/api/imagery/sources/${s.id}/export`, {
        country: ex.country, region: ex.region, place: ex.place, landmark: [Number(ex.lon), Number(ex.lat)], coverage, worldwide: ex.world });
    });
    return [
      h("p", { class: "muted", style: "margin:0" }, `Turns ${s.name} into a catalog file other people can use. Keys, your yard and anything local are left out.`),
      h("div", { class: "formgrid" },
        field("Country (two letters)", inp("country", { maxlength: 2, style: "text-transform:uppercase" }), "XX for a worldwide service.", "exp-country"),
        field("Region, as a user reads it", inp("region", { placeholder: "Example County, Maryland" }), null, "exp-region"),
        field("A public landmark it covers", inp("place", { placeholder: "County Courthouse" }), null, "exp-place"),
        field("Its longitude", inp("lon", { type: "number", step: "any", placeholder: "-76.5000" }), null, "exp-lon"),
        field("Its latitude", inp("lat", { type: "number", step: "any", placeholder: "39.0000" }), null, "exp-lat")),
      h("p", { class: "tiny muted", style: "margin:-4px 0 0" }, "Never your house: at least 2 km from your yard. Reviewers render the source there to check it."),
      h("fieldset", { class: "wizcov" }, h("legend", { class: "muted small" }, "Where it has imagery"),
        radio("ref", "A shared outline: ", h("select", { class: "field slim", "aria-label": "Shared outline", onchange: (e) => { ex.ref = e.target.value; ex.cov = "ref"; draw(); } },
          h("option", { value: "" }, "choose…"), (st.outlines || []).map((o) => h("option", { value: o.ref, selected: o.ref === ex.ref || null }, `${o.name} (${o.ref})`)))),
        radio("extent", "The service's own extent", extent ? h("span", { class: "muted mono tiny" }, ` ${extent.map((v) => v.toFixed(2)).join(", ")}`)
          : [" ", h("button", { type: "button", class: "linkbtn", onclick: lookExtent }, "look it up")]),
        radio("geojson", "An outline you paste (GeoJSON)", ex.cov === "geojson" ? h("textarea", { class: "field", rows: 4, "aria-label": "GeoJSON outline",
          oninput: (e) => { ex.geojson = e.target.value; } }, ex.geojson) : null),
        radio("world", "The whole world", ex.cov === "world" ? h("label", { class: "row gap tiny" }, h("input", { type: "checkbox", checked: ex.world || null,
          onchange: (e) => { ex.world = e.target.checked; } }), "This service really covers the whole world") : null)),
      h("p", { class: "tiny muted", style: "margin:0" }, "Outlines smaller than 20 by 10 km are refused, so a yard can never be shared as one."),
      nav(button("Make the file", make, true)),
      st.exp ? [
        (st.exp.notes || []).length ? h("ul", { class: "wiznotes" }, st.exp.notes.map((n) => h("li", null, n))) : null,
        st.exp.files.map((f) => h("div", { class: "wizfile" },
          h("div", { class: "row gap wrap", style: "justify-content:space-between" }, h("span", { class: "mono small" }, f.path),
            h("span", { class: "row gap" },
              h("button", { type: "button", class: "btn slim", onclick: async () => toast(await copyText(f.text) ? "Copied." : "Copy did not work here: use Download.") }, "Copy"),
              h("button", { type: "button", class: "btn slim", onclick: () => download(f.path.split("/").pop(), f.text) }, "Download"))),
          h("pre", null, f.text))),
        h("p", { class: "muted small", style: "margin:0" }, "Then: add the file", st.exp.files.length > 1 ? "s" : "", " to the repository, run ",
          h("span", { class: "mono" }, `python -m app.imagery verify ${st.exp.files[0].path} --live`), ", and open a pull request with the image it makes. catalog/README.md explains the rest.")] : null,
      st.exp ? nav(button("Done", () => close(), true)) : null];
  }

  function draw() {
    const body = { paste: pasteStep, review: reviewStep, try: tryStep, saved: savedStep, export: exportStep }[st.step]();
    fill(box,
      h("div", { class: "row gap", style: "justify-content:space-between" },
        h("h3", { id: "wiz-title", style: "margin:0;font-size:18px" }, st.step === "export" ? `Export ${st.saved?.name || ""} for sharing` : "Add a source"),
        h("button", { type: "button", class: "btn slim", onclick: () => close() }, "Close")),
      st.step === "export" && saved ? null : stepsBar(),
      h("div", { class: "wizbody" }, body),
      st.busy ? h("p", { class: "small", style: `color:${AMBER};margin:0`, role: "status" }, st.busy) : null,
      st.error ? h("p", { class: "small", style: `color:${CORAL};margin:0`, role: "alert" }, st.error) : null);
  }

  close = dialog(box, "wiz-title");
  draw();
}
