/* The Cameras tile on Live: the robot's own cameras (front, left, right, back), one at a time (§13).
   Loaded after ops.js and uses its helpers (h, fill, postJSON, getJSON, state, dot, toast).

   What opening it does, in this order: ask the controller to switch the cameras on (smart_vision_control, which needs
   Control on), ask the panel for the camera's join details (POST /api/video/token), load Agora's Web SDK from this
   panel (web/vendor/, never a CDN), then join the robot's channel as an audience member and play uid 1's video.
   The video goes from Agora's network to this browser; none of it passes through the panel.

   Rules this file keeps (tests/test_cameras_js.py checks the ones a test can):
   - It never switches the cameras off: state 0 would also cut the official app's view (docs/dev/protocol.md).
   - Video runs only while the tile is open and the tab is showing Live. Closing it, changing camera, leaving Live,
     hiding the tab and closing the page all leave the channel.
   - The token, key and salt live in local variables of one function call. They are not kept on `cams`, in the DOM, in
     storage or the URL, and nothing here writes to the console. (The SDK keeps a few `websdk_ng_*` items of its own
     in this panel's localStorage: a device id, a cache of its settings and of a preload. This file clears them when
     the SDK has loaded and when the channel is left; none of them holds the token, key or salt.)
   - Agora's SDK is told, before any client exists, not to send usage statistics, not to upload logs, not to fetch
     remote configuration (which could switch the two back on mid-session), not to preload the access point or
     preconnect, and to use a random device id. See camerasQuietSdk. */
"use strict";

const CAM_CHOICES = [["front", "Front"], ["left", "Left"], ["right", "Right"], ["back", "Back"]];
const CAM_SDK_URL = "/vendor/AgoraRTC_N-production.js";
const CAM_ROBOT_UID = 1;                           // the robot publishes as uid 1 (app/video.py ROBOT_UID)
const CAM_SLOW_MS = 15000;                         // joined but no video yet: say why it may be
const CAM_SETTINGS_MS = 20000;                     // how long a read of the video setting is trusted
const CAM_SDK_TIMEOUT_MS = 20000;                  // a script load that hangs gives up, so the next try loads again
const CAM_SDK_STORAGE = "websdk_ng_";              // the prefix of what the SDK itself keeps in localStorage
// Switched off for the SDK (4.24.9, read from the vendored bundle). Each is written with the third argument true: the
// SDK ignores a plain setParameter for a key a remote or cached configuration has already set.
const CAM_SDK_OFF = [
  ["ENABLE_EVENT_REPORT", false],                  // usage statistics
  ["UPLOAD_LOG", false],                           // log upload
  ["ENABLE_CONFIG_DISTRIBUTE", false],             // remote configuration, which can set the two above again
  ["ENABLE_PRELOAD", false],                       // re-requests the access point with the token every 2 minutes
  ["ENABLE_CREATE_CLIENT_PRECONNECT", false],      // requests to Agora hosts when a client is created...
  ["CREATE_CLIENT_PRECONNECT_MODE", "report"],     // ...and "request" is the mode that fetches; "report" does not
];

const cams = { open: false, camera: savedCamera(), phase: "closed", message: "", hint: "", client: null, ticket: 0,
               slowTimer: null, leaving: Promise.resolve(), settings: null, settingsAt: 0, card: null, stage: null, sdk: null,
               subscribed: 0 };

function savedCamera() {
  try {
    const c = localStorage.getItem("yarbo.robotCamera");
    if (CAM_CHOICES.some(([k]) => k === c)) return c;
  } catch { /* no storage */ }
  return "front";
}

// Whether the tile shows at all: the video setting on and the service address set (the panel says so), or the demo,
// which shows a placeholder and never contacts anything.
async function camerasAvailable(s) {
  if (s.demo) return true;
  if (!cams.settings || Date.now() - cams.settingsAt > CAM_SETTINGS_MS) {
    try { cams.settings = await getJSON("/api/settings"); cams.settingsAt = Date.now(); } catch { /* keep the last answer */ }
  }
  const d = cams.settings;
  return !!(d && d.robot_video_enabled && d.robot_video_configured);
}

async function camerasCard(s) {
  if (!(await camerasAvailable(s))) {
    if (cams.open) camerasStop("Robot video was switched off.");
    return null;
  }
  cams.demo = !!s.demo;
  cams.controlOn = !!(s.ops && s.ops.control_enabled);
  if (!cams.card) {
    cams.stage = h("div", { class: "camsstage", role: "img", "aria-label": "Live view from the robot's camera" });
    cams.card = h("div", { class: "card camscard" });
  }
  camerasDraw();
  return cams.card;
}

const CAM_PHASE_COLOR = { connecting: () => AMBER, waiting: () => AMBER, playing: () => GREEN, error: () => CORAL };

function camerasDraw() {
  if (!cams.card) return;
  const colour = (CAM_PHASE_COLOR[cams.phase] || (() => MUTED))();
  const label = { connecting: "Connecting…", waiting: "Waiting for the robot", playing: "Live", error: "Error",
                  closed: "Closed" }[cams.phase] || "Closed";
  fill(cams.card,
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, "Cameras ",
        h("span", { class: "tip", tabindex: "0", title: "The robot's own cameras, not the yard cameras. They come through Yarbo's cloud and Agora's network, one at a time, and only while this is open." }, "ⓘ")),
      cams.demo ? h("span", { class: "small muted" }, "Demo")
                : h("span", { class: "small", style: `color:${colour}` }, dot(colour), " ", label)),
    cams.demo ? h("p", { class: "muted small", style: "margin:6px 0 0" }, "The demo robot has no cameras. A real robot shows its video here.")
    : cams.open ? [
      h("div", { class: "camchips", role: "group", "aria-label": "Robot camera" },
        CAM_CHOICES.map(([k, name]) => h("button", { type: "button", class: `btn slim${cams.camera === k ? " on" : ""}`,
          "aria-pressed": String(cams.camera === k), title: `The robot's ${name.toLowerCase()} camera`,
          onclick: () => camerasPick(k) }, name)),
        h("button", { type: "button", class: "btn slim", title: "Stop the video and leave the channel", onclick: () => camerasStop() }, "Close")),
      cams.stage,
      cams.message ? h("p", { class: "small", style: `margin:6px 0 0;color:${colour}`, role: "status" }, cams.message) : null,
      cams.hint ? h("p", { class: "muted small", style: "margin:4px 0 0" }, cams.hint) : null,
      cams.phase === "error" ? h("button", { type: "button", class: "btn slim", style: "margin-top:6px", onclick: () => camerasStart({ on: true }) }, "Try again") : null]
    : [
      h("div", { class: "row gap", style: "margin-top:6px" },
        h("button", { type: "button", class: "btn slim", title: "Switch the robot's cameras on and show one here. Needs Control on.",
          onclick: () => camerasOpen() }, "Open")),
      cams.message ? h("p", { class: "muted small", style: "margin:6px 0 0", role: "status" }, cams.message) : null]);
}

function camerasSet(phase, message, hint) {
  cams.phase = phase;
  cams.message = message || "";
  cams.hint = hint || "";
  camerasDraw();
}

function camerasOpen() {
  cams.open = true;
  camerasStart({ on: true });
}

function camerasPick(camera) {
  if (camera === cams.camera && cams.phase !== "error") return;
  cams.camera = camera;
  try { localStorage.setItem("yarbo.robotCamera", camera); } catch { /* no storage */ }
  camerasStart({ on: false });                      // the cameras are already on: nothing to send
}

// Leaves the channel and forgets everything about the stream. Sends nothing to the robot.
function camerasStop(message) {
  cams.open = false;
  camerasLeave();
  camerasSet("closed", message || "");
}

function camerasLeave() {
  cams.ticket++;
  clearTimeout(cams.slowTimer);
  const client = cams.client;
  cams.client = null;
  camerasForgetSdkStorage();
  if (client) {
    try { client.removeAllListeners(); } catch { /* already gone */ }
    // Remembered, so a start that follows waits for this leave to finish before it makes another client.
    const left = Promise.resolve().then(() => client.leave()).catch(() => { /* already left */ })
      .then(camerasForgetSdkStorage);               // the SDK may write as it leaves
    cams.leaving = Promise.all([cams.leaving, left]).then(() => {});
  }
  if (cams.stage) cams.stage.replaceChildren();
}

// The SDK keeps a device id and caches of its remote settings in localStorage, and reads the cached settings when it
// loads. Clear them before it loads (a stale cache could pin a setting on) and after (it writes its device id on load).
function camerasForgetSdkStorage() {
  try {
    const names = Object.keys(localStorage);
    for (const name of names) if (name.startsWith(CAM_SDK_STORAGE)) localStorage.removeItem(name);
  } catch { /* no storage */ }
}

function camerasLoadSdk() {
  if (window.AgoraRTC) return Promise.resolve(window.AgoraRTC);
  cams.sdk ||= new Promise((resolve, reject) => {
    camerasForgetSdkStorage();
    const el = h("script", { src: CAM_SDK_URL });
    const timer = setTimeout(() => {
      try { el.remove(); } catch { /* not attached */ }
      reject(new Error("The video library took too long to load."));
    }, CAM_SDK_TIMEOUT_MS);
    el.onload = () => {
      clearTimeout(timer);
      camerasForgetSdkStorage();
      if (window.AgoraRTC) resolve(window.AgoraRTC); else reject(new Error("The video library did not load."));
    };
    el.onerror = () => { clearTimeout(timer); reject(new Error("The video library is missing (web/vendor/).")); };
    document.head.append(el);
  }).catch((e) => { cams.sdk = null; throw e; });
  return cams.sdk;
}

// Before any client exists: nothing to Agora that the panel does not need (see CAM_SDK_OFF), a device id made up for
// this start instead of the one the SDK would keep in localStorage, and a quiet console.
function camerasQuietSdk(AgoraRTC) {
  for (const [key, value] of CAM_SDK_OFF) AgoraRTC.setParameter(key, value, true);
  const id = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `${Math.random()}`.slice(2);
  AgoraRTC.setParameter("INSTALL_ID", id, true);
  AgoraRTC.setLogLevel(4);
}

const camerasBytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

// A short, safe reason for the screen, with the step that failed: our own errors say what they mean; an SDK error is
// shown by its code only; an error with no message (a DOMException from the browser's crypto, say) by its name.
// Never the token, key, salt, channel or App ID, and never more than CAM_WHY_MAX characters.
const CAM_WHY_MAX = 100;
function camerasWhy(e, step) {
  let reason = "";
  if (e && e.code && !e.status) reason = String(e.code);
  else if (e && e.message) reason = String(e.message);
  else if (e && e.name) reason = String(e.name);
  const text = reason ? `${step} failed: ${reason}` : `${step} failed.`;
  return text.length > CAM_WHY_MAX ? `${text.slice(0, CAM_WHY_MAX - 1)}…` : text;
}

async function camerasStart({ on }) {
  camerasLeave();
  const ticket = cams.ticket;
  const live = () => ticket === cams.ticket && cams.open;
  const camera = cams.camera;
  camerasSet("connecting", "Connecting…");
  let hint = "";
  let step = "Getting a token";                   // what failed, for the error; a label only, no join details
  try {
    await cams.leaving;                           // never two live clients: wait out any leave still in flight
    if (!live()) return;
    if (on) hint = await camerasSwitchOn();
    if (!live()) return;
    const join = await postJSON("/api/video/token", { camera });
    if (!live()) return;
    step = "Loading the video library";
    const AgoraRTC = await camerasLoadSdk();      // only after the panel has given a token
    if (!live()) return;
    if (AgoraRTC.checkSystemRequirements && !AgoraRTC.checkSystemRequirements()) throw new Error("This browser cannot play the robot's video.");
    step = "Setting up";
    camerasQuietSdk(AgoraRTC);
    const client = AgoraRTC.createClient({ mode: "live", codec: "h264" });
    cams.client = client;
    await client.setClientRole("audience");
    if (!live()) return;
    client.setEncryptionConfig("aes-128-gcm2", join.key, camerasBytes(join.salt));
    camerasListen(client, ticket);
    step = "Joining";
    await client.join(join.app_id, join.channel, join.token, join.uid);
    if (!live()) return;
    // The robot may have been publishing already: user-published can have started the video before join returned.
    if (cams.phase !== "playing") {
      camerasSet("waiting", "Waiting for the robot's video…", hint || "The cameras may be off, or the robot asleep.");
      cams.slowTimer = setTimeout(() => {
        if (live() && cams.phase === "waiting") {
          camerasSet("waiting", "No video from the robot yet.", "It may be asleep, or its cameras are off: open the Yarbo app, or switch Control on to wake them.");
        }
      }, CAM_SLOW_MS);
    }
    // Already-present users: subscribe only to the robot, and only if it is publishing video. Otherwise stay in
    // Waiting; user-published starts the playback.
    step = "Playing";
    for (const u of client.remoteUsers || []) {
      if (Number(u.uid) === CAM_ROBOT_UID && u.hasVideo) await camerasPlay(client, u, "video", ticket);
    }
  } catch (e) {
    if (!live()) return;
    camerasLeave();                               // the tile stays open, in its error state
    camerasSet("error", camerasWhy(e, step), hint);
  }
}

// Switches the robot's cameras on through the controller (people only, Control on). Returns a hint for the screen:
// nothing when it was sent, why not when it could not be. A refusal does not stop the video: the cameras may be on
// already (the app, a run), and joining a channel nobody publishes to costs nothing.
async function camerasSwitchOn() {
  // The latest status, not the value the last card render saw: Control may have just been switched on in the top bar.
  const controlOn = state.status && state.status.ops ? !!state.status.ops.control_enabled : cams.controlOn;
  if (!controlOn) return "Control is off, so the panel cannot switch the cameras on. They play if the robot already has them on.";
  try {
    const r = await postJSON("/api/control/send", { cmd: "smart_vision_control", args: {}, confirm: true });
    if (r && r.sent) return "";
    return `The cameras were not switched on: ${(r && (r.refused || r.error)) || "refused"}.`;
  } catch (e) {
    return `The cameras were not switched on: ${e.message}.`;
  }
}

function camerasListen(client, ticket) {
  const live = () => ticket === cams.ticket && cams.open && cams.client === client;
  client.on("user-published", async (user, mediaType) => {
    if (!live()) return;
    try {
      await camerasPlay(client, user, mediaType, ticket);
    } catch (e) {
      if (!live()) return;
      camerasLeave();                             // the tile stays open, in its error state
      camerasSet("error", camerasWhy(e, "Playing"));
    }
  });
  client.on("user-unpublished", (user, mediaType) => {
    if (live() && Number(user.uid) === CAM_ROBOT_UID && mediaType === "video") {   // not the robot's audio stopping
      cams.subscribed = 0;                        // a later user-published subscribes again
      if (cams.stage) cams.stage.replaceChildren();
      camerasSet("waiting", "The robot stopped sending video.", "Its cameras may have switched off, or it went to sleep.");
    }
  });
  // A new token needs a new viewer uid, which only a new request gives, so renewing is a quick rejoin.
  const renew = () => { if (live()) camerasStart({ on: false }); };
  client.on("token-privilege-will-expire", renew);
  client.on("token-privilege-did-expire", renew);
  client.on("crypt-error", () => { if (live()) { camerasLeave(); camerasSet("error", "Could not decrypt the stream."); } });
  client.on("connection-state-change", (cur, _prev, reason) => {
    if (live() && cur === "DISCONNECTED" && reason !== "LEAVE") {
      camerasLeave();
      camerasSet("error", "The connection was lost.");
    }
  });
}

async function camerasPlay(client, user, mediaType, ticket) {
  if (Number(user.uid) !== CAM_ROBOT_UID || mediaType !== "video") return;
  if (cams.subscribed === ticket) return;         // user-published and the remoteUsers loop can both get here
  cams.subscribed = ticket;
  await client.subscribe(user, "video");
  if (ticket !== cams.ticket || !cams.open || !user.videoTrack) return;
  clearTimeout(cams.slowTimer);
  user.videoTrack.play(cams.stage, { fit: "contain" });
  camerasSet("playing", "");
}

// Video only while it can be seen: leave when the tab is hidden, when Live is left and when the page closes.
document.addEventListener("visibilitychange", () => { if (document.hidden && cams.open) camerasStop("Stopped while the tab was hidden."); });
window.addEventListener("pagehide", () => { if (cams.open) camerasStop(); });
// After the shell's own hashchange handler has set state.view (a timer, not a microtask: which listener runs first is not fixed).
window.addEventListener("hashchange", () => setTimeout(() => { if (cams.open && state.view !== "live") camerasStop(); }, 0));
