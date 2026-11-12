/* Phase 2 UI: control, pending confirmations, follow-cam, schedule, maintenance, camera polygons (Settings is settings.js).
   Loaded after app.js and uses its helpers (h, fill, kv, card, chip, getJSON, putJSON, state, setSource, toast…).
   Commands only go out through POST /api/control/send or the pending confirmation, both behind the server's
   catalogue, master switch and interlocks. */
"use strict";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// As app/ops.py: the states in which a pause or a return to dock would do nothing.
const NOT_PAUSABLE = new Set(["paused", "charging_break", "returning", "docking", "docked", "idle"]);
const NOT_DOCKABLE = new Set(["charging_break", "returning", "docking", "docked"]);
const ops = { followTimer: null, pinnedCam: null, confirm: null, editing: null, drawing: null, planId: null, watch: null,
              mode: savedLiveMode(), camAway: false, arm: null, sending: null, mini: null, feed: null };

async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { /* not json */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `${r.status} ${text}`);
  return data;
}

// A view opened straight from its URL can render before the first SSE status arrives.
async function currentStatus() {
  if (!state.status) { try { state.status = await getJSON("/api/status"); } catch { /* the stream will fill it */ } }
  return state.status || {};
}

function checkList(checks) {
  return h("ul", { class: "rules" }, (checks || []).map((c) => h("li", null,
    dot(c.ok === true ? GREEN : c.ok === false ? (c.hard ? CORAL : AMBER) : MUTED),
    h("span", null, h("b", null, c.name), " ", h("span", { class: "muted" }, c.detail),
      c.ok === false && !c.hard ? h("span", { class: "muted" }, " (soft)") : null))));
}

// Why it cannot run (app/why.py): what is wrong, what fixes it, and where. The same words on Live, Schedule and runs.
const WHY_COLOR = { block: CORAL, warn: AMBER, info: MUTED };
function whyList(reasons) {
  return h("ul", { class: "rules whylist" }, (reasons || []).map((r) => h("li", null,
    dot(WHY_COLOR[r.severity] || MUTED),
    h("span", null, h("b", null, r.title), r.detail ? h("span", { class: "muted" }, ` · ${r.detail}`) : null,
      r.fix ? h("span", { class: "whyfix" }, r.fix, r.link ? [" ", h("a", { href: r.link }, "→")] : null) : null))));
}
const whyHead = (reasons) => `${reasons[0].title}${reasons.length > 1 ? ` (+${reasons.length - 1} more)` : ""}`;

// Live: the robot paused itself, or the last run ended without finishing its plan.
function whyCard(w) {
  if (!w || (!w.held && !w.last_run)) return null;
  const items = [w.held, w.last_run].filter(Boolean);
  return h("div", { class: `card${w.held && w.held.severity === "block" ? " attention" : ""}` },
    h("p", { class: "label" }, w.held ? "Why it stopped" : "Last run"),
    whyList(items),
    w.last_run ? h("p", { class: "muted tiny", style: "margin:4px 0 0" },
      h("a", { href: `#runs/${w.last_run.run_id}` }, "Open the run"), ` · ended ${when(w.last_run.ended)}`) : null);
}

/* --------------------------------------------------------------- chips */
const GUARD_NAMES = { person: "Person guard", rain: "Rain guard", away: "Everyone-left guard", dark: "After-dark guard" };
// The top bar's alerts from ops (a run waiting for a yes, guards); also where a command's result is announced.
function opsAlerts(s) {
  const o = s.ops;
  if (!o) return [];
  const lc = o.last_command, w = ops.watch;
  if (w && lc && lc.at === w.at && lc.done) {
    toast(lc.confirmed ? `${w.label}: done, ${lc.confirm}` : lc.confirmed === false ? `${w.label}: sent, but ${lc.confirm}`
          : `${w.label}: sent (${lc.confirm})`);
    ops.watch = null;
  }
  return [
    o.pending ? alertChip("#live", AMBER, h("b", null, "Waiting for you"), ` · ${o.pending.plan}`) : null,
    ...(o.guards || []).filter((g) => g.active || g.acted).map((g) =>
      titled(alertChip("#settings/rules", AMBER, h("b", null, GUARD_NAMES[g.key] || "Guard"),
                       ` · ${g.acted === "pause" ? "paused for" : g.acted === "dock" ? "docked for" : "seeing"} ${g.why}`),
             `${GUARD_NAMES[g.key] || "A guard"}: Settings → Run rules → During a run`)),
  ];
}

// The top bar's switches, which it shows as one group.
function opsSwitches(s) {
  const o = s.ops;
  if (!o) return [];
  return [
    switchChip("control_enabled", "Control", o.control_enabled, "let the panel send commands to the robot"),
    state.view === "studio" ? switchChip("map_write_enabled", "Map writing", o.map_write_enabled, "let Map Studio change the robot's map") : null,
  ];
}

// Grouped with the other connection chips (HaLow, Wi-Fi, RTK) in the top bar: "Cloud" while connected, else its state.
function cloudChip(s) {
  const c = s.ops && s.ops.cloud;
  if (!c) return null;
  const ok = c.state === "connected";
  return titled(chipLink("#settings/robot", ok ? GREEN : c.state === "needs login" ? CORAL : MUTED, connLabel("Cloud", ok ? "" : `: ${c.state}`)),
                `Yarbo cloud: ${c.state}`);
}

// A switch in the top bar: on asks for ENABLE (as in Settings → Safety), off is one click. Logged as in Settings.
function switchChip(key, label, on, what) {
  return h("button", { type: "button", class: `chip switchchip${on ? " on" : ""}`, "aria-pressed": on ? "true" : "false",
                       title: on ? `${label} is on: click to switch it off` : `Switch ${label.toLowerCase()} on…`,
    onclick: async () => {
      if (!on && prompt(`Type ENABLE to ${what}.`) !== "ENABLE") return;
      try { await putJSON(`/api/settings/${key}`, !on); toast(`${label} ${on ? "off" : "on"}.`); }
      catch (e) { toast(e.message); return; }
      try { state.status = await getJSON("/api/status"); renderChips(state.status); } catch { /* the stream catches up */ }
      if (state.view === "settings") renderSettings();
    } },
    h("span", { class: "minitrack", "aria-hidden": "true" }), label, h("span", { class: "swstate" }, on ? " ON" : " off"));
}

/* -------------------------------------------------------- live extras */
// Live, top to bottom: what the robot is doing, a run waiting for a yes, Control, the follow-cam. In the camera
// layout the follow-cam takes the map's slot, the emergency buttons lead the panel and the small map comes next.
// When no camera's coverage holds the robot (and none is pinned), the camera layout puts the map back in the big
// slot until one does; the emergency buttons stay at the top of the panel. On a phone the map layout's panel sits under
// the map, so during a run the emergency buttons lead it there too (Control's own buttons are a screen further down).
const PHONE_LIVE = STACKED;
PHONE_LIVE.addEventListener("change", () => { if (state.view === "live") refreshLive(); });
const runGoing = (s) => !!s.run || (!!s.activity && !["docked", "idle"].includes(s.activity));
async function liveExtras(s) {
  const o = s.ops || {};
  // Live can render before the map's plan list arrives; an empty picker would send plan_id 0.
  if (!state.plansCache.length) { try { state.plansCache = (await getJSON("/api/plans")).plans || []; } catch { /* none yet */ } }
  const [fc, robotCams] = await Promise.all([followCamData(), camerasCard(s)]);   // cameras.js: the robot's own cameras
  const rest = [whyCard(o.why), o.pending ? pendingCard(o.pending, o.control_enabled) : null, controlCard(s),
                robotCams];
  if (ops.mode === "camera") {
    const away = !fc.pinned && (!fc.cam || fc.covered === false);
    if (away !== ops.camAway) { ops.camAway = away; syncLiveMode(); }
    if (away) return [emergencyCard(s), camAwayCard(fc), statusCard(s), ...rest];
    drawCamStage(fc, s);
    return [emergencyCard(s), miniMapCard(), statusCard(s), ...rest];
  }
  return [PHONE_LIVE.matches && runGoing(s) ? emergencyCard(s) : null, statusCard(s), ...rest, followCamCard(fc)];
}

/* ---------------------------------------------------------- live layout
   Map (the yard map big, the follow-cam a card) or Camera (the follow-cam big, the map a small card in the panel).
   One class on .layout; the map itself (#map) is moved between its slot and the card, so there is one map. The
   choice is per browser. */
function savedLiveMode() { try { return localStorage.getItem("yarbo.liveView") === "camera" ? "camera" : "map"; } catch { return "map"; } }

function setLiveMode(mode) {
  ops.mode = mode === "camera" ? "camera" : "map";
  try { localStorage.setItem("yarbo.liveView", ops.mode); } catch { /* no storage: this page only */ }
  ops.arm = null;
  syncLiveMode();
  refreshLive();
}

function syncLiveMode() {
  // Drawing camera coverage and aligning imagery need the big map, so they show the map layout while they last.
  const live = state.view === "live", cam = live && ops.mode === "camera" && !ops.camAway && !ops.drawing && !state.align;
  const bar = $("#liveview");
  bar.hidden = !live;
  for (const b of $$("[data-mode]", bar)) {
    if (b.dataset.mode === ops.mode) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    b.onclick = () => { if (b.dataset.mode !== ops.mode) setLiveMode(b.dataset.mode); };
  }
  $(".layout").classList.toggle("cam", cam);
  $("#camstage").hidden = !cam;
  if (!cam) { clearCamStage(); }
  const el = MAP_EL, home = cam ? miniSlot() : $(".mapwrap");
  if (el.parentElement !== home) {
    home.prepend(el);
    requestAnimationFrame(() => { if (state.map) { state.map.resize(); fitYard(); } });
  }
  lockMiniMap(cam);
}

// The small map is an overview of the whole yard: it cannot be panned or zoomed away from that (Map layout gives the
// full map). Switched only when the layout changes, so it leaves Map Studio's own settings alone.
const MINI_LOCKED = ["dragPan", "scrollZoom", "doubleClickZoom", "touchZoomRotate", "keyboard"];
function lockMiniMap(on) {
  if (!state.map || ops.miniLocked === on) return;
  ops.miniLocked = on;
  for (const k of MINI_LOCKED) state.map[k][on ? "disable" : "enable"]();
  sync3d(false);                                   // the small map is flat; back in the map layout, 3D comes back
  if (on) fitYard();
}

function miniSlot() { return (ops.mini ||= h("div", { class: "minimap" })); }

// Kept as one element across renders: it holds the live map while the camera layout is on.
function miniMapCard() {
  ops.miniCard ||= h("div", { class: "card" },
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, "Map"),
      h("button", { type: "button", class: "linkbtn small", onclick: () => setLiveMode("map") }, "Map layout")),
    miniSlot());
  return ops.miniCard;
}

// In the camera layout while no camera sees the robot: why the map is big, and the chips to pin a camera anyway.
function camAwayCard(d) {
  return h("div", { class: "card" },
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, "Follow-cam"),
      d.yard ? yardLine(d.yard) : null),
    h("p", { class: "muted small", style: "margin:4px 0" }, d.cam
      ? "No camera sees the robot, so the map is shown. The camera comes back when the robot drives into view."
      : ["No yard camera is set up, so the map is shown. Pick yard cameras and draw what each covers in ",
         h("a", { href: "#settings/cameras" }, "Settings → Cameras"), "."]),
    camChips(d));
}

function clearCamStage() {
  const st = $("#camstage");
  if (st && st.childElementCount) st.replaceChildren();
}

// The follow-cam big, with the camera chips top left, the yard check top right and a strip of the robot's state.
// The picture element is kept while the camera stays the same, so a status update does not reload it.
function drawCamStage(d, s) {
  const st = $("#camstage");
  if (!st) return;
  if (!d.cam) {
    stopFeed();
    fill(st, h("div", { class: "camempty" }, h("p", null, "No yard camera sees the robot."),
      h("p", { class: "muted small" }, "Pick yard cameras and draw what each covers in ", h("a", { href: "#settings/cameras" }, "Settings → Cameras"), ".")));
    return;
  }
  const r = s.robot || {}, plan = (s.run && s.run.plan) || {};
  fill(st, camFeed(d.cam, 720),
    h("div", { class: "camtl" }, camChips(d), h("span", { class: "camname" }, d.pinned ? `${shortCam(d.cam)} (pinned)` : `${shortCam(d.cam)} · ${d.reason || "auto"}`)),
    d.yard ? h("div", { class: "camtr" }, yardLine(d.yard)) : null,
    h("div", { class: "cambar" },
      h("span", { class: "state", style: `color:${stateColor(s)}` }, dot(stateColor(s)), s.pause_reason ? `paused: ${s.pause_reason}` : s.state),
      h("span", null, h("b", { class: "mono" }, `${num(r.battery)}%`), " battery"),
      r.rtk ? h("span", null, r.rtk.label) : null,
      plan.progress_pct !== null && plan.progress_pct !== undefined ? h("span", null, `${num(plan.progress_pct)}% of the plan`) : null,
      s.stalled ? h("span", { style: `color:${CORAL}` }, `not moving for ${dur(s.stalled_s)}`) : null,
      s.rtk_wait ? h("span", { style: `color:${AMBER}` }, `waiting for an RTK fix for ${dur(s.stalled_s)}`) : null));
}

/* The emergency block: one big button that pauses a running plan or resumes a paused one (the robot's own
   planning_paused decides which, through its activity), with Stop and Return to dock under it. The same command
   path and checks as the Control card; the confirmation is a second tap within 4 s instead of a dialog. */
// The Yarbo cloud carries commands; the reason when it doesn't.
const routeOk = (o) => (o.link ? !!o.link.commands : !!(o.cloud && o.cloud.state === "connected"));
const noRouteWhy = (o) => [`No route to the robot: the Yarbo cloud is ${(o.cloud || {}).state || "not set up"}. `,
  h("a", { href: "#settings/robot" }, "Settings → Robot"), "."];

function emergencyCard(s) {
  const o = s.ops || {}, on = o.control_enabled;
  const act = s.activity, known = !!act && !s.lost_contact && s.state !== "waiting for data";
  const running = !!s.run || !["docked", "idle"].includes(act);
  const held = HELD.has(act);
  const main = held ? ["resume", "Resume", "▶"] : ["pause", "Pause", "❚❚"];
  const can = {
    pause: !known || !NOT_PAUSABLE.has(act), resume: !known || held,
    stop: !known || running, cmd_recharge: !known || !NOT_DOCKABLE.has(act),
  };
  const off = !on || !routeOk(o);
  const arm = ops.arm && Date.now() < ops.arm.until ? ops.arm : null;
  // Armed: the whole button turns amber, says what the second tap does, and a bar drains over the 4 s. Live redraws on
  // every status event, so the bar starts part-way through (a negative delay) rather than over again.
  const button = (cmd, label, icon, cls) => {
    const armed = arm && arm.cmd === cmd, busy = ops.sending === cmd;
    return h("button", { type: "button", class: `btn ${cls}${armed ? " armed" : ""}`, "data-cmd": cmd,
      "data-key": `emerg-${cmd}`,
      "data-nofocus": armed ? "" : null,   // armed: a redraw must not leave focus on it (Space or Enter would send)
      disabled: off || !can[cmd] || !!ops.sending || null, "aria-live": "polite",
      onclick: () => emergencyTap(cmd, label) },
      icon ? h("span", { class: "eicon", "aria-hidden": "true" }, icon) : null,
      busy ? `${label}…` : armed ? h("span", { class: "armtext" }, h("b", null, `Tap again to ${label.toLowerCase()}`),
        h("small", null, "or wait to cancel")) : label,
      armed ? h("span", { class: "armbar", "aria-hidden": "true", style: `animation-delay:-${4000 - (arm.until - Date.now())}ms` }) : null);
  };
  const armedLabel = arm && { pause: "Pause", resume: "Resume", stop: "Stop", cmd_recharge: "Return to dock" }[arm.cmd];
  const why = !on ? ["Control is off. ", h("a", { href: "#settings/safety" }, "Switch it on"), " to use these."]
            : !routeOk(o) ? noRouteWhy(o)
            : known && !can[main[0]] ? `Nothing to ${main[1].toLowerCase()}: the robot is ${s.state}.` : null;
  return h("div", { class: "card ecard" },
    button(main[0], main[1], main[2], held ? "pri huge" : "danger huge"),
    h("div", { class: "ehalf" }, button("stop", "Stop", "■", ""), button("cmd_recharge", "Return to dock", "⌂", "")),
    armedLabel ? h("p", { class: "armhint", role: "status" }, `Armed: tap “${armedLabel}” again within 4 seconds to send it.`)
      : h("p", { class: "muted small", style: "margin:8px 0 0" }, why || "These need two taps: the first arms the button (it turns amber), the second sends."));
}

// The second tap must come at least this long after the first: a bounce, a held key or a habitual double-click (500 ms
// is the usual system double-click time) is not a decision.
const ARM_MIN_GAP_MS = 600;
async function emergencyTap(cmd, label) {
  // Never leave a two-tap button focused, on any tap (an ignored one too): Space or Enter on it would be the next tap.
  const f = document.activeElement;
  if (f && f.closest && f.closest(".ecard")) f.blur();
  const a = ops.arm;
  // A bounce or key repeat, not a second tap: ignore it, and the button stays armed.
  if (a && a.cmd === cmd && Date.now() - a.at < ARM_MIN_GAP_MS) return;
  if (!a || a.cmd !== cmd || Date.now() > a.until) {
    ops.arm = { cmd, at: Date.now(), until: Date.now() + 4000 };
    refreshLive();
    setTimeout(() => { if (ops.arm && ops.arm.cmd === cmd && Date.now() >= ops.arm.until) { ops.arm = null; refreshLive(); } }, 4100);
    return;
  }
  ops.arm = null;
  ops.sending = cmd;
  refreshLive();
  try { reportSend(await postJSON("/api/control/send", { cmd, args: {}, confirm: true, override: false }), label); }
  catch (e) { toast(e.message); }
  ops.sending = null;
  refreshLive();
}

// What a send came back with; a result still on its way arrives on the stream (opsAlerts).
function reportSend(r, label) {
  if (r.sent && r.confirmed === null && r.at) ops.watch = { at: r.at, label };
  toast(!r.sent ? `${label}: not sent: ${r.refused || r.error}`
        : r.confirmed ? `${label}: done, ${r.confirm}`
        : r.confirmed === false ? `${label}: sent, but ${r.confirm}` : `${label}: sent, checking that the robot follows…`);
}

function planAreas(p) {
  const names = {};
  for (const f of (state.mapData && state.mapData.geojson.features) || []) {
    if (f.properties.kind === "areas") names[f.properties.id] = f.properties.name;
  }
  return (p.areaIds || []).map((id) => names[id] || `area ${id}`).join(", ");
}

/* ---------------------------------------------------------- readiness
   What a start of a plan would face right now (POST /api/control/preview: reads only, nothing is sent). One block,
   shown on Live (in Control, folded) and on Schedule; the rules themselves are edited in Settings → Run rules.
   Checked at most every 30 s per plan; every block on screen for that plan is redrawn when the answer comes. */
const ready = { cache: new Map(), busy: new Set(), open: false };

function readinessView(planId, full = false) {
  const key = String(planId), c = ready.cache.get(key);
  const el = h("div", { class: "readiness", "data-plan": key, "data-full": full ? "1" : "" });
  if (c) drawReadiness(el, c, full, planId);
  else fill(el, h("p", { class: "muted small", style: "margin:0" }, "Checking the start rules…"));
  if (!c || Date.now() - c.at > 30000) refreshReadiness(planId);
  return el;
}

async function refreshReadiness(planId) {
  const key = String(planId);
  if (ready.busy.has(key)) return;
  ready.busy.add(key);
  let c;
  try { c = { at: Date.now(), r: await postJSON("/api/control/preview", { cmd: "start_plan", args: { plan_id: planId } }) }; }
  catch (e) { c = { at: Date.now(), error: e.message }; }
  ready.busy.delete(key);
  ready.cache.set(key, c);
  for (const el of $$(`.readiness[data-plan="${key}"]`)) drawReadiness(el, c, el.dataset.full === "1", planId);
}

function drawReadiness(el, c, full, planId) {
  const again = h("button", { type: "button", class: "linkbtn", style: "font-size:inherit;min-height:0", onclick: () => refreshReadiness(planId) }, "check again");
  if (c.error) {
    fill(el, h("p", { class: "small", style: `color:${AMBER};margin:0` }, `Could not check the start rules: ${c.error} · `, again));
    return;
  }
  const r = c.r, hard = r.checks.some((x) => x.ok === false && x.hard);
  // The reasons beyond "control is off" (that one is said on its own when it is the only one).
  const reasons = (r.why || []).filter((x) => x.code !== "control_off");
  const [color, text] = r.would_send ? [GREEN, "Every start check passes"]
    : reasons.length ? [reasons.some((x) => x.severity === "block") ? CORAL : AMBER, `Can't start: ${whyHead(reasons)}`]
    : !r.enabled && r.reason === "ok" ? [MUTED, "Every start check passes (control is off, so nothing would be sent)"]
    : [hard ? CORAL : AMBER, `A start would be refused: ${r.reason}`];
  const head = h("span", { class: "readyhead small" }, dot(color), h("span", null, text));
  const foot = h("p", { class: "muted tiny", style: "margin:2px 0 0" }, `Checked at ${clock(c.at / 1000)} · `, again, " · ",
    h("a", { href: "#settings/rules" }, "the rules"));
  const why = (r.why || []).length && !r.would_send ? whyList(r.why) : null;
  const checks = h("details", { class: "allchecks" },
    h("summary", { class: "muted small", "data-key": "ready-checks" }, "Every check"), checkList(r.checks));
  if (full) fill(el, h("p", { style: "margin:0" }, head), why, why ? checks : checkList(r.checks), foot);
  else fill(el, h("details", { open: ready.open || null, ontoggle: (e) => { ready.open = e.target.open; } },
    h("summary", { "data-key": "ready-summary" }, head), why, why ? checks : checkList(r.checks), foot));
}

function pendingCard(p, controlOn) {
  return h("div", { class: "card attention" },
    h("p", { class: "label" }, "Ready to start"),
    h("div", { class: "big", style: "font-size:22px" }, p.plan),
    h("p", { class: "muted small" }, `Window ${p.window} · raised ${clock(p.created)} · expires 30 min after`),
    p.camera ? h("img", { class: "snap", alt: `Current view from ${p.camera}`, src: `/api/frigate/snapshot/${encodeURIComponent(p.camera)}.jpg?height=360&t=${Date.now()}` }) : null,
    checkList(p.checks),
    controlOn ? null : h("p", { class: "muted small" }, "Control is switched off, so confirming is logged but does not start the robot."),
    h("div", { class: "row gap" },
      h("button", { type: "button", class: "btn pri", "data-key": "pending-start", onclick: async (e) => {
        e.target.disabled = true;
        try {
          const r = await postJSON("/api/schedule/pending/confirm");
          toast(r.sent ? `Started ${p.plan}.` : `Not started: ${r.refused || r.error || r.msg}`);
        } catch (err) { toast(err.message); }
        refreshLive();
      } }, "Start it"),
      h("button", { type: "button", class: "btn", "data-key": "pending-dismiss",
        onclick: async () => { await postJSON("/api/schedule/pending/dismiss"); refreshLive(); } }, "Not now")));
}

// Which camera the follow-cam shows (pinned, or the one whose coverage holds the robot), the yard cameras, the yard check.
async function followCamData() {
  let fc = null, cams = [];
  try { fc = await getJSON("/api/followcam"); } catch { /* frigate off */ }
  try { cams = (await getJSON("/api/frigate/cameras")).yard || []; } catch { /* ignore */ }
  return { cam: ops.pinnedCam || (fc && fc.camera) || null, pinned: !!ops.pinnedCam, reason: fc && fc.reason,
           covered: fc ? fc.covered : null, cams, yard: fc && fc.yard };
}

const yardLine = (yard) => h("span", { class: "small", style: `color:${yard.ok === false ? CORAL : yard.ok ? GREEN : MUTED}` },
  yard.ok === false ? `Yard not clear: ${yard.detail}` : yard.ok ? "Yard clear" : yard.detail);

const camChips = (d) => (d.cams.length > 1 ? h("div", { class: "camchips" },
  h("button", { type: "button", class: `btn slim${ops.pinnedCam ? "" : " on"}`, "data-key": "yardcam-auto",
                onclick: () => { ops.pinnedCam = null; refreshLive(); } }, "Auto"),
  d.cams.map((c) => h("button", { type: "button", class: `btn slim${ops.pinnedCam === c ? " on" : ""}`,
                                 "data-key": `yardcam-cam-${c}`,
                                 onclick: () => { ops.pinnedCam = c; refreshLive(); } }, c.replace(/_frigate$/, "")))) : null);

function followCamCard(d) {
  return h("div", { class: "card" },
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, "Follow-cam"),
      d.yard ? yardLine(d.yard) : null),
    d.cam ? camFeed(d.cam, 360, "snap")
          : h("p", { class: "muted small" }, "Pick yard cameras in ", h("a", { href: "#settings/cameras" }, "Settings"), " to see the robot here."),
    d.cam ? h("p", { class: "muted small" }, d.pinned ? `${d.cam} (pinned)` : `${d.cam} · ${d.reason}`) : null,
    d.cam ? h("p", { class: "small", style: "margin:4px 0 0" }, h("button", { type: "button", class: "linkbtn", onclick: () => setLiveMode("camera") }, "Camera layout"),
              h("span", { class: "muted" }, ": the picture big, with pause and stop beside it")) : null,
    camChips(d));
}

/* The follow-cam picture: Frigate's live MJPEG through the panel (/api/frigate/stream), one <img> kept across the
   renders Live does on every status event so the stream is not restarted. It runs only while it is on screen (Live,
   tab visible); if the stream fails it falls back to a snapshot every 3 s and tries the stream again every 30 s.
   An MJPEG that ends quietly (Frigate restarted) just freezes, so the stream is also reopened every 5 minutes. */
const FEED_RENEW_MS = 5 * 60000;
const FEED_FPS = 5;  // Frigate sends high-quality JPEGs: 720p at 5 fps is ~10 Mbit/s
function camFeed(cam, height, cls) {
  const key = `${cam}|${height}`;
  if (ops.feed && ops.feed.key !== key) stopFeed();
  if (!ops.feed) {
    const img = h("img", { id: "followimg", alt: `Live view from ${cam}` });
    ops.feed = { key, cam, height, img, mode: null, failedAt: 0 };
    img.onerror = () => {
      const f = ops.feed;
      if (f && f.img === img && f.mode === "stream") { f.mode = "snap"; f.failedAt = Date.now(); feedTick(); }
    };
    playFeed();
  } else if (ops.feed.mode !== "stream") {
    setTimeout(feedTick, 0);  // back on Live: resume once the picture is on the page
  }
  ops.feed.img.className = cls || "";
  return ops.feed.img;
}

function playFeed() {
  const f = ops.feed;
  if (!f) return;
  const c = encodeURIComponent(f.cam);
  if (f.mode !== "snap") {
    f.mode = "stream";
    f.startedAt = Date.now();
    f.img.src = `/api/frigate/stream/${c}.mjpeg?height=${f.height}&fps=${FEED_FPS}&t=${Date.now()}`;
  } else {
    f.img.src = `/api/frigate/snapshot/${c}.jpg?height=${f.height}&t=${Date.now()}`;
  }
}

// Ends the stream's connection (an <img> keeps loading it even off screen).
function pauseFeed() {
  const f = ops.feed;
  if (f && (f.mode === "stream" || f.mode === "snap")) {
    f.img.removeAttribute("src");
    f.mode = f.mode === "snap" ? "snap-paused" : null;
  }
}

function stopFeed() {
  if (!ops.feed) return;
  pauseFeed();
  ops.feed.img.onerror = null;
  ops.feed = null;
}

function feedTick() {
  const f = ops.feed;
  if (!f) return;
  const onScreen = f.img.isConnected && state.view === "live" && !document.hidden;
  if (!onScreen) { pauseFeed(); return; }
  if (f.mode === "snap-paused") f.mode = "snap";
  if (f.mode === "snap" && Date.now() - f.failedAt > 30000) f.mode = null;  // try the stream again
  if (f.mode !== "stream" || Date.now() - f.startedAt > FEED_RENEW_MS) playFeed();
}

function startFollowTimer() {
  clearInterval(ops.followTimer);
  ops.followTimer = setInterval(feedTick, 3000);
  document.addEventListener("visibilitychange", feedTick);
}

function controlCard(s) {
  const o = s.ops || {};
  const on = o.control_enabled;
  const plans = state.plansCache || [];
  // Live re-renders on every status event, so the picked plan lives in ops.planId, not in the <select>.
  if (!plans.some((p) => p.id === ops.planId)) ops.planId = plans[0]?.id ?? null;
  const plan = plans.find((p) => p.id === ops.planId);
  const startArgs = () => ({ plan_id: ops.planId });
  const startLabel = plan ? `Start ${plan.name}` : "Start plan";
  const disabled = !on || !routeOk(o);
  const why = !on ? ["Control is switched off. ", h("a", { href: "#settings/safety" }, "Switch it on in Settings"), "."]
            : !routeOk(o) ? noRouteWhy(o) : null;
  // Only what makes sense in the robot's state is offered; when the state is unknown, everything is (the server's
  // interlocks still decide).
  const act = s.activity, known = !!act && !s.lost_contact && s.state !== "waiting for data";
  const running = !!s.run || !["docked", "idle"].includes(act);
  const no = (ok, text) => (known && !ok ? text : null);
  const idle = {
    start_plan: no(!running, "A run is already going."),
    pause: no(!NOT_PAUSABLE.has(act), `Nothing to pause: the robot is ${s.state}.`),
    resume: no(HELD.has(act), `Nothing to resume: the robot is ${s.state}.`),
    stop: no(running, `Nothing to stop: the robot is ${s.state}.`),
    cmd_recharge: no(!NOT_DOCKABLE.has(act), act === "docked" ? "Already on the dock." : `Already on its way: ${s.state}.`),
  };
  // A touch screen never shows a disabled button's title, so the same reasons go in one line under the grid
  // (.whyline). The button's name replaces "Nothing to pause:", so buttons off for the same state share one entry
  // ("Pause, Stop: the robot is docked."). Control off and no plans already say so above the buttons.
  const labels = { start_plan: "Start", pause: "Pause", resume: "Resume", stop: "Stop",
    cmd_recharge: "Return to dock" };
  const offWhy = new Map();
  for (const [cmd, text] of Object.entries(idle)) {
    const t = text?.replace(/^Nothing to \w+: /, "");
    if (t) offWhy.set(t, [...(offWhy.get(t) || []), labels[cmd]]);
  }
  const whyLine = [...offWhy].map(([t, names]) => `${names.join(", ")}: ${t[0].toLowerCase()}${t.slice(1)}`).join(" ");
  const btn = (cmd, label, args) => h("button", { type: "button", class: "btn", "data-key": `ctl-${cmd}`,
    disabled: disabled || !!idle[cmd] || null,
    title: idle[cmd] || null, onclick: () => openConfirm(cmd, label, args) }, label);
  return h("div", { class: "card" },
    h("p", { class: "label" }, "Control"),
    why ? h("p", { class: "muted small" }, why) : null,
    plans.length ? [
      h("label", { for: "ctlplan", class: "muted small" }, "Plan"),
      h("select", { id: "ctlplan", class: "field", "data-key": "ctl-plan",
        onchange: (e) => { ops.planId = Number(e.target.value); closeConfirm(); refreshLive(); } },
        plans.map((p) => h("option", { value: p.id, selected: p.id === ops.planId || null }, p.name))),
      h("p", { class: "muted small", style: "margin:-4px 0 8px" }, `Mows ${planAreas(plan) || "no areas"}`),
      readinessView(plan.id)]
      : h("p", { class: "muted small" }, "No plans on the robot. Make them in the Yarbo app; they appear here (",
          h("a", { href: "#help/not-yet" }, "the panel cannot make plans yet"), ")."),
    h("div", { class: "ctlgrid" },
      h("button", { type: "button", class: "btn pri wide", "data-key": "ctl-start_plan",
        disabled: disabled || !plan || !!idle.start_plan || null, title: idle.start_plan || null,
        onclick: () => openConfirm("start_plan", startLabel, startArgs()) }, plan ? `Start ${plan.name}…` : "Start plan…"),
      btn("pause", "Pause"), btn("resume", "Resume"), btn("stop", "Stop"), btn("cmd_recharge", "Return to dock")),
    whyLine ? h("p", { class: "whyline muted tiny", style: "margin-top:6px" }, whyLine) : null);
}

/* A command's confirmation is a modal over the page (it used to open under the buttons, below the fold on a phone or
   in the camera layout). It lives outside Live, so the redraw on every status event leaves it alone; Esc, Cancel or a
   tap outside closes it. Its head says whether the command would go; Send is offered only when it would (soft
   checks: once the override is ticked), so a refused start is never a button that seems to do nothing. */
async function openConfirm(cmd, label, args = {}, previewOnly = false) {
  closeConfirm();
  const opener = document.activeElement;
  const box = h("div", { class: "imgdialogbox confirmbox", role: "dialog", "aria-modal": "true", "aria-labelledby": "confirm-title" });
  const overlay = h("div", { class: "imgdialog", onclick: (e) => { if (e.target === overlay) closeConfirm(); } });
  const onKey = (e) => { if (e.key === "Escape") closeConfirm(); };
  const c = ops.confirm = { cmd, label, args, previewOnly, preview: null, override: false, box,
    close: () => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      $(".shell")?.removeAttribute("inert");
      if (opener && opener.isConnected && opener.focus) opener.focus();
    } };
  overlay.append(box);
  $(".shell")?.setAttribute("inert", "");
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  drawConfirm();
  try { c.preview = await postJSON("/api/control/preview", { cmd, args }); } catch (e) { c.error = e.message; }
  if (ops.confirm === c) drawConfirm();
}

function closeConfirm() {
  const c = ops.confirm;
  ops.confirm = null;
  if (c) c.close();
}

function drawConfirm() {
  const c = ops.confirm, p = c.preview;
  const softOnly = p && !p.would_send && p.checks.every((x) => x.hard ? x.ok === true : true);
  const canSend = p && p.enabled && (p.would_send || (softOnly && c.override && c.cmd === "start_plan"));
  const head = !p ? null
    : p.would_send ? h("p", { class: "small", style: `color:${GREEN};margin:0 0 6px` }, "Every check passes.")
    : softOnly && !c.previewOnly && c.cmd === "start_plan" ? h("p", { class: "small", style: `color:${AMBER};margin:0 0 6px` },
        `Only soft checks fail (${p.reason}). Tick the override below to start anyway.`)
    : h("p", { class: "small", style: `color:${CORAL};margin:0 0 6px` }, h("b", null, "Can't send: "), p.reason);
  fill(c.box,
    h("h3", { id: "confirm-title" }, c.previewOnly ? `Gate check · ${c.label}` : `Send “${c.label}”?`),
    c.error ? h("p", { class: "small", style: `color:${CORAL}` }, c.error) : null,
    head,
    p ? checkList(p.checks) : h("p", { class: "muted small" }, "Checking interlocks…"),
    p && !p.enabled ? h("p", { class: "muted small" }, "Control is switched off; nothing will be sent.") : null,
    !c.previewOnly && softOnly && c.cmd === "start_plan" ? h("label", { class: "row gap small" },
      h("input", { type: "checkbox", checked: c.override || null, onchange: (e) => { c.override = e.target.checked; drawConfirm(); } }),
      "Override the soft checks above") : null,
    h("div", { class: "row gap", style: "justify-content:flex-end;margin-top:12px" },
      h("button", { type: "button", class: "btn", onclick: closeConfirm }, c.previewOnly ? "Close" : "Cancel"),
      c.previewOnly ? null : h("button", { type: "button", class: "btn pri", disabled: !canSend || null, onclick: async (e) => {
        e.target.disabled = true;
        try { reportSend(await postJSON("/api/control/send", { cmd: c.cmd, args: c.args, confirm: true, override: c.override }), c.label); }
        catch (err) { toast(err.message); }
        if (ops.confirm === c) closeConfirm();
        refreshLive();
      } }, "Send")));
  if (!c.box.contains(document.activeElement)) c.box.querySelector("button:not([disabled])")?.focus();  // a redraw drops focus
}

function refreshLive() {
  if (state.status && state.view === "live") renderLive(state.status);
}

/* ------------------------------------------------------------- schedule */
async function renderSchedule() {
  const panel = $("#p-schedule");
  let d;
  try { d = await getJSON("/api/schedule"); } catch (e) { fill(panel, h("p", { class: "muted pad" }, e.message)); return; }
  await currentStatus();
  state.plansCache = d.plans;
  const planName = (id) => (d.plans.find((p) => p.id === id) || {}).name || (id === null ? "no plan" : `plan ${id}`);
  // The start checks for the plan the next window would run (or the robot's first plan).
  const checkPlan = (d.windows.find((w) => w.enabled && w.plan_id !== null) || {}).plan_id ?? d.plans[0]?.id ?? null;
  const save = async (windows) => {
    try { await putJSON("/api/schedule/windows", windows); toast("Schedule saved."); } catch (e) { toast(e.message); }
    ops.editing = null;
    renderSchedule();
  };
  const upcoming = d.upcoming.map((u) => {
    const w = u.window, ev = u.last_eval;
    return h("div", { class: "card" },
      h("div", { class: "row gap", style: "justify-content:space-between;flex-wrap:wrap" },
        h("b", null, w.name || planName(w.plan_id)),
        h("span", { class: "muted small" }, u.active ? "window open now" : u.next ? `next ${when(u.next)}` : "no upcoming window")),
      kv("Plan", planName(w.plan_id)),
      kv("When", `${w.days.map((x) => DAYS[x]).join(", ") || "no days"} · ${w.start}–${w.end}`),
      kv("Head", w.head), kv("Start", w.auto_start ? "automatically (if control is on)" : "ask me first"),
      u.today ? kv("Today", u.today) : null,
      ev ? h("div", null,
        h("p", { class: "muted small" }, `Checked at ${clock(ev.at)}: ${ev.ok ? "every check passes" : (ev.why || []).length ? "it won't start yet, because" : ev.reason}`),
        ev.ok || !(ev.why || []).length ? checkList(ev.checks) : whyList(ev.why)) : null,
      h("div", { class: "row gap" },
        h("button", { type: "button", class: "btn slim", onclick: () => { ops.editing = { ...w }; renderSchedule(); } }, "Edit"),
        h("button", { type: "button", class: "btn slim", onclick: () => save(d.windows.map((x) => (x.id === w.id ? { ...x, enabled: !x.enabled } : x))) }, w.enabled ? "Disable" : "Enable"),
        h("button", { type: "button", class: "btn slim", onclick: () => { if (confirm(`Delete this window?`)) save(d.windows.filter((x) => x.id !== w.id)); } }, "Delete")));
  });
  fill(panel,
    h("div", { class: "healthbar" },
      h("p", { class: "muted small", style: "margin:0" }, "The panel owns scheduling. Inside a window it checks the gates every minute and asks you to confirm once they all pass."),
      h("button", { type: "button", class: "btn pri", onclick: () => { ops.editing = { days: [1, 4], start: "09:00", end: "14:00", head: "mower", plan_id: d.plans[0]?.id ?? null, enabled: true, auto_start: false, name: "" }; renderSchedule(); } }, "Add window")),
    d.pending ? h("div", { class: "pad0" }, pendingCard(d.pending, state.status?.ops?.control_enabled)) : null,
    robotSchedulesCard(d.robot_schedules, planName),
    ops.editing ? windowEditor(ops.editing, d, save) : null,
    h("div", { class: "healthgrid" },
      h("div", { class: "card" },
        h("p", { class: "label" }, checkPlan === null ? "Right now" : `Right now · ${planName(checkPlan)}`),
        readinessView(checkPlan, true)),
      upcoming.length ? upcoming : h("div", { class: "card" }, h("p", { class: "muted" }, "No windows yet."))),
    h("div", { class: "card", style: "margin:12px" }, h("p", { class: "label" }, "History"),
      d.log.length ? d.log.map((l) => h("div", null,
        kv(`${when(l.ts)} · ${l.outcome}`, `${planName((d.windows.find((w) => w.id === l.window_id) || {}).plan_id)}${(l.detail.why || []).length ? "" : ` ${l.detail.reason || l.detail.result || ""}`}`),
        (l.detail.why || []).length ? whyList(l.detail.why) : null))
                   : h("p", { class: "muted small" }, "Nothing yet.")));
}

// Schedules saved in the Yarbo app live on the robot and start mows without any of the panel's run rules (#64). The
// panel never writes them; it says they are there.
function robotSchedulesCard(list, planName) {
  if (!list || !list.length) return null;
  return h("div", { class: "card", style: "margin:12px" },
    h("p", { class: "label" }, h("span", { style: `color:${AMBER}` }, "On the robot itself")),
    h("p", { class: "small", style: "margin:0 0 6px" },
      `${list.length === 1 ? "A schedule was" : `${list.length} schedules were`} saved in the Yarbo app. They start mows without the run rules (dry lawn, yard clear, someone home, daylight). Remove them in the app to let the panel decide.`),
    list.map((s) => kv(s.name || `Schedule ${s.id}`,
      `${s.enabled ? "on" : "off"} · ${planName(s.plan_id)} · ${s.days.join(", ") || "no days"} · ${s.start_utc || "?"}–${s.end_utc || "?"} UTC`)));
}

function windowEditor(w, d, save) {
  const field = (label, input) => h("label", { class: "formrow" }, h("span", { class: "muted small" }, label), input);
  const controlOn = state.status?.ops?.control_enabled;
  return h("div", { class: "card", style: "margin:12px" },
    h("p", { class: "label" }, w.id ? "Edit window" : "New window"),
    h("div", { class: "formgrid" },
      field("Name (optional)", h("input", { class: "field", value: w.name || "", oninput: (e) => { w.name = e.target.value; } })),
      field("Plan", h("select", { class: "field", onchange: (e) => { w.plan_id = Number(e.target.value); } },
        d.plans.map((p) => h("option", { value: p.id, selected: p.id === w.plan_id }, p.name)))),
      field("From", h("input", { class: "field", type: "time", value: w.start, oninput: (e) => { w.start = e.target.value; } })),
      field("To", h("input", { class: "field", type: "time", value: w.end, oninput: (e) => { w.end = e.target.value; } })),
      field("Head", h("select", { class: "field", onchange: (e) => { w.head = e.target.value; } },
        ["mower", "snow", "any"].map((x) => h("option", { value: x, selected: x === w.head }, x))))),
    h("fieldset", { class: "days" }, h("legend", { class: "muted small" }, "Days"),
      DAYS.map((name, i) => h("label", null, h("input", { type: "checkbox", checked: w.days.includes(i) || null,
        onchange: (e) => { w.days = e.target.checked ? [...new Set([...w.days, i])] : w.days.filter((x) => x !== i); } }), name))),
    h("label", { class: "row gap small" }, h("input", { type: "checkbox", checked: w.auto_start || null, onchange: (e) => { w.auto_start = e.target.checked; } }),
      "Start automatically when every gate passes", controlOn ? null : h("span", { class: "muted" }, " (only works once control is on)")),
    h("div", { class: "row gap" },
      h("button", { type: "button", class: "btn pri", onclick: () => {
        // The editor holds a copy; replace (or add) it in the saved list.
        getJSON("/api/schedule").then((cur) => save(w.id ? cur.windows.map((x) => (x.id === w.id ? w : x)) : [...cur.windows, w]));
      } }, "Save"),
      h("button", { type: "button", class: "btn", onclick: () => { ops.editing = null; renderSchedule(); } }, "Cancel")));
}

/* ---------------------------------------------------------- maintenance */
async function renderMaintenance() {
  const panel = $("#p-maintenance");
  let d;
  try { d = await getJSON("/api/maintenance"); } catch (e) { fill(panel, h("p", { class: "muted pad" }, e.message)); return; }
  const saveItems = async (items) => {
    try { await putJSON("/api/maintenance/items", items); } catch (e) { toast(e.message); }
    renderMaintenance();
  };
  const raw = d.items.map(({ id, key, name, unit, interval, done_at, baseline, note }) => ({ id, key, name, unit, interval, done_at, baseline, note }));
  const color = (due) => (due === "overdue" || due === "due" ? CORAL : due === "due soon" ? AMBER : due === "ok" ? GREEN : MUTED);
  const UNIT = { blade_hours: "h", distance_km: "km", mows: "runs", runs: "runs", charge_cycles: "charges", days: "days", head_change: "" };
  const count = (it, v) => `${num(v, ["mows", "runs", "charge_cycles", "head_change"].includes(it.unit) ? 0 : 1)}${UNIT[it.unit] ? ` ${UNIT[it.unit]}` : ""}`;
  fill(panel,
    h("div", { class: "healthbar" },
      h("p", { class: "muted small", style: "margin:0" }, "Counters come from recorded runs. Intervals start at Yarbo's own advice where it gives one; “Done” restarts the count."),
      h("span", { class: "muted small" }, `blade ${num(d.counters.blade_hours, 1)} h · ${num(d.counters.distance_km, 1)} km · ${d.counters.runs} runs · ${d.counters.charge_cycles} charges`)),
    h("div", { class: "healthgrid" }, d.items.map((it) => h("div", { class: "card" },
      h("div", { class: "row gap", style: "justify-content:space-between" }, h("b", null, it.name),
        h("span", { class: "small", style: `color:${color(it.due)}` }, it.due)),
      h("p", { class: "muted small" }, it.unit_label),
      it.recommended ? h("p", { class: "muted tiny", style: "margin:0 0 6px" },
        h("a", { href: it.recommended.source, target: "_blank", rel: "noopener" }, it.recommended.says),
        it.recommended.interval !== null && (it.recommended.interval !== it.interval || it.recommended.unit !== it.unit)
          ? [" · ", h("button", { type: "button", class: "linkbtn", style: "font-size:inherit;min-height:0",
              onclick: () => saveItems(raw.map((x) => (x.id === it.id ? { ...x, interval: it.recommended.interval, unit: it.recommended.unit,
                                                                     baseline: it.recommended.unit === it.unit ? x.baseline : (it.recommended.unit === "days" ? x.done_at / 86400 : d.counters[it.recommended.unit] || 0) } : x))) },
              `use ${count({ unit: it.recommended.unit }, it.recommended.interval)}`)] : null) : null,
      it.pct !== null ? h("div", { class: "bar" }, h("div", { class: "fillbar", style: `width:${Math.min(100, it.pct)}%;background:${color(it.due)}` })) : null,
      kv("Since last done", `${count(it, it.used)}${it.interval ? ` of ${count(it, it.interval)}` : ""} · ${when(it.done_at)}`),
      it.unit !== "head_change" ? h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Interval"),
        h("input", { class: "field", type: "number", min: "0", step: "any", value: it.interval ?? "", placeholder: "your choice",
          onchange: (e) => saveItems(raw.map((x) => (x.id === it.id ? { ...x, interval: e.target.value ? Number(e.target.value) : null } : x))) })) : null,
      h("div", { class: "row gap" },
        h("button", { type: "button", class: "btn slim", onclick: async () => { await postJSON(`/api/maintenance/${it.id}/done`, {}); toast(`${it.name}: marked done.`); renderMaintenance(); } }, "Mark done"),
        h("button", { type: "button", class: "btn slim", onclick: () => { if (confirm(`Remove “${it.name}”?`)) saveItems(raw.filter((x) => x.id !== it.id)); } }, "Remove"))))),
    h("div", { class: "card", style: "margin:12px" }, h("p", { class: "label" }, "Add an item"),
      h("div", { class: "formgrid" },
        h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Name"), h("input", { class: "field", id: "mnew" })),
        h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Counts"),
          h("select", { class: "field", id: "mnewunit" }, Object.entries(d.units).map(([k, v]) => h("option", { value: k }, v)))),
        h("label", { class: "formrow" }, h("span", { class: "muted small" }, "Interval"), h("input", { class: "field", id: "mnewint", type: "number", min: "0", step: "any" }))),
      h("button", { type: "button", class: "btn pri", onclick: () => {
        const name = $("#mnew").value.trim();
        if (!name) { toast("Give it a name."); return; }
        const unit = $("#mnewunit").value;
        saveItems([...raw, { name, unit, interval: $("#mnewint").value ? Number($("#mnewint").value) : null, baseline: d.counters[unit] || 0 }]);
      } }, "Add")));
}

/* ------------------------------------------------- camera polygons on map */
const shortCam = (c) => c.replace(/_frigate$/, "").replace(/_/g, " ");

// Area of a [[lon, lat], ...] ring in m²: shoelace in metres around the first corner (fine at yard scale).
function ringAreaM2(ring) {
  if (!ring || ring.length < 3) return 0;
  const [lon0, lat0] = ring[0], kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110540;
  const pts = ring.map(([lon, lat]) => [(lon - lon0) * kx, (lat - lat0) * ky]);
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

const validPolys = (polys) => Object.fromEntries(Object.entries(polys || {}).filter(([, r]) => ringAreaM2(r) >= 1));

function opsMapInit() {
  const map = state.map;
  ops.camMarkers = [];
  for (const id of ["cameras", "draw"]) map.addSource(id, { type: "geojson", data: EMPTY });
  map.addLayer({ id: "cameras-fill", type: "fill", source: "cameras", paint: { "fill-color": MAP.camera, "fill-opacity": 0.06 } }, "track-line");
  map.addLayer({ id: "cameras-line", type: "line", source: "cameras", paint: { "line-color": MAP.camera, "line-width": 1.5, "line-dasharray": [0.5, 2] } }, "track-line");
  map.addLayer({ id: "draw-fill", type: "fill", source: "draw", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": SKY, "fill-opacity": 0.25 } });
  map.addLayer({ id: "draw-line", type: "line", source: "draw", filter: ["!=", ["geometry-type"], "Point"], paint: { "line-color": SKY, "line-width": 2.5 } });
  map.addLayer({ id: "draw-pts", type: "circle", source: "draw", filter: ["==", ["geometry-type"], "Point"],
                 paint: { "circle-radius": ["case", ["get", "first"], 8, 5], "circle-color": "#F2F5F3",
                          "circle-stroke-color": SKY, "circle-stroke-width": 2 } });
  loadCameraPolygons();
}

// Zones load after this file's layers are added and also slot in under the track, which would bury the coverage.
function raiseCameraLayers() {
  const map = state.map;
  if (!map || !map.getLayer("track-line")) return;
  for (const id of ["cameras-fill", "cameras-line"]) if (map.getLayer(id)) map.moveLayer(id, "track-line");
}

function applyCamVisibility() {
  (ops.camMarkers || []).forEach((m) => { m.getElement().style.display = camerasVisible() && state.layers.labels ? "" : "none"; });
}

async function loadCameraPolygons() {
  let d;
  try { d = await getJSON("/api/frigate/cameras"); } catch { return; }
  const polys = validPolys(d.polygons);
  setSource("cameras", { type: "FeatureCollection", features: Object.entries(polys).map(([cam, ring]) => ({
    type: "Feature", properties: { cam }, geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } })) });
  raiseCameraLayers();
  (ops.camMarkers || []).forEach((m) => m.remove());
  ops.camMarkers = Object.entries(polys).map(([cam, ring]) => new maplibregl.Marker({ element: h("div", { class: "zlabel cam" }, shortCam(cam)) })
    .setLngLat(centroid([...ring, ring[0]])).addTo(state.map));
  applyCamVisibility();
}

// `back` is where to return once drawing is over (Settings → Cameras); drawing itself happens on the Live map.
function startDraw(camera, back = null) {
  if (ops.drawing) { ops.drawing.back = null; ops.drawing.finish(false); }   // pre-empted: stay on the map
  $(".drawpanel")?.remove();
  if (location.hash !== "#live") location.hash = "#live";
  const map = state.map;
  const d = { camera, pts: [], hover: null, back };
  ops.drawing = d;
  syncLiveMode();
  map.doubleClickZoom.disable();
  map.getCanvas().style.cursor = "crosshair";
  if (!state.layers.cameras) $('.tool[data-layer="cameras"]')?.click();
  requestAnimationFrame(() => { if (!state.fitted) state.fitted = fitYard(); });

  const near = (a, b, tol) => { const pa = map.project(a), pb = map.project(b); return Math.hypot(pa.x - pb.x, pa.y - pb.y) <= tol; };
  const status = h("p", { class: "small" });
  const save = h("button", { type: "button", class: "btn pri", onclick: () => d.finish(true) }, "Save");
  const undo = h("button", { type: "button", class: "btn", onclick: () => { d.pts.pop(); redraw(); } }, "Undo");
  const redraw = () => {
    const pts = d.pts, ring = d.hover && pts.length ? [...pts, d.hover] : pts;
    const feats = pts.map((p, i) => ({ type: "Feature", properties: { first: i === 0 && pts.length >= 3 }, geometry: { type: "Point", coordinates: p } }));
    if (ring.length >= 3) feats.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } });
    else if (ring.length === 2) feats.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ring } });
    setSource("draw", { type: "FeatureCollection", features: feats });
    const area = ringAreaM2(pts);
    status.textContent = pts.length < 3 ? `${pts.length} corner${pts.length === 1 ? "" : "s"} so far; at least 3 needed.`
                                        : `${pts.length} corners · ${Math.round(area)} m². Click the first corner (or Save) to finish.`;
    save.disabled = !(pts.length >= 3 && area >= 1);
    undo.disabled = !pts.length;
  };
  const onClick = (e) => {
    const p = [e.lngLat.lng, e.lngLat.lat];
    if (d.pts.length >= 3 && near(p, d.pts[0], 12)) { d.finish(true); return; }
    if (d.pts.length && near(p, d.pts[d.pts.length - 1], 8)) return;  // a double-click or a shaky click: same corner
    d.pts.push(p);
    redraw();
  };
  const onMove = (e) => { if (d.pts.length) { d.hover = [e.lngLat.lng, e.lngLat.lat]; redraw(); } };
  const onOut = () => { d.hover = null; redraw(); };
  const onKey = (e) => {
    if (e.target.closest?.("input, textarea, select")) return;
    if (e.key === "Escape") d.finish(false);
    else if (e.key === "Enter") d.finish(true);
    else if (e.key === "Backspace" || (e.key === "z" && (e.ctrlKey || e.metaKey))) { e.preventDefault(); d.pts.pop(); redraw(); }
  };
  const panel = h("div", { class: "alignpanel drawpanel", role: "dialog", "aria-label": `Draw coverage for ${shortCam(camera)}` });
  d.finish = async (keep) => {
    if (ops.drawing !== d) return;
    if (keep && !(d.pts.length >= 3 && ringAreaM2(d.pts) >= 1)) { toast("Add at least 3 separate corners first."); return; }
    map.off("click", onClick);
    map.off("mousemove", onMove);
    map.off("mouseout", onOut);
    document.removeEventListener("keydown", onKey);
    map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = "";
    ops.drawing = null;
    syncLiveMode();
    setSource("draw", EMPTY);
    const leave = () => { panel.remove(); if (d.back) location.hash = d.back; };
    if (!keep) { leave(); return; }
    let cams;
    try {
      cams = await getJSON("/api/frigate/cameras");
      await putJSON("/api/settings/camera_polygons", { ...validPolys(cams.polygons), [camera]: d.pts });
    } catch (e) { toast(e.message); leave(); return; }
    await loadCameraPolygons();
    const left = cams.yard.filter((c) => c !== camera && !(ringAreaM2(cams.polygons[c]) >= 1));
    fill(panel,
      h("p", { class: "label" }, `Saved: ${shortCam(camera)} · ${Math.round(ringAreaM2(d.pts))} m²`),
      h("p", { class: "muted small" }, left.length ? "Yard cameras still without coverage:" : "Every yard camera has its coverage."),
      left.length ? h("div", { class: "camchips" }, left.map((c) => h("button", { type: "button", class: "btn slim", onclick: () => startDraw(c, d.back) }, shortCam(c)))) : null,
      h("div", { class: "row gap" }, h("button", { type: "button", class: "btn", onclick: leave }, "Done")));
  };
  map.on("click", onClick);
  map.on("mousemove", onMove);
  map.on("mouseout", onOut);
  document.addEventListener("keydown", onKey);
  fill(panel,
    h("p", { class: "label" }, `Coverage: ${shortCam(camera)}`),
    h("img", { class: "snap", alt: `What ${shortCam(camera)} sees now`, src: `/api/frigate/snapshot/${encodeURIComponent(camera)}.jpg?height=240&t=${Date.now()}` }),
    h("p", { class: "muted small" }, "Click the corners of the ground this camera can see. Click the first corner again to finish. Backspace undoes a corner; Esc cancels."),
    status,
    h("div", { class: "row gap" }, save, undo, h("button", { type: "button", class: "btn", onclick: () => d.finish(false) }, "Cancel")));
  $(".mapwrap").append(panel);
  redraw();
}

startFollowTimer();
