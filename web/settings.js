/* Settings: one section at a time, chosen from the list on the left. Each section is a stack of rows: the setting's
   name and what it does on the left, its control on the right. A change is saved as soon as it is made.
   Loaded after ops.js and imagery.js and uses their helpers (h, fill, dot, mono, colored, when, dur, clock, toast,
   getJSON, putJSON, postJSON, checkList, startDraw, ringAreaM2, validPolys, shortCam, drawPicker, loadImagery,
   fmtRes). Reads the panel's own state and PUTs the panel's own settings; nothing here goes to the robot. */
"use strict";

const SECTIONS = [
  ["setup", "Set up", "A guided first run: each setting in order, with what it is for"],
  ["robot", "Robot", "The Yarbo cloud sign-in that connects the panel to the robot"],
  ["safety", "Safety", "The switches that let the panel act on the robot"],
  ["rules", "Run rules", "What must be true for a run to start, and what stops one"],
  ["lawn", "Lawn conditions", "How the weather station decides whether the grass is dry"],
  ["cameras", "Cameras", "Which Frigate cameras see the yard, and the ground each one covers"],
  ["imagery", "Map imagery", "The aerial photo under the robot's map"],
  ["ha", "Home Assistant", "The connection, presence, and the entities this panel publishes"],
  ["alerts", "Notifications", "Where alerts go"],
  ["assistant", "AI assistant", "Optional: talk to the panel through an AI assistant over MCP"],
  ["access", "Access", "The admin password, sessions, and API tokens for other programs"],
  ["panel", "This panel", "Recording cadence, retention, and how this instance runs"],
  ["data", "Raw data", "What the robot and the panel report, as JSON"],
];
const ACTIONS = [["pause", "Pause"], ["dock", "Return to dock"], ["notify", "Notify only"], ["ignore", "Ignore"]];
const HA_ENTITIES = ["sensor.yarbo_panel_state", "sensor.yarbo_panel_battery", "sensor.yarbo_panel_rtk", "sensor.yarbo_panel_lawn",
                     "sensor.yarbo_panel_local_link", "sensor.yarbo_panel_last_run", "binary_sensor.yarbo_panel_running"];
const HA_DEFAULT_URL = "http://homeassistant.local:8123";
const setUI = { section: "robot", drawn: null, data: null, ticket: 0 };

/* ------------------------------------------------------------- pieces */
const srow = (name, help, ...ctl) => h("div", { class: "srow" },
  h("div", { class: "stext" }, h("div", { class: "sname" }, name), help ? h("p", { class: "shelp" }, help) : null),
  h("div", { class: "sctl" }, ...ctl));
const sstack = (name, help, ...ctl) => { const el = srow(name, help, ...ctl); el.classList.add("stack"); return el; };
const ssub = (row) => { row.classList.add("sub"); return row; };
const scard = (title, ...content) => h("div", { class: "scard" }, title ? h("p", { class: "label" }, title) : null, ...content);
const snote = (...text) => h("p", { class: "shelp top" }, ...text);
const sfoot = (...text) => h("p", { class: "shelp foot" }, ...text);
const slink = (href, text) => h("a", { href }, text);
const sbtn = (label, onclick, cls = "btn slim") => h("button", { type: "button", class: cls, onclick }, label);
const sstate = (color, text) => h("span", { class: "state" }, dot(color), text);
const disable = (el, off) => { el.disabled = Boolean(off); return el; };
// The switches that let the panel act on the robot are switched on only by typing ENABLE.
const enableWithPrompt = (key, text, msg) => { if (prompt(text) === "ENABLE") saveSetting(key, true, msg); };

function switchCtl(name, on, onchange, key) {
  return h("label", { class: "switch" },
    h("input", { type: "checkbox", role: "switch", "aria-label": name, "data-key": key || null, checked: on || null,
                 onchange: (e) => onchange(e.target.checked) }),
    h("span", { class: "track", "aria-hidden": "true" }));
}

// A number with its unit. Blank is allowed only when `blank` (the placeholder for "unset") is given.
function numCtl({ key, value, min, max, step, unit, blank, onsave }) {
  const input = h("input", { class: "field", type: "number", "data-key": key, value: value ?? "", min, max, step: step ?? 1,
    placeholder: blank || null, "aria-label": key,
    onchange: (e) => {
      const t = e.target, v = t.value.trim();
      if (t.validity.badInput) { toast("That is not a number."); t.value = value ?? ""; return; }
      if (v === "") {
        if (blank !== undefined) { if (value !== null && value !== undefined) onsave(null); }
        else t.value = value ?? "";
        return;
      }
      const n = Number(v);
      if (Number.isFinite(n) && n !== value) onsave(n);
    } });
  return h("span", { class: "unitfield" }, input, unit ? h("span", { class: "unit" }, unit) : null);
}

function selCtl({ key, value, options, onsave }) {
  return h("select", { class: "field", "data-key": key, "aria-label": key, onchange: (e) => onsave(e.target.value) },
    options.map(([v, label]) => h("option", { value: v, selected: v === value || null }, label)));
}

function textCtl({ key, value, placeholder, type, onsave }) {
  return h("input", { class: "field", type: type || "text", "data-key": key, "aria-label": key, value: value || "",
    placeholder: placeholder || null, autocomplete: "off", spellcheck: "false",
    onchange: (e) => { const v = e.target.value.trim(); if (v !== (value || "")) onsave(v || null); } });
}

async function saveSetting(key, value, msg) {
  try { await putJSON(`/api/settings/${key}`, value); toast(msg || "Saved."); }
  catch (e) { toast(e.message); }
  await renderSettings();
}

/* Integrations: the user's value over the environment's. PUT /api/integrations/<group> takes a partial object;
   null for a field hands it back to the environment. */
async function saveIntegration(group, values, msg) {
  if (group === "home_assistant") setUI.haSensors = null;          // read HA's entities again with the new connection
  try {
    const r = await fetch(`/api/integrations/${group}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `${r.status}`);
    toast(data.note ? `${msg || "Saved."} ${data.note[0].toUpperCase()}${data.note.slice(1)}.` : (msg || "Saved."));
  } catch (e) { toast(e.message); await renderSettings(); return false; }
  await renderSettings();
  return true;
}

async function testIntegration(group, values, out, button) {
  if (button) button.disabled = true;
  fill(out, h("span", { class: "muted" }, "Testing…"));
  try {
    const r = await fetch(`/api/integrations/${group}/test`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values || {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `${r.status}`);
    fill(out, sstate(data.ok ? GREEN : CORAL, data.detail));
  } catch (e) { fill(out, sstate(CORAL, e.message)); }
  if (button) button.disabled = false;
}

// One line under a field: set here (with a way back to the environment's value), from the environment, or unset.
function whence(group, field, f) {
  if (!f) return null;
  const name = mono(f.env_name);
  const secret = f.env_set !== undefined;
  const envHas = secret ? f.env_set : !(f.env === null || f.env === undefined || f.env === "");
  let text;
  const back = h("button", { type: "button", class: "linkbtn", onclick: () => saveIntegration(group, { [field]: null }, "Back to the environment's value.") }, "use that instead");
  if (f.source === "setting") {
    const how = f.value === false || (secret && f.set === false) ? "Switched off here" : "Set here";
    text = envHas
      ? [how, ". The environment (", name, ") has ", secret ? "its own" : mono(String(f.env)), ": ", back, "."]
      : [how, ". ", name, " is not set in the environment."];
  } else if (f.source === "env") {
    text = ["From the environment (", name, "). A value here takes over without a restart."];
  } else if (f.source === "default") {
    text = ["The built-in default. A value here, or ", name, " in the environment, changes it."];
  } else {
    text = ["Not set. ", name, " in the environment would be the default."];
  }
  return h("p", { class: "shelp src" }, ...text);
}
const intRow = (name, help, group, field, f, ...ctl) => {
  const row = srow(name, help, ...ctl);
  const w = whence(group, field, f);
  if (w) $(".stext", row).append(w);
  return row;
};
const intStack = (name, help, group, field, f, ...ctl) => { const row = intRow(name, help, group, field, f, ...ctl); row.classList.add("stack"); return row; };
const ival = (f) => (f && f.value !== null && f.value !== undefined && f.value !== false ? f.value : "");
// What an emptied field means: switched off when the environment would otherwise bring a value back, else unset.
const envHas = (f) => !!f && (f.env_set !== undefined ? f.env_set : !(f.env === null || f.env === undefined || f.env === ""));
const emptied = (f) => (envHas(f) ? false : null);

/* ------------------------------------------------------------- view */
async function settingsData() {
  const empty = { status: { enabled: false }, cameras: [], yard: [], polygons: {} };
  const [st, s, cams, imgs, imv, about, conds, setup, access] = await Promise.all([
    getJSON("/api/settings"), getJSON("/api/status"), getJSON("/api/frigate/cameras").catch(() => empty),
    getJSON("/api/imagery/sources").catch(() => null), getJSON("/api/imagery").catch(() => null),
    getJSON("/api/about").catch(() => null), getJSON("/api/conditions").catch(() => null),
    getJSON("/api/setup/status").catch(() => null),
    Promise.all([getJSON("/api/auth/status"), getJSON("/api/auth/tokens")]).then(([a, t]) => ({ ...a, tokens: t.tokens })).catch(() => null)]);
  return { st, s, cams, imgs, imv, about, conds, setup, access };
}

async function renderSettings(section, auto = false) {
  const panel = $("#p-settings");
  if (!auto) setUI.dirty = false;                                  // a save or a navigation: what is shown is current again
  setUI.at = Date.now();
  if (section && SECTIONS.some(([id]) => id === section)) setUI.section = section;
  if (!$(".settings", panel)) {
    fill(panel, h("div", { class: "settings" },
      h("details", { class: "sfold", open: true }, h("summary", null, "Sections", h("span", { class: "sfoldcur" })),
        h("nav", { class: "snav", "aria-label": "Settings sections" })),
      h("section", { class: "ssec" })));
  }
  // On a phone the list is a fold: open on #settings, shut once a section is chosen. Only a navigation (showView
  // passes the sub-page or null) sets it; a save or a refresh leaves it as the user left it.
  if (section !== undefined) $(".sfold", panel).open = !section || !phoneNav();
  if (setUI.data) drawSettings(setUI.data, false);               // at once, from the last fetch; refreshed below
  else { drawSettingsNav(null); fill($(".ssec", panel), h("p", { class: "muted pad" }, "Loading…")); }
  const ticket = ++setUI.ticket;
  let d;
  try { d = await settingsData(); }
  catch (e) {
    if (ticket !== setUI.ticket || state.view !== "settings") return;
    if (setUI.data) toast(`Could not refresh: ${e.message}`);
    else fill($(".ssec", panel), h("div", { class: "shead" }, h("h2", null, "Settings"), h("p", { class: "muted" }, e.message)),
              sbtn("Try again", () => renderSettings(), "btn"));
    return;
  }
  if (ticket !== setUI.ticket || state.view !== "settings") return;   // a newer render or another view took over
  if (auto && (setUI.dirty || $("#p-settings .ssec details.sadvanced[open]"))) return;   // typing or Advanced opened meanwhile
  setUI.data = d;
  drawSettings(d, true);
}

// While something the open page waits for is on its way (Robot / the guide's robot step: the cloud signing in and
// the robot's serial; Map imagery and the guide's imagery step: the yard imagery; Lawn conditions and
// the guide's weather step: the first Tempest reading), the page re-reads every 3 s so nobody has to reload, backing
// off to 15 s after 2 minutes and 60 s after 10. Robot and the guide's robot step also re-read every 15 s.
// A field typed in and not saved, or an open Advanced fold, is never redrawn: refreshing waits for the next save
// or navigation.
function settingsWaitsFor(d) {
  const t = d.s.transport?.robot || {}, cloud = d.s.ops?.cloud || {}, tp = d.s.tempest || {};
  const sec = setUI.section, step = sec === "setup" ? setUI.step : null;
  const signingIn = ["connecting", "reconnecting"].includes(cloud.state);
  if (sec === "robot" || step === "robot") return Boolean(signingIn || (cloud.state === "connected" && !d.s.sn));
  if (sec === "imagery" || step === "imagery") return Boolean(t.connected && d.imgs && !d.imgs.yard?.source);
  if (sec === "lawn" || step === "weather") return Boolean(tp.listening_port && (tp.last_obs_age_s === null || tp.last_obs_age_s === undefined));
  return false;
}
document.addEventListener("input", (e) => {
  if (e.target.closest?.("#p-settings .ssec") && !["checkbox", "radio", "range"].includes(e.target.type)) setUI.dirty = true;
});
setInterval(() => {
  if (state.view !== "settings" || document.hidden || !setUI.data || setUI.dirty) return;
  if ($("#p-settings .ssec details.sadvanced[open]")) return;     // an open Advanced fold stays open
  const waiting = settingsWaitsFor(setUI.data);
  if (!waiting) setUI.waitSince = null;
  else if (!setUI.waitSince) setUI.waitSince = Date.now();
  const steady = setUI.section === "robot" || (setUI.section === "setup" && setUI.step === "robot");
  const waited = waiting ? Date.now() - setUI.waitSince : 0;
  const every = waiting ? (waited < 120000 ? 3000 : waited < 600000 ? 15000 : 60000) : steady ? 15000 : 0;
  if (every && Date.now() - (setUI.at || 0) >= every) renderSettings(undefined, true);
}, 1000);

// Redraws the list and the open section. A save redraws while the user may already be typing in the next field, so
// whatever is focused keeps its text, caret and focus across the redraw.
function drawSettings(d, fresh) {
  const panel = $("#p-settings"), sec = $(".ssec", panel);
  const focusables = () => $$("a[href], button, input, select, textarea", sec);
  const active = document.activeElement, key = active?.dataset?.key || null;
  const typed = key && active.tagName === "INPUT" && !["checkbox", "range"].includes(active.type)
    ? { value: active.value, start: active.selectionStart, end: active.selectionEnd } : null;
  const slot = !key && active && sec.contains(active) ? focusables().indexOf(active) : -1;   // a link or a plain button
  drawSettingsNav(d);
  drawSection(d, fresh);
  const el = key ? $(`[data-key="${key}"]`, panel) : slot >= 0 ? focusables()[slot] : null;
  if (!el) return;
  if (typed) {
    el.value = typed.value;
    try { if (typed.start !== null) el.setSelectionRange(typed.start, typed.end); } catch { /* number inputs keep no selection */ }
  }
  el.focus({ preventScroll: true });
}

// At 700 px or less the section list folds away under one row (style.css); wider, the fold is always open.
const phoneNav = () => window.matchMedia("(max-width: 700px)").matches;
window.matchMedia("(max-width: 700px)").addEventListener("change", (e) => {
  const fold = $("#p-settings .sfold");
  if (fold && !e.matches) fold.open = true;          // wider, the summary is hidden: a shut fold would hide the list
});

function drawSettingsNav(d) {
  $("#p-settings .sfoldcur").textContent = `· ${SECTIONS.find(([x]) => x === setUI.section)[1]}`;
  fill($("#p-settings .snav"), SECTIONS.map(([id, name]) => {
    const [color, caption] = d ? sectionStatus(id, d) : [MUTED, ""];
    const on = id === setUI.section;
    return h("a", { class: `sitem${on ? " on" : ""}`, href: `#settings/${id}`, "aria-current": on ? "page" : null,
                    onclick: () => { if (phoneNav()) $("#p-settings .sfold").open = false; } },  // also the open one
      dot(color), h("span", { class: "stitle" }, name), h("span", { class: "scap" }, caption));
  }));
}

const SECTION_BODY = { setup: secSetup, robot: secRobot, assistant: secAssistant, safety: secSafety, rules: secRules, lawn: secLawn, cameras: secCameras,
                       imagery: secImagery, ha: secHA, alerts: secAlerts, access: secAccess, panel: secPanel, data: secData };

function drawSection(d, fresh) {
  const sec = $("#p-settings .ssec");
  const [id, name, blurb] = SECTIONS.find(([x]) => x === setUI.section);
  sec.classList.toggle("full", id === "imagery" || id === "data" || (id === "setup" && setUI.step === "imagery"));
  fill(sec, h("div", { class: "shead" }, h("h2", null, name), h("p", { class: "muted" }, blurb)), SECTION_BODY[id](d));
  if (setUI.drawn !== id) { $("#p-settings").scrollTop = 0; setUI.drawn = id; }
  if (id === "data") { if (state.dataCache && setUI.dataLoaded === setUI.dataSrc) drawTree(); else loadDataTree(); }
}

// One dot and one line per section, so the list doubles as a checklist of what still needs setting up.
function sectionStatus(id, d) {
  const s = d.s, o = s.ops || {}, st = d.st;
  switch (id) {
    case "setup": {
      const su = d.setup;
      if (!su) return [MUTED, ""];
      return [su.required_done ? GREEN : AMBER, `${su.done} of ${su.steps.length} steps done${su.required_done ? "" : " · start here"}`];
    }
    case "robot": {
      const cloud = o.cloud || {};
      if (cloud.demo) return [GREEN, "Demo robot (no Yarbo account)"];
      if (cloud.state === "connected") return s.sn ? [GREEN, "Connected through the Yarbo cloud"] : [AMBER, "Waiting for the robot's serial"];
      if (["connecting", "reconnecting"].includes(cloud.state)) return [AMBER, `Yarbo cloud ${cloud.state}`];
      return [CORAL, cloud.state === "needs login" ? "Sign in to Yarbo cloud again" : "Sign in to Yarbo cloud"];
    }
    case "assistant": {
      const m = st.mcp || {};
      if (m.enabled) return [GREEN, `Connected · token …${m.hint || ""}${st.mcp_control_enabled ? " · commands on" : ""}`];
      return [MUTED, "Optional · not connected"];
    }
    case "safety":
      return [st.control_enabled || st.map_write_enabled ? AMBER : MUTED,
              `Control ${st.control_enabled ? "ON" : "off"} · map writing ${st.map_write_enabled ? "ON" : "off"}`];
    case "rules": {
      const g = st.gates;
      return [MUTED, `Battery ≥ ${g.min_battery_pct} %${g.require_dry ? " · dry lawn" : ""}${g.presence_required ? " · someone home" : ""}`];
    }
    case "lawn": {
      const t = s.tempest || {}, c = d.conds;
      const heard = t.last_obs_age_s !== null && t.last_obs_age_s !== undefined;
      if (c && c.state) return [STATE_COLOR[c.state] || MUTED, heard ? `${c.state} · Tempest ${dur(t.last_obs_age_s)} ago` : `${c.state} · from Home Assistant`];
      if (!heard) return [CORAL, "No weather observations"];
      return [MUTED, `No reading yet · Tempest ${dur(t.last_obs_age_s)} ago`];
    }
    case "cameras": {
      const cams = d.cams;
      if (!cams.status?.enabled) return [MUTED, "Frigate not set up"];
      if (cams.status.error) return [CORAL, cams.status.error];
      const yard = cams.yard || [], drawn = yard.filter((c) => ringAreaM2(cams.polygons[c]) >= 1).length;
      if (!yard.length) return [AMBER, `${cams.cameras.length} cameras, none picked yet`];
      return [drawn < yard.length ? AMBER : GREEN, `${yard.length} yard camera${yard.length === 1 ? "" : "s"} · ${drawn} with coverage`];
    }
    case "imagery": {
      const cur = d.imgs?.yard?.source;
      return cur ? [GREEN, `${cur.name} · ${fmtRes(cur.m_per_px)}`] : [AMBER, "No imagery yet"];
    }
    case "ha": {
      const ha = s.ha || {};
      return !ha.enabled ? [MUTED, "Not set up"] : ha.error ? [CORAL, ha.error] : [GREEN, st.presence_entity ? "Connected · presence set" : "Connected"];
    }
    case "alerts": {
      const n = o.notify || {};
      const ch = [n.ha_service ? (n.ha ? "HA app" : "HA app (not connected)") : null, n.webhook ? "webhook" : null].filter(Boolean);
      return ch.length ? [n.ha_service && !n.ha ? AMBER : GREEN, ch.join(" + ")] : [MUTED, "Events only"];
    }
    case "access": {
      const a = d.access;
      if (!a) return [MUTED, ""];
      if (a.disabled) return [CORAL, "Sign-in is off (PANEL_AUTH)"];
      return [GREEN, `Password set${a.tokens?.length ? ` · ${a.tokens.length} API token${a.tokens.length > 1 ? "s" : ""}` : ""}`];
    }
    case "panel":
      return [MUTED, s.demo ? "Demo data" : `Up ${dur(s.uptime_s)}`];
    case "data":
      return [MUTED, "JSON inspector"];
  }
  return [MUTED, ""];
}

/* ------------------------------------------------------------- set up (first-run wizard) */
/* Every step is the same controls as its own section (so nothing is set up two ways), with a line on what it is for
   and Back / Next. Done comes from GET /api/setup/status, read from what the panel already knows; the wizard stores
   nothing. It opens by itself on a fresh install (not signed in to Yarbo cloud, or no map yet), once per browser session. */
const SETUP_STEPS = {
  robot: ["Connect to the robot", "The panel reaches the robot through the Yarbo cloud, as the Yarbo app does. Sign in with your Yarbo account; the password goes straight to Yarbo's SDK and is never stored. The robot's serial comes from your account, and the map is read from the robot.",
          (d) => robotCards(d).cloud],
  assistant: ["Connect an AI assistant (optional)", "An assistant over MCP can read the panel and help you with it by conversation: check on the robot, tighten the run rules, or hunt down a sharper aerial map of your yard. Skip it if you don't use one.",
              (d) => secAssistant(d)],
  imagery: ["Pick the map imagery", "The aerial photo under the robot's map, in two parts: pick the photo, then line it up with the map.",
            (d) => setupImagery(d)],
  cameras: ["Cameras (optional)", "With Frigate, the yard cameras keep runs from starting while someone is out there, pause a run when a person or pet walks in, and follow the robot on Live. Switch on the cameras that see grass and draw what each covers.",
            (d) => secCameras(d)],
  ha: ["Home Assistant (optional)", "Home Assistant brings presence (is anyone home?), notifications to your phone, and weather or soil sensors. The panel also publishes its own state to HA. Paste a long-lived token from your HA profile.",
       (d) => secHA(d).slice(0, 2)],
  weather: ["Weather for Lawn conditions", "Dry, Damp or Wet decides whether a scheduled run starts. A Tempest station on your network is heard with no setup; Home Assistant sensors can stand in or add to it. The thresholds can wait: they are in Lawn conditions.",
            (d) => secLawn(d).slice(0, 2)],
  safety: ["Safety", "Four switches matter most before the robot is ever started from here. Change them here; the recommended values are the safe ones.",
           (d) => setupSafety(d)],
  alerts: ["Notifications", "Where alerts go: a robot error, a stuck robot, a run waiting for your yes, maintenance due. Pick the Home Assistant notify service for your phone or a webhook, then send a test.",
           (d) => secAlerts(d)[0]],
};

function secSetup(d) {
  const su = d.setup;
  if (!su) return [scard(null, snote("The setup status could not be read. Each section on the left still works on its own."))];
  const steps = su.steps.filter((x) => SETUP_STEPS[x.id]);
  if (!steps.some((x) => x.id === setUI.step) && setUI.step !== "end") setUI.step = (steps.find((x) => !x.done) || steps[0]).id;
  const i = steps.findIndex((x) => x.id === setUI.step);
  const go = (id) => { setUI.step = id; drawSettings(setUI.data, false); $("#p-settings").scrollTop = 0; };
  const rail = h("ol", { class: "setup-steps", "aria-label": "Steps" }, steps.map((x, n) => h("li", null,
    h("button", { type: "button", class: `setup-step${x.id === setUI.step ? " on" : ""}`, "aria-current": x.id === setUI.step ? "step" : null,
                  title: `${x.optional ? "Optional · " : ""}${x.detail}`, onclick: () => go(x.id) },
      h("span", { class: `setup-num${x.done ? " done" : ""}` }, x.done ? "✓" : String(n + 1)),
      h("span", { class: x.optional ? "muted" : null }, x.title)))),
    h("li", null, h("button", { type: "button", class: `setup-step${setUI.step === "end" ? " on" : ""}`, onclick: () => go("end") },
      h("span", { class: `setup-num${su.required_done ? " done" : ""}` }, su.required_done ? "✓" : "·"), h("span", null, "Finish"))));
  if (setUI.step === "end") {
    const left = steps.filter((x) => !x.done);
    return [rail, scard("All set?",
      snote(su.required_done ? "Every step you need is done. The optional ones can be done any time from their own sections."
                             : "Some steps you need are not done yet; the panel works without them, but they are listed here."),
      left.map((x) => srow(x.title, x.detail, sstate(x.optional ? MUTED : AMBER, x.optional ? "optional" : "not done"), sbtn("Go", () => go(x.id)))),
      srow("Next", ["Make a schedule in ", slink("#schedule", "Schedule"), ", or start a plan from ", slink("#live", "Live"), " once control is on."],
        sbtn("Open Live", () => { location.hash = "#live"; }, "btn pri")),
      sfoot("This guide stays under Settings → Set up. The same steps are in ", slink("#help/setup", "Help → Setting up"), "."))];
  }
  const x = steps[i], [title, why, body] = SETUP_STEPS[x.id];
  const next = steps[i + 1] ? steps[i + 1].id : "end";
  return [rail,
    h("div", { class: "scard setup-intro" },
      h("p", { class: "label" }, `Step ${i + 1} of ${steps.length}`),
      h("div", { class: "sname", style: "font-size:17px" }, title),
      h("p", { class: "shelp" }, why),
      h("p", { class: "shelp" }, sstate(x.done ? GREEN : x.optional ? MUTED : AMBER, x.done ? `Done: ${x.detail}` : `Now: ${x.detail}`))),
    body(d),
    h("div", { class: "setup-nav" },
      i > 0 ? sbtn("Back", () => go(steps[i - 1].id), "btn") : h("span"),
      h("span", { class: "row gap" },
        x.done ? null : sbtn(x.optional ? "Skip" : "Later", () => go(next), "btn"),
        sbtn(next === "end" ? "Finish" : "Next", () => go(next), x.done ? "btn pri" : "btn")))];
}

// Imagery in the guide: 1. pick the photo (the picker, with where the yard's location came from), 2. line it up.
function setupImagery(d) {
  const part = setUI.imgPart || "pick", cur = d.imgs?.yard?.source;
  const entry = cur && (d.imgs?.sources || []).find((x) => x.id === cur.id);
  const go = (p) => { setUI.imgPart = p; drawSettings(setUI.data, false); $("#p-settings").scrollTop = 0; };
  const tabs = h("div", { class: "subtabs setup-subtabs", role: "tablist" },
    [["pick", "1 · Pick the photo"], ["align", "2 · Line it up"]].map(([id, label]) =>
      h("button", { type: "button", role: "tab", class: "subtab", "aria-selected": String(part === id), "aria-current": part === id ? "step" : null, onclick: () => go(id) }, label)));
  const [picker, alignCard] = secImagery(d);
  if (part === "pick") {
    return [tabs,
      scard(null, h("p", { class: "shelp top" },
        d.s.transport?.robot?.connected
          ? `Your yard's location comes from the robot's own map${entry?.region ? ` (${entry.region})` : ""}, so only the sources that cover it are asked. They are tried and sorted sharpest first; Automatic keeps the sharpest free one.`
          : "Connect the robot first: its map says where your yard is, and only sources that cover it are asked."),
        h("div", { class: "row gap wrap", style: "margin:0 0 12px" }, sbtn("Next: line it up", () => go("align"), "btn pri"))),
      picker];
  }
  return [tabs,
    scard("Line the photo up with the robot's map",
      h("p", { class: "shelp top" }, "Aerial photos are often a metre or two off. On the map, check that the zone outlines sit on the lawn edges, paths and driveway in the photo."),
      h("ol", { class: "howto" },
        h("li", null, "Press ", h("b", null, "Align on the map"), ": the map opens with the align panel."),
        h("li", null, "Pick a spot on the robot's map (a path corner, the dock), then the same spot in the photo."),
        h("li", null, "One pair fixes the shift, two far apart fix the rotation too. Save, and you come back here.")),
      h("div", { class: "row gap wrap", style: "margin:4px 0 14px" },
        sbtn("Align on the map", beginAlign, "btn pri"),
        sbtn("Back to the photos", () => go("pick"), "btn"))),
    alignCard];
}

// The settings that matter before anything is started from here, changeable in place, each with its safe value.
function setupSafety(d) {
  const g = d.st.gates, on = d.st.control_enabled, mw = d.st.map_write_enabled;
  const safe = !on && !mw && g.yard_clear_required && !g.guard_resume;
  const gate = (k, v) => saveSetting("gates", { [k]: v });
  const rec = (ok, text) => ok ? null : h("span", { class: "muted small" }, `recommended: ${text}`);
  const master = (key, value, name, help, onText, offMsg, onMsg) => srow(name, help,
    rec(!value, "off"), sstate(value ? AMBER : MUTED, value ? "ON" : "off"),
    value ? sbtn("Switch off", () => saveSetting(key, false, offMsg), "btn")
          : sbtn("Switch on…", () => enableWithPrompt(key, onText, onMsg), "btn"));
  return scard("Safety switches",
    master("control_enabled", on, "Control", "Lets the panel start, pause, resume, stop and dock the robot. Leave it off until you have watched a run or two: the panel still records and plans.",
           "Type ENABLE to let the panel send commands to the robot.", "Control switched off.", "Control switched on."),
    master("map_write_enabled", mw, "Map writing", "Lets Map Studio send map edits to the robot. Each write is read back and undone on a mismatch.",
           "Type ENABLE to let the panel change the robot's map.", "Map writing is off.", "Map writing is on."),
    srow("Yard must be clear to start", ["No start while a yard camera sees a person or pet. Needs ", slink("#settings/cameras", "yard cameras"), "; without them the check says unknown."],
      rec(g.yard_clear_required, "on"), switchCtl("Yard must be clear to start", g.yard_clear_required, (v) => gate("yard_clear_required", v), "yard_clear_required")),
    srow("Resume after a guard pause", "When on, a run a guard paused (someone walked in) resumes by itself once the yard is clear again. Resuming spins the blades.",
      rec(!g.guard_resume, "off"), switchCtl("Resume after a guard pause", g.guard_resume, (v) => gate("guard_resume", v), "guard_resume")),
    srow("All at once", safe ? "Everything is at the recommended value." : "Sets all four to the safe values. Nothing is sent to the robot.",
      safe ? sstate(GREEN, "recommended") : sbtn("Use the recommended", async () => {
        try {
          if (on) await putJSON("/api/settings/control_enabled", false);
          if (mw) await putJSON("/api/settings/map_write_enabled", false);
          await putJSON("/api/settings/gates", { yard_clear_required: true, guard_resume: false });
          toast("Safety set to the recommended values.");
        } catch (e) { toast(e.message); }
        renderSettings();
      }, "btn pri")),
    sfoot("Battery, dry lawn, someone home and what each guard does are in ", slink("#settings/rules", "Run rules"),
          "; assistant commands are in ", slink("#settings/safety", "Safety"), "."));
}

/* A fresh install (no robot address or no map yet) opens the guide by itself, from Live, once per browser session. */
async function maybeOpenSetup() {
  try { if (sessionStorage.getItem("yarbo.setupOffered")) return; } catch { /* no storage: offer every load */ }
  let su;
  try { su = await getJSON("/api/setup/status"); } catch { return; }
  if (!su.first_run) return;
  try { sessionStorage.setItem("yarbo.setupOffered", "1"); } catch { /* see above */ }
  const hash = location.hash.slice(1);
  if (!hash || hash === "live") location.hash = "#settings/setup";
}

/* ------------------------------------------------------------- robot */
// The robot's serial comes from the Yarbo account (one robot: used; several: each with Use this); it is never typed.
async function robotFromAccount(sn) {
  try {
    await postJSON("/api/robot/account", { sn });
    toast("Saved; connecting to that robot.");
  } catch (e) { toast(e.message); }
  await renderSettings();
}

function serialRows(d, cloud) {
  const sn = d.s.sn;
  return [
    srow("Robot", "The robot this panel follows, by serial. It comes from your Yarbo account once you sign in.",
      sn ? sstate(GREEN, mono(sn)) : sstate(cloud.state === "connected" ? AMBER : MUTED, "waiting for the serial")),
    ...(cloud.devices || []).map((x) => srow(["Robot ", mono(x.sn)],
      [x.name || x.model || "On your Yarbo account", ". Your account has more than one robot: choose the one this panel follows."],
      sbtn("Use this", () => robotFromAccount(x.sn), "btn pri")))];
}

function robotCards(d) {
  const s = d.s, cloud = s.ops?.cloud || {};
  const signedIn = ["connected", "connecting", "reconnecting"].includes(cloud.state);
  const cloudColor = cloud.state === "connected" ? GREEN : cloud.state === "needs login" ? CORAL : signedIn ? AMBER : MUTED;
  const seen = s.ops?.phone_app_seen_s;
  return {
    robot: scard("Robot",
      srow("Phone app", "The Yarbo app keeps the robot awake while it is open; the panel waits for it to go quiet before a start.",
        seen === null || seen === undefined ? "not seen" : `seen ${dur(seen)} ago`),
      srow("Wi-Fi survey", ["Which networks the robot hears where, for the ", slink("#live", "Signal"), " map (a scan every 30 s ",
        "during runs). Next run only: one run, a report in ", slink("#events", "Events"), ", then off."],
        selCtl({ key: "wifi_survey", value: d.st.wifi_survey || "off",
                 options: [["off", "Off"], ["next", "Next run only"], ["on", "Every run"]], onsave: (v) => saveSetting("wifi_survey", v) }))),
    cloud: scard("Yarbo cloud",
      snote("The panel reaches the robot through Yarbo's cloud, as the Yarbo app does. The password goes straight to ",
            "Yarbo's SDK and is never stored; only the session tokens are kept on the server."),
      srow("Session", ["connecting", "reconnecting"].includes(cloud.state)
          ? "Signing in to Yarbo's cloud broker usually takes under a minute; this page updates by itself." : null,
        sstate(cloudColor, cloud.state || "unknown")),
      cloud.username ? srow("Account", null, cloud.username) : null,
      cloud.broker ? srow("Broker", null, mono(cloud.broker)) : null,
      cloud.commands_sent ? srow("Messages sent", "Reads and commands through this session.", String(cloud.commands_sent)) : null,
      cloud.error ? srow("Last error", null, colored(CORAL, cloud.error)) : null,
      ...serialRows(d, cloud),
      srow("Video service", ["The Yarbo cloud's robot service. It receives your Yarbo session, so its address is ",
                             "built in. Only the YARBO_ROBOT_SERVICE_URL environment variable can replace it, ",
                             "never Settings."],
        sstate(d.st.robot_video_configured ? GREEN : MUTED,
               d.st.robot_video_configured ? "configured" : "not configured")),
      srow("Robot video", ["Lets a signed-in person watch the robot's cameras. The video goes from Agora's network to ",
                           "your browser, so turning it on makes the browser connect to Agora. Off unless you ",
                           "switch it on; assistants and API tokens can't use it."],
        sstate(d.st.robot_video_enabled ? AMBER : MUTED, d.st.robot_video_enabled ? "ON" : "Off"),
        d.st.robot_video_enabled
          ? sbtn("Switch robot video off",
                 () => saveSetting("robot_video_enabled", false, "Robot video is off."), "btn")
          : sbtn("Switch robot video on…", () => {
              if (confirm("Your browser will connect to Agora's network to show the robot's cameras. " +
                          "Switch robot video on?"))
                saveSetting("robot_video_enabled", true, "Robot video is on.");
            }, "btn pri")),
      cloud.demo ? srow("Account", "DEMO=1: a made-up robot, no Yarbo account and no sign-in.", "none (demo)")
      : signedIn
        ? srow("Sign out", "Ends the session on the server. The panel loses the robot until you sign in again.",
            sbtn("Sign out", async () => {
              try { await postJSON("/api/cloud/logout"); toast("Signed out."); } catch (e) { toast(e.message); }
              renderSettings();
            }, "btn"))
        : loginForm(cloud)) };
}
function secRobot(d) { const c = robotCards(d); return [c.cloud, c.robot]; }

function loginForm(cloud) {
  return h("form", { class: "login", onsubmit: async (e) => {
      e.preventDefault();
      const f = e.target, body = { email: f.email.value, password: f.password.value };
      f.password.value = "";
      try { await postJSON("/api/cloud/login", body); toast("Signed in to Yarbo cloud."); } catch (err) { toast(err.message); }
      renderSettings();
    } },
    h("div", { class: "formgrid" },
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Email"),
        h("input", { class: "field", name: "email", type: "email", autocomplete: "username", required: true, value: cloud.username || "" })),
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Password"),
        h("input", { class: "field", name: "password", type: "password", autocomplete: "current-password", required: true }))),
    h("button", { type: "submit", class: "btn pri" }, "Sign in"));
}

/* ------------------------------------------------------------- safety */
function secSafety(d) {
  const on = d.st.control_enabled, mw = d.st.map_write_enabled, ai = d.st.mcp_control_enabled, mcp = d.st.mcp || {};
  return [
    scard("Control",
      srow("Master switch", ["Lets the panel send commands to the robot: start plan, pause, resume, stop and return to dock. Nothing else ",
                             "is in the catalogue, manual drive is blocked for good, and every attempt passes the checks in ",
                             slink("#settings/rules", "Run rules"), " and is logged in ", slink("#events/commands", "Events → Commands"), "."],
        sstate(on ? AMBER : MUTED, on ? "ON" : "Off"),
        on ? sbtn("Switch control off", () => saveSetting("control_enabled", false, "Control switched off."), "btn")
           : sbtn("Switch control on…", () => enableWithPrompt("control_enabled", "Type ENABLE to let the panel send commands to the robot.", "Control switched on."), "btn pri"))),
    scard("Map writing",
      srow("Map switch", ["Lets ", slink("#studio", "Map Studio"), " send no-go zones and pathways, area and no-vision zone edits (in place), ",
                          "and pathway mow settings to the robot. Each write is read back and compared; a mismatch is undone. ",
                          "Areas and no-vision zones are never added or deleted."],
        sstate(mw ? AMBER : MUTED, mw ? "ON" : "Off"),
        mw ? sbtn("Switch map writing off", () => saveSetting("map_write_enabled", false, "Map writing is off."), "btn")
           : sbtn("Switch map writing on…", () => enableWithPrompt("map_write_enabled", "Type ENABLE to let the panel change the robot's map (no-go zones, pathways, area and no-vision zone edits, pathway mow settings).", "Map writing is on."), "btn pri"))),
    scard("AI assistants",
      srow("Assistant commands", ["Lets a connected AI assistant (", slink("#help/mcp", "MCP"), ") start, pause, resume, stop and dock the robot ",
                                  "when you ask it. Control must be on too; every check still applies and cannot be overridden."],
        sstate(ai ? AMBER : MUTED, ai ? "ON" : "Off"),
        ai ? sbtn("Switch assistant commands off", () => saveSetting("mcp_control_enabled", false, "Assistant commands are off."), "btn")
           : sbtn("Switch assistant commands on…", () => enableWithPrompt("mcp_control_enabled", "Type ENABLE to let a connected AI assistant send commands to the robot.", "Assistant commands are on."),
                  mcp.enabled ? "btn pri" : "btn"))),
  ];
}

/* ------------------------------------------------------------- run rules */
// The two people rules act at different times and are easy to mix up (#54): say so when only one of them is on.
function peopleNote(g, where, controlOn) {
  const startOn = !!g.yard_clear_required, guardActs = !!controlOn && ["pause", "dock"].includes(g.guard_person);
  if (startOn === guardActs) return null;
  const text = where === "start"
    ? (startOn ? null : `Off for starts, but the person guard still ${g.guard_person === "dock" ? "sends a running mow home" : "pauses a running mow"} when it sees someone.`)
    : (guardActs ? null : controlOn ? "The guard won't stop a running mow for people; they only block a start."
                                    : "With control off the guard only notifies; people only block a start.");
  return text ? ssub(srow("", text, null)) : null;
}

function secRules(d) {
  const g = d.st.gates;
  const gate = (k, v) => saveSetting("gates", { [k]: v });
  return [
    scard("Before a run starts",
      snote("The scheduler checks these before every window, and so does a start from Live. A failed check refuses the start; ",
            "a manual start can override the soft ones. How they stand right now is shown on ", slink("#live", "Live"), " and ",
            slink("#schedule", "Schedule"), "."),
      srow("Minimum battery", "Starts need at least this much charge.",
        numCtl({ key: "min_battery_pct", value: g.min_battery_pct, min: 0, max: 100, unit: "%", onsave: (v) => gate("min_battery_pct", v) })),
      srow("Lawn must be dry", ["From the weather station, by the rules in ", slink("#settings/lawn", "Lawn conditions"), "."],
        switchCtl("Lawn must be dry", g.require_dry, (v) => gate("require_dry", v), "require_dry")),
      g.require_dry ? ssub(srow("Damp is fine too", "Dew or light rain that is drying out.",
        switchCtl("Damp is fine too", g.allow_damp, (v) => gate("allow_damp", v), "allow_damp"))) : null,
      srow("Someone must be home", ["By the presence entity in ", slink("#settings/ha", "Home Assistant"), ". Unknown counts as nobody home."],
        switchCtl("Someone must be home", g.presence_required, (v) => gate("presence_required", v), "presence_required")),
      srow("Yard must be clear to start", ["No person or pet on a ", slink("#settings/cameras", "yard camera"),
        ". Only before a start: a mow already running is the person guard's (During a run, below)."],
        switchCtl("Yard must be clear to start", g.yard_clear_required, (v) => gate("yard_clear_required", v), "yard_clear_required")),
      peopleNote(g, "start", d.st.control_enabled),
      g.yard_clear_required ? ssub(srow("Clear for", null,
        numCtl({ key: "yard_clear_minutes", value: g.yard_clear_minutes, min: 0, max: 120, unit: "min", onsave: (v) => gate("yard_clear_minutes", v) }))) : null,
      sstack("Who counts", "Frigate labels that make the yard busy, separated by commas.",
        textCtl({ key: "yard_labels", value: g.yard_labels.join(", "), onsave: (v) => {
          const labels = (v || "").split(",").map((x) => x.trim()).filter(Boolean);
          if (!labels.length) { toast("Keep at least one label, or the yard-clear rule and the person guard could never fire."); renderSettings(); return; }
          gate("yard_labels", labels);
        } })),
      srow("Phone app quiet for", "No keep-awake from the Yarbo app in this window, so the panel never starts while someone is driving by hand.",
        numCtl({ key: "phone_app_quiet_s", value: g.phone_app_quiet_s, min: 0, max: 3600, unit: "s", onsave: (v) => gate("phone_app_quiet_s", v) }))),
    scard("During a run",
      snote("Checked every 5 seconds while the robot works. With control off, each of these can only notify."),
      srow("Person guard", "A person or pet appears on a yard camera (inside its drawn coverage) while a mow runs. Separate from the start check above.",
        selCtl({ key: "guard_person", value: g.guard_person, options: ACTIONS, onsave: (v) => gate("guard_person", v) })),
      peopleNote(g, "guard", d.st.control_enabled),
      srow("Rain starts", "From the weather station.",
        selCtl({ key: "guard_rain", value: g.guard_rain, options: ACTIONS, onsave: (v) => gate("guard_rain", v) })),
      srow("Everyone leaves", "The presence entity says nobody is home.",
        selCtl({ key: "guard_everyone_left", value: g.guard_everyone_left, options: ACTIONS, onsave: (v) => gate("guard_everyone_left", v) })),
      srow("Leaves the charger after dark", "A plan that stopped to charge heads back out by itself when it is full, at any hour. After sunset at your yard, Return to dock sends it home instead (not when you sent it out yourself); the next schedule window starts the plan again.",
        selCtl({ key: "guard_after_dark", value: g.guard_after_dark || "dock", options: ACTIONS.filter(([v]) => v !== "pause"), onsave: (v) => gate("guard_after_dark", v) })),
      srow("Resume after a guard pause", "Once the cause is gone. Never after a return to dock.",
        switchCtl("Resume after a guard pause", g.guard_resume, (v) => gate("guard_resume", v), "guard_resume")),
      g.guard_resume ? ssub(srow("Clear for", "Only a pause the guard sent, and only if the start checks also pass (yard clear, someone home, dry lawn).",
        numCtl({ key: "guard_resume_after_min", value: g.guard_resume_after_min, min: 1, max: 45, unit: "min", onsave: (v) => gate("guard_resume_after_min", v) }))) : null),
  ];
}

/* ------------------------------------------------------------- lawn */
function secLawn(d) {
  const c = d.st.conditions, t = d.s.tempest, now = d.conds, wp = d.st.integrations?.weather?.tempest_port;
  const ws = d.st.weather_sources, haOn = !!(d.s.ha && d.s.ha.enabled);
  const haErr = (now && now.ha_weather && now.ha_weather.errors) || {}, soilNow = (now && now.ha_weather && now.ha_weather.soil) || [];
  const saveWs = (change) => saveSetting("weather_sources", { ...ws, ...change });
  const using = (now && now.sources) || {};
  if (haOn) haSensors(d.s.ha);
  const hs = setUI.haSensors;
  const haRow = (key, name, help, placeholder) => sstack(name, [help, using[key] ? ` Now from ${using[key] === "ha" ? "Home Assistant" : "the Tempest"}.` : ""],
    h("span", { class: "row gap wrap" },
      entityCtl({ key: `ha_${key}`, input: key, value: ws.ha[key], placeholder, onsave: (v) => saveWs({ ha: { ...ws.ha, [key]: v } }) }),
      selCtl({ key: `use_${key}`, value: ws.use[key], options: [["auto", "Either"], ["tempest", "Tempest only"], ["ha", "HA only"]],
               onsave: (v) => saveWs({ use: { ...ws.use, [key]: v } }) })));
  const soil = ws.ha.soil || [];
  const saveSoil = (list) => saveWs({ ha: { ...ws.ha, soil: list } });
  const cond = (k, v) => saveSetting("conditions", { [k]: v });
  const n = (key, extra) => numCtl({ key, value: c[key], onsave: (v) => cond(key, v), ...extra });
  const heard = !!t && t.last_obs_age_s !== null && t.last_obs_age_s !== undefined;
  return [
    scard("Now",
      now && now.state ? [
        h("div", { class: "row gap", style: "align-items:baseline;flex-wrap:wrap;margin:10px 0 4px" },
          h("span", { class: "big", style: `color:${STATE_COLOR[now.state]}` }, now.state),
          h("span", { class: "muted" }, now.raining ? "raining now" : now.dry_by ? `dry by about ${clock(now.dry_by)}` : now.state === "Dry" ? "good to mow" : "")),
        h("ul", { class: "rules", style: "margin-bottom:12px" }, (now.rules || []).map((r) => h("li", null, dot(STATE_COLOR[r.state]),
          h("span", null, h("b", null, r.rule), " ", h("span", { class: "muted" }, r.detail)))))]
        : snote("Waiting for the first observation from the weather station."),
      intRow("Weather station", "A Tempest hub broadcasts on your network over UDP; no account or cloud involved. 0 switches the listener off.",
        "weather", "tempest_port", wp,
        heard ? [t.station ? mono(t.station) : null, t.battery_v ? mono(`${Number(t.battery_v).toFixed(2)} V`) : null, sstate(GREEN, `heard ${dur(t.last_obs_age_s)} ago`)] : t ? sstate(CORAL, t.error || "nothing heard") : sstate(MUTED, "off"),
        numCtl({ key: "tempest_port", value: ival(wp) === "" ? null : ival(wp), min: 0, max: 65535, unit: "UDP",
                 blank: wp && wp.env !== null && wp.env !== undefined ? String(wp.env) : "off", onsave: (v) => saveIntegration("weather", { tempest_port: v }) })),
      t && !heard && !t.packets && (d.s.uptime_s || 0) > 75
        ? h("p", { class: "shelp" }, colored(AMBER, "Nothing heard from a Tempest yet. "),
            `The hub broadcasts about once a minute on UDP ${t.listening_port || 50222} to its own network, so the panel must be on `,
            "the same network (not a guest network or another VLAN). In Docker the container needs host networking (",
            mono("network_mode: host"), "): broadcasts don't reach Docker's bridge network. No Tempest? Home Assistant sensors can stand in below.")
        : null,
      t && (t.stations || []).length ? srow("Station", "When the hub hears more than one station, pick the one in your yard.",
        selCtl({ key: "station", value: ws.station || "", options: [["", "Any station"], ...t.stations.map((x) => [x.serial, `${x.serial} · heard ${dur(x.age_s)} ago`])],
                 onsave: (v) => saveWs({ station: v || null }) })) : null,
      srow("Units", "How rain and temperatures are shown on Live and in the rules. The limits below stay in mm and °C.",
        selCtl({ key: "units", value: ws.units, options: [["metric", "mm · °C"], ["us", "in · °F"]], onsave: (v) => saveWs({ units: v }) }))),
    scard("From Home Assistant",
      snote(haOn ? "Sensors in Home Assistant can stand in for the weather station, or add to it. Each input uses the source you pick; “Either” takes the Tempest while it reports, else Home Assistant."
                 : ["Connect ", slink("#settings/ha", "Home Assistant"), " to use its sensors (rain, temperature, humidity, soil moisture)."]),
      haOn ? [
        haRow("rain", "Rain", "A rain rate (best), a running total, an on/off rain sensor, or a weather entity's rainy / not.", "sensor.backyard_rain_rate"),
        haRow("temp", "Temperature", "°C, °F or K; read from the sensor's unit.", "sensor.outdoor_temperature"),
        haRow("humidity", "Humidity", "Relative humidity, %.", "sensor.outdoor_humidity"),
        sstack("Soil moisture", "Zigbee2MQTT soil sensors appear in Home Assistant as entities. Pick up to 8; the wettest one counts.",
          soil.length ? h("span", { class: "row gap wrap" }, soil.map((e) => h("span", { class: "chip" }, mono(entityLabel("soil", e)),
            h("button", { type: "button", class: "linkbtn", "aria-label": `Remove ${e}`, onclick: () => saveSoil(soil.filter((x) => x !== e)) }, "×")))) : null,
          soil.length < 8 ? entityCtl({ key: "ha_soil", input: "soil", value: null, exclude: soil, none: "Add a soil sensor…", placeholder: "sensor.lawn_soil_moisture",
                                        onsave: (v) => { if (v) saveSoil([...soil, v]); } }) : null),
        hs && hs.error ? sfoot(colored(CORAL, `Could not list Home Assistant's sensors (${hs.error}); type the entity ids instead.`))
          : sfoot(hs && hs.data ? `Matched by Home Assistant's device classes; likely ones first. ` : "Reading Home Assistant's sensors… ",
                  hs && hs.data ? h("label", { class: "row gap", style: "display:inline-flex" }, h("input", { type: "checkbox", "data-key": "ha_all", checked: setUI.haAll || null,
                    onchange: (e) => { setUI.haAll = e.target.checked; drawSettings(setUI.data, false); } }), "Show all sensors") : null,
                  hs && hs.data ? [" · ", h("button", { type: "button", class: "linkbtn", onclick: () => { setUI.haSensors = null; drawSettings(setUI.data, false); } }, "Refresh")] : null),
        Object.keys(haErr).length ? sfoot(colored(CORAL, `Not read: ${Object.entries(haErr).map(([e, m]) => `${e} (${m})`).join(", ")}`)) : null] : null),
    scard("Soil",
      srow("Now", null, soilNow.length ? mono(soilNow.map((x) => `${x.entity.replace(/^sensor\./, "")} ${Math.round(x.pct)} %`).join(" · ")) : h("span", { class: "muted" }, "no soil sensor")),
      srow("Damp from", "Soil this wet holds the lawn at Damp, however long ago it rained.", n("soil_damp_pct", { min: 1, max: 100, unit: "%" })),
      srow("Wet from", "And this wet holds it at Wet.", n("soil_wet_pct", { min: 1, max: 100, unit: "%" }))),
    lawnCharts(ws.units),
    scard("Rain",
      srow("Look back", "Rain older than this no longer counts.", n("lookback_h", { min: 1, max: 48, step: 0.5, unit: "h" })),
      srow("Drying time", "Rain on the grass fades with this time constant: it halves in about 0.7 × this.", n("tau_h", { min: 0.25, max: 24, step: 0.25, unit: "h" })),
      srow("Wet above", "This much faded rain means Wet.", n("wet_mm", { min: 0, max: 50, step: 0.1, unit: "mm" })),
      srow("Damp above", "This much, but less than Wet, means Damp.", n("damp_mm", { min: 0, max: 50, step: 0.05, unit: "mm" })),
      srow("Raining now", "Any rain inside this window.",
        numCtl({ key: "raining_window_s", value: Math.round(c.raining_window_s / 60), min: 1, max: 60, unit: "min", onsave: (v) => cond("raining_window_s", v * 60) }))),
    scard("Dew",
      srow("Dew risk", "When the air is within this much of its dew point.", n("dew_spread_c", { min: 0, max: 10, step: 0.5, unit: "°C" })),
      srow("Gone after sunrise", "Dew counts as dried this long after sunrise.", n("dry_after_sunrise_h", { min: 0, max: 12, step: 0.5, unit: "h" }))),
    scard("Robot rain sensor",
      srow("Reads wet below", "The raw value from the robot's own sensor. Blank until it has been calibrated on a wet morning; then it can veto a start on its own.",
        numCtl({ key: "robot_rain_wet_below", value: c.robot_rain_wet_below, step: "any", blank: "not set", onsave: (v) => cond("robot_rain_wet_below", v) }))),
  ];
}

/* HA entities for each input (GET /api/ha/sensors), kept for a minute (a failed read for 10 s); a redraw follows the
   read. A new address, or the connection coming back, reads them again at once, so the lists fill in as soon as HA
   is set up. */
function haSensors(ha) {
  const key = `${ha.url || ""}|${ha.error ? "down" : "up"}`, hs = setUI.haSensors;
  if (hs && hs.key === key && (hs.loading || Date.now() - hs.at < (hs.error ? 10000 : 60000))) return;
  setUI.haSensors = { key, loading: true, at: Date.now(), data: hs && hs.key === key ? hs.data : null, error: null };
  getJSON("/api/ha/sensors")
    .then((data) => { if (setUI.haSensors.key === key) setUI.haSensors = { key, at: Date.now(), data, error: null }; })
    .catch((e) => {
      if (setUI.haSensors.key !== key) return;
      setUI.haSensors = { key, at: Date.now(), data: null, error: e.message };
      // Nothing redraws Settings by itself, so a page that needs the lists tries again while it stays open.
      setTimeout(() => {
        const open = state.view === "settings" && ["lawn", "ha", "setup"].includes(setUI.section);
        if (open && setUI.haSensors && setUI.haSensors.error && setUI.data) { setUI.haSensors = null; drawSettings(setUI.data, false); }
      }, 15000);
    })
    .finally(() => { if (state.view === "settings" && setUI.data && setUI.haSensors.key === key) drawSettings(setUI.data, false); });
}

function entityLabel(input, entity) {
  const d = setUI.haSensors && setUI.haSensors.data;
  const x = d && [...(d[input] || []), ...(d.all || [])].find((c) => c.entity === entity);
  return x ? `${x.name} · ${x.value}` : entity;
}

/* A list of the HA entities that fit an input, with what each reads now; "Show all sensors" adds the rest, and
   "Type an id…" is there for anything the list misses. Without the list (HA not read yet, or it failed) it is the
   plain entity-id field it replaced. */
function entityCtl({ key, input, value, placeholder, onsave, exclude = [], none = "None" }) {
  const d = setUI.haSensors && setUI.haSensors.data;
  if (!d) return textCtl({ key, value, placeholder, onsave });
  const opt = (c) => h("option", { value: c.entity, selected: c.entity === value || null, title: `${c.entity}${c.why ? ` · ${c.why}` : ""}` },
    `${c.name} · ${c.value}${c.why && c.why.startsWith("weather") ? " (weather entity)" : ""}`);
  const fits = (d[input] || []).filter((c) => !exclude.includes(c.entity));
  const known = new Set(fits.map((c) => c.entity));
  const rest = setUI.haAll && input !== "presence" ? (d.all || []).filter((c) => !known.has(c.entity) && !exclude.includes(c.entity)) : [];
  const listed = value && (known.has(value) || rest.some((c) => c.entity === value));
  return h("select", { class: "field", "data-key": key, "aria-label": key, onchange: (e) => {
      let v = e.target.value;
      if (v === "__type__") {
        v = (prompt("Home Assistant entity id", value || placeholder || "") || "").trim();
        if (!v) { e.target.value = value || ""; return; }
      }
      if ((v || null) !== (value || null)) onsave(v || null);
    } },
    h("option", { value: "" }, none),
    value && !listed ? h("option", { value, selected: true }, `${value} (not in the list)`) : null,
    fits.some((c) => c.likely) ? h("optgroup", { label: "Likely" }, fits.filter((c) => c.likely).map(opt)) : null,
    fits.some((c) => !c.likely) ? h("optgroup", { label: fits.some((c) => c.likely) ? "Other matches" : "Matches" }, fits.filter((c) => !c.likely).map(opt)) : null,
    rest.length ? h("optgroup", { label: "All sensors" }, rest.map(opt)) : null,
    h("option", { value: "__type__" }, "Type an id…"));
}

// The last 24 h of weather, as Health used to show it: temperature with dew point, rain, soil moisture.
function lawnCharts(units) {
  const box = h("div", { class: "scard" }, h("p", { class: "label" }, "Last 24 hours"), h("p", { class: "shelp" }, "Loading…"));
  const us = units === "us";
  Promise.all(["temp_c", "dew_c", "rain_mm", "soil_pct"].map((m) => series(m, 24, true))).then(([temp, dew, rain, soil]) => {
    const t = (pts) => (us ? pts.map(([ts, v]) => [ts, v * 9 / 5 + 32]) : pts);
    const r = us ? rain.map(([ts, v]) => [ts, v / 25.4]) : rain;
    let sum = 0;
    const cum = r.map(([ts, v]) => [ts, (sum += v || 0)]);
    fill(box, h("p", { class: "label" }, "Last 24 hours"),
      chart("Temperature and dew point", [{ points: t(temp), color: SERIES[0], label: "Temperature" }, { points: t(dew), color: SERIES[2], label: "Dew point" }], us ? "°F" : "°C"),
      chart("Rain, adding up", [{ points: cum, color: SERIES[0], label: "Rain" }], us ? "in" : "mm", us ? 2 : 1),
      soil.length ? chart("Soil moisture", [{ points: soil, color: SERIES[3], label: "Soil" }], "%", 0) : null);
  }).catch((e) => fill(box, h("p", { class: "label" }, "Last 24 hours"), sfoot(`Could not load: ${e.message}`)));
  return box;
}

/* ------------------------------------------------------------- cameras */
function secCameras(d) {
  const cams = d.cams, yard = cams.yard || [], polys = cams.polygons || {};
  const list = cams.cameras || [], picked = list.filter((c) => yard.includes(c)), rest = list.filter((c) => !yard.includes(c));
  const setYard = (next) => saveSetting("yard_cameras", next);
  const row = (c) => {
    const on = yard.includes(c), ring = polys[c], area = ring ? ringAreaM2(ring) : 0;
    return h("div", { class: "srow" },
      h("div", { class: "stext" }, h("div", { class: "sname" }, shortCam(c)),
        on ? h("p", { class: "shelp" }, !ring ? "No coverage drawn yet, so the follow-cam cannot use it."
                                     : area < 1 ? colored(CORAL, "Coverage has no area: redraw it.")
                                     : `Covers about ${Math.round(area)} m² of the yard.`) : null),
      h("div", { class: "sctl" },
        on ? sbtn(ring ? "Redraw" : "Draw coverage", () => startDraw(c, `#settings/${setUI.section}`)) : null,
        on && ring ? sbtn("Clear", () => { const next = validPolys(polys); delete next[c]; saveSetting("camera_polygons", next, "Coverage cleared."); }) : null,
        switchCtl(`${shortCam(c)} sees the yard`, on, (v) => setYard(v ? [...yard, c] : yard.filter((x) => x !== c)), `cam_${c}`)));
  };
  const fr = d.st.integrations?.frigate || {}, result = h("span", { class: "small" });
  return [
    scard("Frigate",
      intStack("Server", "The Frigate NVR's address, without a login. Snapshots are proxied through this panel.", "frigate", "url", fr.url,
        textCtl({ key: "frigate_url", value: ival(fr.url), placeholder: "http://frigate.local:5000", type: "url", onsave: (v) => saveIntegration("frigate", { url: v === null ? emptied(fr.url) : v }) })),
      srow("Status", null,
        !cams.status?.enabled ? sstate(MUTED, "not set up") : cams.status.error ? sstate(CORAL, cams.status.error) : sstate(GREEN, `${list.length} cameras`),
        result, sbtn("Test", (e) => testIntegration("frigate", { url: $('[data-key="frigate_url"]')?.value.trim() || undefined }, result, e.target)))),
    cams.status?.enabled ? [
      scard("Yard cameras",
        snote("Switch on the cameras that see grass, then draw the ground each one covers on the map. Only these feed the yard-clear rule, ",
              "the person guard and the follow-cam on Live."),
        picked.length ? picked.map(row) : sfoot("None yet.")),
      rest.length ? scard("Other cameras", rest.map(row)) : null] : null,
  ];
}

/* ------------------------------------------------------------- imagery */
function secImagery(d) {
  const el = h("div", { class: "scard", id: "imgpicker", tabindex: "-1" });
  if (d.imgs) drawPicker(el, d.imgs); else fill(el, sfoot("The imagery catalog could not be read."));
  const a = d.imv?.alignment, cur = d.imgs?.yard?.source;
  const f = (v, unit, digits = 1) => `${Number(v || 0).toFixed(digits)}${unit}`;
  const moved = a && (a.east_m || a.north_m || a.rotate_deg || (a.scale && a.scale !== 1));
  const saveAlign = async (next, msg) => {
    try { await putJSON("/api/settings/imagery_alignment", next); toast(msg); loadImagery(); } catch (e) { toast(e.message); }
    renderSettings();
  };
  return [el,
    scard("Alignment",
      srow("Offset", `How ${cur ? cur.name : "the imagery"} is shifted to sit under the robot's map. Each source keeps its own; the robot's map never moves.`,
        a ? h("span", { class: "mono small" }, moved
              ? `${f(a.east_m, " m")} E · ${f(a.north_m, " m")} N · ${f(a.rotate_deg, "°")}${a.scale && a.scale !== 1 ? ` · ×${f(a.scale, "", 3)}` : ""}`
              : "none") : "—",
        sbtn("Align on the map", beginAlign),
        moved ? sbtn("Reset", () => saveAlign({ east_m: 0, north_m: 0, rotate_deg: 0, scale: 1, opacity: a.opacity ?? 1 }, "Alignment reset.")) : null),
      a ? srow("Opacity", "Of the yard photo over the dark background.",
        h("input", { type: "range", min: 0.2, max: 1, step: 0.05, value: a.opacity ?? 1, "aria-label": "Imagery opacity", "data-key": "opacity",
                     style: "width:140px;accent-color:var(--ok)",
                     onchange: (e) => saveAlign({ ...a, opacity: Number(e.target.value) }, "Opacity saved.") }),
        h("span", { class: "unit" }, `${Math.round((a.opacity ?? 1) * 100)} %`)) : null),
  ];
}

/* ------------------------------------------------------------- home assistant */
function secHA(d) {
  const ha = d.s.ha || {}, ig = d.st.integrations?.home_assistant || {}, tok = ig.token || {};
  if (ha.enabled) haSensors(ha);
  const result = h("span", { class: "small" });
  const form = h("form", { class: "login", onsubmit: (e) => {
      e.preventDefault();
      const f = e.target, values = {}, urlNow = f.url.value.trim();
      if (!ival(ig.url) && urlNow === HA_DEFAULT_URL && !f.token.value) { toast("Paste the token too; the address alone is not saved."); return; }
      if (urlNow !== String(ival(ig.url))) values.url = urlNow || emptied(ig.url);
      if (f.token.value) values.token = f.token.value;
      f.token.value = "";
      if (!Object.keys(values).length) { toast("Nothing changed."); return; }
      saveIntegration("home_assistant", values, "Saved; Home Assistant is being used right away.");
    } },
    h("div", { class: "formgrid" },
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Address"),
        h("input", { class: "field", name: "url", "data-key": "ha_url", type: "url", value: ival(ig.url) || HA_DEFAULT_URL, autocomplete: "off", spellcheck: "false" }),
        whence("home_assistant", "url", ig.url),
        !ival(ig.url) || /\.local(:|\/|$)/.test(String(ival(ig.url)).replace(/^https?:\/\//, ""))
          ? h("p", { class: "shelp src" }, ival(ig.url) ? "" : "Home Assistant's usual address. ", "If the panel runs in Docker, .local names often do not resolve there: use HA's IP address (",
              mono("http://192.0.2.20:8123"), ").") : null),
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, tok.set ? "Long-lived access token (set; paste a new one to replace it)" : "Long-lived access token"),
        h("input", { class: "field", name: "token", "data-key": "ha_token", type: "password", autocomplete: "off", placeholder: tok.set ? "••••••••" : "from your HA profile, Security tab" }),
        whence("home_assistant", "token", tok),
        tok.set ? h("p", { class: "shelp src" }, h("button", { type: "button", class: "linkbtn", onclick: () => saveIntegration("home_assistant", { token: tok.source === "setting" && !tok.env_set ? null : false }, "Token removed.") }, "Remove the token")) : null)),
    h("div", { class: "row gap wrap" },
      h("button", { type: "submit", class: "btn pri" }, "Save"),
      sbtn("Test", (e) => testIntegration("home_assistant", { url: form.url.value.trim() || undefined, token: form.token.value || undefined }, result, e.target), "btn"),
      result));
  return [
    scard("Connection",
      snote("The token is kept on this server only and is never shown again. With both set, the panel publishes its entities ",
            "once a minute, reads the presence entity, and can send notifications through Home Assistant."),
      form,
      srow("Status", null, !ha.enabled ? sstate(MUTED, "not set up") : ha.error ? sstate(CORAL, ha.error) : sstate(GREEN, "connected"))),
    scard("Presence",
      sstack("Presence entity", ["An alarm panel (Alarmo armed away or on vacation means nobody is home), ", mono("zone.home"),
                                 " (how many people are in it), or a person, tracker or group of them (home or not). Used by the ",
                                 slink("#settings/rules", "Someone must be home"), " rule and the Everyone leaves guard."],
        entityCtl({ key: "presence_entity", input: "presence", value: d.st.presence_entity, placeholder: "alarm_control_panel.home", onsave: (v) => saveSetting("presence_entity", v) }),
        ha.enabled && setUI.haSensors && setUI.haSensors.error ? h("p", { class: "shelp src" }, colored(CORAL, `Could not list Home Assistant's entities (${setUI.haSensors.error}).`)) : null)),
    scard("Published entities", snote("Once a minute while connected:"), h("ul", { class: "entlist" }, HA_ENTITIES.map((e) => h("li", null, e)))),
  ];
}

/* ------------------------------------------------------------- notifications */
/* HA's notify services (GET /api/ha/notify_services), read like the sensor lists: kept for a minute (a failed read
   for 10 s), read again at once when the connection changes, and Settings redrawn when the answer comes. */
function haNotify(ha) {
  const key = `${ha.url || ""}|${ha.error ? "down" : "up"}`, hn = setUI.haNotify;
  if (hn && hn.key === key && (hn.loading || Date.now() - hn.at < (hn.error ? 10000 : 60000))) return;
  setUI.haNotify = { key, loading: true, at: Date.now(), data: hn && hn.key === key ? hn.data : null, error: null };
  getJSON("/api/ha/notify_services")
    .then((r) => { if (setUI.haNotify.key === key) setUI.haNotify = { key, at: Date.now(), data: r.services || [], error: null }; })
    .catch((e) => { if (setUI.haNotify.key === key) setUI.haNotify = { key, at: Date.now(), data: null, error: e.message }; })
    .finally(() => { if (state.view === "settings" && setUI.data && setUI.haNotify.key === key) drawSettings(setUI.data, false); });
}

/* The notify services HA has, phones first; "Type a name…" for anything the list misses. Without the list (HA not
   connected or not read yet) it is the plain field it replaced. */
function notifyCtl(value) {
  const list = setUI.haNotify && setUI.haNotify.data;
  const save = (v) => saveSetting("ha_notify_service", v);
  if (!list) return textCtl({ key: "ha_notify_service", value, placeholder: "notify.mobile_app_your_phone", onsave: save });
  const opt = (c) => h("option", { value: c.service, selected: c.service === value || null, title: `${c.service} · ${c.why}` },
    `${c.name} · ${c.service}`);
  const phones = list.filter((c) => c.likely), other = list.filter((c) => !c.likely);
  const listed = value && list.some((c) => c.service === value);
  return h("select", { class: "field", "data-key": "ha_notify_service", "aria-label": "Home Assistant notify service", onchange: (e) => {
      let v = e.target.value;
      if (v === "__type__") {
        v = (prompt("Home Assistant notify service", value || "notify.") || "").trim();
        if (!v) { e.target.value = value || ""; return; }
      }
      if ((v || null) !== (value || null)) save(v || null);
    } },
    h("option", { value: "" }, "None (Events only)"),
    value && !listed ? h("option", { value, selected: true }, `${value} (not in Home Assistant)`) : null,
    phones.length ? h("optgroup", { label: "Phones" }, phones.map(opt)) : null,
    other.length ? h("optgroup", { label: phones.length ? "Other notifiers" : "Notifiers" }, other.map(opt)) : null,
    h("option", { value: "__type__" }, "Type a name…"));
}

function secAlerts(d) {
  const ha = d.s.ha || {}, pu = d.st.integrations?.panel?.url;
  if (ha.enabled) haNotify(ha);
  const hn = setUI.haNotify;
  return [
    scard("Channels",
      sstack("Home Assistant notify service",
        [ha.enabled ? "The service that reaches your phone: the companion app's phones are listed first." : ["Needs the ", slink("#settings/ha", "Home Assistant"), " connection first."],
         " Each alert carries a link back to the panel."],
        notifyCtl(d.st.ha_notify_service),
        ha.enabled && hn && hn.error ? h("p", { class: "shelp src" }, colored(CORAL, `Could not list Home Assistant's notify services (${hn.error}).`)) : null,
        ha.enabled && hn && hn.data && !hn.data.length ? h("p", { class: "shelp src" }, "Home Assistant has no notify services yet. Install the companion app on your phone and sign it in to your Home Assistant; it adds one.") : null),
      sstack("Webhook", "Optional. Each alert is POSTed as JSON.",
        textCtl({ key: "webhook_url", value: d.st.webhook_url, placeholder: "https://…", type: "url", onsave: (v) => saveSetting("webhook_url", v) })),
      intStack("Panel address", "How this panel is reached from a phone: the links in notifications point here.", "panel", "url", pu,
        textCtl({ key: "panel_url", value: ival(pu), placeholder: "http://your-server:8425", type: "url", onsave: (v) => saveIntegration("panel", { url: v === null ? emptied(pu) : v }) })),
      srow("Test", "Sends a test alert through every channel above.",
        h("span", { id: "testresult", class: "muted small" }),
        sbtn("Send a test", async (e) => {
          const b = e.target;
          b.disabled = true;
          try { const r = await postJSON("/api/notify/test"); fill($("#testresult"), testSummary(r)); }
          catch (err) { toast(err.message); }
          b.disabled = false;
        }, "btn"))),
    scard("What is sent",
      h("ul", { class: "help", style: "margin:10px 0 12px" },
        ["A robot error code", "The robot looks stuck", "The head was changed", "The cloud session needs a new sign-in",
         "Maintenance is due (checked daily)", "A scheduled run is waiting for your confirmation", "A guard fired during a run"].map((t) => h("li", null, t))),
      sfoot("Every alert is also written to ", slink("#events", "Events"), ", so nothing is lost without a channel.")),
  ];
}

// The notifier answers {ha: true|false, webhook: true|false} for the channels that are set up; nothing for none.
const testSummary = (r) => {
  const names = { ha: "HA app", webhook: "webhook" };
  const parts = Object.entries(r || {}).map(([k, v]) => `${names[k] || k}: ${v ? "sent" : "failed"}`);
  return parts.length ? parts.join(" · ") : "No channel is set up; the test went to Events only.";
};

/* ------------------------------------------------------------- this panel */
function secPanel(d) {
  const a = d.about, pg = d.st.integrations?.panel || {};
  const st = (a && a.stats) || {}, n = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString());
  // Blank hands the field back to the environment; the placeholder shows what that would be.
  const tune = (field, name, help, extra) => intRow(name, help, "panel", field, pg[field],
    numCtl({ key: field, value: ival(pg[field]) === "" ? null : ival(pg[field]), ...extra,
             blank: pg[field] && pg[field].env !== null && pg[field].env !== undefined ? String(pg[field].env) : "default",
             onsave: (v) => saveIntegration("panel", { [field]: v }) }));
  return [
    scard("Recording",
      snote("How often the panel asks the robot for its full state, and how long raw copies are kept. Changes apply at once."),
      tune("poll_idle_s", "Idle polling", "Seconds between snapshots while the robot is docked or idle.", { min: 5, max: 3600, unit: "s" }),
      tune("poll_active_s", "Polling during a run", "Seconds between snapshots while it works.", { min: 0.5, max: 60, step: 0.5, unit: "s" }),
      tune("run_idle_end_s", "A run ends after", "This long without movement.", { min: 30, max: 3600, unit: "s" }),
      tune("map_refresh_s", "Map refresh", "How often the map, plans and GPS reference are read again.", { min: 300, max: 604800, unit: "s" }),
      tune("raw_every_s", "Raw snapshots", "A full copy of the robot's state is stored this often.", { min: 10, max: 3600, unit: "s" }),
      tune("raw_keep_days", "Kept for", "Raw copies older than this are deleted daily.", { min: 1, max: 3650, unit: "days" })),
    scard("Running",
      a ? [
        srow("Version", "What changed in each version is in Help → Changelog.",
          h("span", null, mono(a.version ? versionLabel(a) : "—"), " ",
            h("a", { href: "#help/changelog" }, "Changelog"))),
        srow("Data directory", "Everything the panel knows lives here: the database, imagery, the cloud session.", mono(a.data_dir)),
        srow("Port", null, mono(String(a.port))),
        a.demo ? srow("Demo data", "DEMO=1: a made-up robot in a made-up yard. No robot, cloud or integration is contacted.", sstate(AMBER, "on")) : null,
        srow("Up for", null, dur(a.uptime_s)),
        srow("Recording since", null, when(st.first_ts)),
        srow("Telemetry samples", null, mono(n(st.telemetry_rows))),
        srow("Raw snapshots", null, mono(n(st.raw_snapshots))),
        srow("Weather rows", null, mono(n(st.weather_rows))),
        srow("Runs · map versions", null, mono(`${n(st.runs)} · ${n(st.map_versions)}`))] : sfoot("Not available."),
      srow("API", "Every endpoint with its schema.", h("a", { href: "/api/docs", target: "_blank", rel: "noopener" }, "/api/docs")),
      srow("Theme", "Light or dark for this browser; System follows your device.", selCtl({ key: "theme", value: currentTheme(),
        options: [["system", "System"], ["dark", "Dark"], ["light", "Light"]], onsave: setTheme })),
      srow("AI assistant", ["The MCP token and how to connect are under ", slink("#settings/assistant", "AI assistant"), "."],
        sstate(d.st.mcp?.enabled ? GREEN : MUTED, d.st.mcp?.enabled ? "On" : "Off"))),
  ];
}

// The theme is per browser (localStorage). The map's colours are read once at start, so a change reloads the page.
function currentTheme() { try { return localStorage.getItem("theme") || "system"; } catch { return "system"; } }
function setTheme(v) {
  try { if (v === "system") localStorage.removeItem("theme"); else localStorage.setItem("theme", v); } catch { /* no storage */ }
  location.reload();
}

/* The light / dark switch in the top bar. A click is an explicit choice that stops following the device;
   System (follow the device again) stays in Settings → This panel → Theme. The icon shows what a click switches to. */
function shownTheme() {
  const t = document.documentElement.dataset.theme;
  return t === "light" || t === "dark" ? t : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
(function themeButton() {
  const b = document.getElementById("themebtn");
  if (!b) return;
  const next = shownTheme() === "light" ? "dark" : "light";
  const icon = next === "dark"
    ? svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" }))
    : svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("circle", { cx: 12, cy: 12, r: 4 }),
        svg("path", { d: "M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }));
  fill(b, icon);
  b.setAttribute("aria-label", `Switch to ${next} mode`);
  b.title = `Switch to ${next} mode. To follow your device again: Settings → This panel → Theme → System.`;
  b.addEventListener("click", () => setTheme(next));
})();

/* ------------------------------------------------------------- access */
/* The admin password (app/auth.py), signing out, and named API tokens for programs such as a Home Assistant
   integration. A new token is shown once; only its hash is kept. */
let apiTokenShown = null;
async function authPost(url, body, method = "POST") {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.detail) || String(r.status));
  return data;
}
// The Metrics switch, shown with sign-in on (under API tokens) and with PANEL_AUTH=off (there are no tokens then).
const metricsRow = (d) => srow("Metrics",
  "Serves /api/metrics for Prometheus: no position, no serial. Off unless you switch it on.",
  sstate(d.st.metrics_enabled ? AMBER : MUTED, d.st.metrics_enabled ? "ON" : "Off"),
  sbtn(d.st.metrics_enabled ? "Switch metrics off" : "Switch metrics on",
       () => saveSetting("metrics_enabled", !d.st.metrics_enabled,
                         d.st.metrics_enabled ? "Metrics are off." : "Metrics are on."), "btn"));
function secAccess(d) {
  const a = d.access || {};
  if (a.disabled) {
    return [scard("Sign-in", srow("Off", ["PANEL_AUTH=off is set, so anyone who can reach this panel can use it, robot control included. ",
                                           "Only for a panel behind a reverse proxy that signs people in."], sstate(CORAL, "Off"))),
            scard("Metrics", metricsRow(d))];
  }
  const cur = h("input", { class: "field slim", type: "password", autocomplete: "current-password", placeholder: "Current" });
  const npw = h("input", { class: "field slim", type: "password", autocomplete: "new-password", placeholder: `New (${a.min_password || 8}+ characters)` });
  const change = async () => {
    try { await authPost("/api/auth/password", { current: cur.value, password: npw.value }); toast("Password changed. Other sessions are signed out."); cur.value = npw.value = ""; }
    catch (e) { toast(e.message); }
  };
  const signOut = async (all) => {
    if (all && !confirm("Sign out every browser, this one too?")) return;
    try { await authPost(all ? "/api/auth/signout_all" : "/api/auth/logout"); } catch (e) { toast(e.message); return; }
    location.href = "/login.html?signed_out=1";
  };
  const name = h("input", { class: "field slim", placeholder: "What will use it, e.g. Home Assistant", "data-key": "token-name" });
  // The choice lives in setUI, not only in the checkbox: a redraw (a background fetch) rebuilds the checkbox, and
  // a lost tick would mint a full token where a metrics-only one was asked for.
  const metricsOnly = h("input", { type: "checkbox", "data-key": "token-metrics", checked: setUI.tokenMetrics || null,
    onchange: (e) => { setUI.tokenMetrics = e.target.checked; } });
  const create = async () => {
    const body = { name: name.value, scope: setUI.tokenMetrics ? "metrics" : "full" };
    try { apiTokenShown = await authPost("/api/auth/tokens", body); } catch (e) { toast(e.message); return; }
    setUI.tokenMetrics = false;
    await renderSettings();
  };
  const revoke = async (t) => {
    if (!confirm(`Revoke "${t.name}"? Whatever uses it loses access.`)) return;
    if (apiTokenShown && apiTokenShown.id === t.id) apiTokenShown = null;
    try { await authPost(`/api/auth/tokens/${encodeURIComponent(t.id)}`, null, "DELETE"); } catch (e) { toast(e.message); }
    await renderSettings();
  };
  const tokens = a.tokens || [];
  return [
    scard(a.password === false ? "Sign-in" : "Password",
      a.oidc ? srow(`Sign in with ${a.oidc.name}`, "OpenID Connect, set in the panel's environment (OIDC_*): only the allowed groups and domains get in.",
        sstate(GREEN, "On")) : null,
      a.password === false
        ? srow("Password", "PANEL_PASSWORD=off: the password and first-run setup are off.", sstate(MUTED, "Off"))
        : srow("Change password", "Signs out every other browser.", h("div", { class: "row gap wrap" }, cur, npw, sbtn("Change", change))),
      srow("Sign out", "This browser, or every browser signed in.",
        h("div", { class: "row gap wrap" }, sbtn("Sign out", () => signOut(false)), sbtn("Sign out everywhere", () => signOut(true)))),
      a.password === false ? null
        : sfoot("Forgot the password? Run ", mono("python -m app.auth reset"), " in the container and restart it; its log shows a new setup code.")),
    scard("API tokens",
      snote(h("span", null, "For programs that call the panel's API, such as a Home Assistant integration or a script: header ",
            mono("Authorization: Bearer <token>"), ". A token can read, and can ask for robot commands while control is on, under the strictest checks: anything unknown blocks a start or resume, and it cannot override. ",
            "It cannot change settings, safety switches or the map. AI assistants use their own token (", slink("#settings/assistant", "AI assistant"), ").",
            " A metrics-only token reads /api/metrics and nothing else.")),
      metricsRow(d),
      ...tokens.map((t) => srow(t.scope === "metrics" ? `${t.name} (metrics only)` : t.name,
        `Created ${when(t.created)} · last used ${t.used ? when(t.used) : "never"}`,
        h("span", { class: "row gap" }, mono(`…${t.hint}`), sbtn("Revoke", () => revoke(t))))),
      apiTokenShown ? srow(apiTokenShown.scope === "metrics" ? "New token (metrics only)" : "New token",
        "Copy it now: it is not shown again.",
        h("div", { class: "row gap wrap" }, h("span", { class: "mono secret" }, apiTokenShown.token),
          sbtn("Copy", () => copyToast(apiTokenShown.token)))) : null,
      srow("New token", null, h("div", { class: "row gap wrap" }, name,
        h("label", { class: "row gap small tapmin",
          title: "A metrics-only token can read /api/metrics and nothing else. Use it for Prometheus." },
          metricsOnly, "Metrics only"),
        sbtn("Create", create)))),
  ];
}

/* ------------------------------------------------------------- ai assistant */
/* AI assistants over MCP: a token turns /mcp on; it is shown once, only its hash is kept. Optional, but the
   recommended second step after the robot: an assistant can read every setting and the help, find the most
   detailed imagery for the yard, and walk through cameras, rules, schedule and alerts by conversation. */
let mcpShown = null;
async function mcpToken(method) {
  const r = await fetch("/api/mcp/token", { method, headers: { "X-Panel-Request": "1" } });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.detail) || String(r.status));
  return data;
}
const ASK_FIRST = [
  "Find the most detailed aerial map of my yard: check every source that covers it, look at the samples, and if a county or state service would be sharper, find it for me.",
  "Walk me through setting up this panel: cameras, run rules, lawn conditions, schedule and alerts.",
  "What's the robot doing, and would it start right now?",
];
function secAssistant(d) {
  const m = d.st.mcp || {};
  const url = `${location.origin}/mcp`;
  const token = mcpShown || "<your token>";
  const create = async () => {
    if (m.enabled && !confirm("Make a new token? The old one stops working.")) return;
    try { mcpShown = (await mcpToken("POST")).token; } catch (e) { toast(e.message); }
    await renderSettings();
  };
  const revoke = async () => {
    if (!confirm("Revoke the token? Connected assistants lose access and assistant commands switch off.")) return;
    mcpShown = null;
    try { await mcpToken("DELETE"); toast("MCP is off."); } catch (e) { toast(e.message); }
    await renderSettings();
  };
  // Claude Desktop's config file only starts local programs, and its "Add custom connector" needs https and connects
  // from Anthropic's cloud, which cannot see a home network. So Desktop goes through the mcp-remote bridge (Node.js),
  // which runs on the user's computer. The header goes in through an env var: Windows mangles a header with a space.
  const bridgeArgs = ["-y", "mcp-remote", url, ...(url.startsWith("http:") ? ["--allow-http"] : []), "--header", "Authorization:${AUTH_HEADER}"];
  const desktopSnippet = JSON.stringify({ mcpServers: { groundskeeper: { command: "npx", args: bridgeArgs, env: { AUTH_HEADER: `Bearer ${token}` } } } }, null, 2);
  const codeSnippet = `claude mcp add --transport http groundskeeper ${url} --header "Authorization: Bearer ${token}"`;
  const copyBtn = (text, label = "Copy") => sbtn(label, () => copyToast(text));
  const code = (text) => h("pre", { class: "mdcode", style: "margin:6px 0 0;white-space:pre-wrap;word-break:break-all" }, text);
  const step = (n, title, ...body) => h("div", { class: "srow stack" },
    h("div", { class: "stext" }, h("div", { class: "sname" }, `${n}. ${title}`), ...body));
  return [
    scard("Why connect one",
      snote(h("span", null, "An AI assistant that speaks MCP (Claude Desktop, Claude Code, others) reads the same settings and help pages you see and can set most of them up by conversation. ",
            "It is best at the fiddly part: finding the ", h("b", null, "most detailed aerial map"), " of your yard. It compares every source that covers you, ",
            "looks at the yard samples, and when the sharpest free one is coarse (say 60 cm per pixel) it searches your county or state's public map services for one at 7–30 cm, ",
            "then hands you the address to paste in. You can skip this and set everything by hand; nothing else depends on it.")),
      srow("Status", ["Every change it makes is confirmed with you first, and it can only send the robot commands if you also switch on ",
                      slink("#settings/safety", "Assistant commands"), ". ", slink("#help/mcp", "More")],
        m.enabled ? sstate(GREEN, `On · token …${m.hint || ""}`) : sstate(MUTED, "Off"))),
    scard("Connect",
      step(1, "Create a token",
        h("p", { class: "shelp" }, "The token turns MCP on and is the only thing an assistant needs besides the address. It is shown once; only a fingerprint is kept."),
        h("div", { class: "row gap wrap", style: "margin-top:6px" },
          sbtn(m.enabled ? "New token…" : "Create a token", create, m.enabled ? "btn slim" : "btn pri"),
          m.enabled ? sbtn("Revoke", revoke) : null),
        mcpShown ? h("div", { class: "row gap wrap", style: "margin-top:8px" },
          h("span", { class: "mono secret" }, mcpShown), copyBtn(mcpShown)) : null),
      step(2, "Add the panel to your assistant",
        h("p", { class: "shelp" }, "Address: ", mono(url), mcpShown ? null : ". The snippets below fill in your token once you have created one."),
        h("p", { class: "shelp", style: "margin-top:8px" }, h("b", null, "Claude Code"), " (terminal), one command:"),
        code(codeSnippet), h("div", { class: "row gap", style: "margin-top:4px" }, copyBtn(codeSnippet)),
        h("p", { class: "shelp", style: "margin-top:8px" }, h("b", null, "Claude Desktop"), " connects through ", mono("mcp-remote"),
          ", a small bridge that runs on your computer; it needs ", h("a", { href: "https://nodejs.org/", target: "_blank", rel: "noopener" }, "Node.js"),
          ". Settings → Developer → Edit config, add the ", mono("groundskeeper"), " entry inside ", mono("mcpServers"),
          " (keep any servers already there), save, and restart Claude:"),
        code(desktopSnippet), h("div", { class: "row gap", style: "margin-top:4px" }, copyBtn(desktopSnippet)),
        h("p", { class: "shelp", style: "margin-top:6px" }, "Not ", h("b", null, "Settings → Connectors → Add custom connector"),
          ": it only takes https addresses and connects from Anthropic's cloud, which cannot reach your home network or send the panel's token."),
        h("p", { class: "shelp", style: "margin-top:8px" }, h("b", null, "Other MCP clients"), ": streamable HTTP at the address above with header ",
          mono("Authorization: Bearer <token>"), ". A client that only takes https addresses can use ", mono("mcp-remote"), " the same way. ",
          "The panel must be reachable from wherever the assistant runs (same network, or a VPN).")),
      step(3, "Try asking",
        h("ul", { class: "shelp", style: "margin:4px 0 0 18px;padding:0" }, ASK_FIRST.map((q) => h("li", null, "“", q, "”"))),
        h("p", { class: "shelp", style: "margin-top:6px" }, "It starts by reading the help pages, so it knows what the panel can and cannot do. ",
          "Your home location, robot serial and account are removed from everything it reads."))),
  ];
}

/* ------------------------------------------------------------- raw data */
const DATA_SOURCES = [
  ["/api/snapshot", "Latest snapshot (get_device_msg)"], ["/api/map/raw", "Map (get_map, decoded)"], ["/api/plans", "Plans (read_all_plan)"],
  ["/api/cfd", "Low-confidence config"], ["/api/replies", "Other replies seen (schedules, recharge point…)"],
  ["/api/commands", "Commands other clients sent"], ["/api/status", "Panel status"], ["/api/stats", "Recorder stats"],
];

function secData() {
  setUI.dataSrc = setUI.dataSrc || DATA_SOURCES[0][0];
  return [scard(null,
    h("div", { class: "datatools" },
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Source"),
        h("select", { class: "field", "data-key": "datasrc", onchange: (e) => { setUI.dataSrc = e.target.value; loadDataTree(); } },
          DATA_SOURCES.map(([v, t]) => h("option", { value: v, selected: v === setUI.dataSrc || null }, t)))),
      h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Filter keys and values"),
        h("input", { class: "field", type: "search", "data-key": "datafilter", value: setUI.dataFilter || "", placeholder: "e.g. temp, rtk, blade",
                     autocomplete: "off", oninput: (e) => { setUI.dataFilter = e.target.value; drawTree(); } })),
      h("div", { class: "row gap" },
        sbtn("Expand all", () => $$("#datatree details").forEach((d) => { d.open = true; })),
        sbtn("Reload", () => loadDataTree()),
        sbtn("Download JSON", downloadData))),
    h("div", { id: "datatree", class: "tree" }, sfoot("Loading…")))];
}

async function loadDataTree() {
  const src = setUI.dataSrc || DATA_SOURCES[0][0];
  setUI.dataLoading = src;
  let data, err = null;
  try { data = await getJSON(src); } catch (e) { err = e.message; }
  if (setUI.dataLoading !== src) return;               // another source was picked meanwhile
  if (err) { state.dataCache = null; setUI.dataLoaded = null; const el = $("#datatree"); if (el) fill(el, sfoot(`Not available yet (${err}).`)); return; }
  state.dataCache = data;
  setUI.dataLoaded = src;
  drawTree();
}

function drawTree() {
  const el = $("#datatree");
  if (!el || state.dataCache === null) return;
  const f = (setUI.dataFilter || "").trim().toLowerCase();
  fill(el, treeNode(state.dataCache, undefined, f) || h("p", { class: "muted" }, "No matches."));
}

function downloadData() {
  if (state.dataCache === null) return;
  const blob = new Blob([JSON.stringify(state.dataCache, null, 2)], { type: "application/json" });
  const name = (setUI.dataLoaded || "data").split("/").pop();
  const a = h("a", { href: URL.createObjectURL(blob), download: `yarbo-${name}-${Date.now()}.json` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

maybeOpenSetup();
