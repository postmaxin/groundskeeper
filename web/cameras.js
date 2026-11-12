/* The Cameras tile on Live: the robot's own cameras (front, left, right, back), one at a time (§13).
   Loaded after ops.js and uses its helpers (h, fill, postJSON, getJSON, state, dot, toast).

   What opening it does, in this order: ask the controller to switch the cameras on (smart_vision_control, which needs
   Control on), ask the panel for the camera's join details (POST /api/video/token), load Agora's Web SDK from this
   panel (web/vendor/, never a CDN), then join the robot's channel as a host that publishes nothing and play uid
   1's video. Host, not audience: the robot publishes only while another broadcaster is in its channel (an audience
   member is invisible to it), and it stops when the last one leaves (docs/dev/protocol.md, Robot video).
   The video goes from Agora's network to this browser; none of it passes through the panel.

   Rules this file keeps (tests/test_cameras_js.py checks the ones a test can):
   - It never switches the cameras off: state 0 would also cut the official app's view (docs/dev/protocol.md).
   - While the tile is open (connecting, waiting, playing) it asks the panel for one keep-awake beat every 4 s
     (POST /api/video/keepalive). The robot streams only while someone keeps it awake, as the official app does. The
     timer belongs to the start's ticket and camerasLeave() clears it, so every way of stopping stops the beats; the
     panel itself never repeats a beat, so a closed tab stops them too. A press of play opens a window of at most
     15 min on the panel; when the panel ends it, the tile closes.
   - Video runs only while the tile is open and the tab is showing Live. Closing it, changing camera, leaving Live,
     hiding the tab and closing the page all leave the channel.
   - The token, key and salt live in local variables of one function call. They are not kept on `cams`, in the DOM, in
     storage or the URL, and nothing here writes to the console. The stream's secret is longer than Agora's Web SDK
     can wrap (see camerasSwapInstall), so for the client's lifetime it is written into the SDK's join messages to
     Agora in its place. (The SDK keeps a few `websdk_ng_*` items of its own in this panel's localStorage: a device
     id, a cache of its settings and of a preload. This file clears them when the SDK has loaded and when the channel
     is left; none of them holds the token, key or salt.)
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
const CAM_STATS_MS = 2000;                         // how often the stats line under the video is refreshed
const CAM_BEAT_MS = 4000;                          // the official app keeps the robot awake about this often while open
const CAM_JOIN_TIMEOUT_MS = 30000;                 // a join that never settles gives up, so the swap cannot linger
// After leaving a channel the tile was watching, wait this long before camera-on and the next join: the robot stops
// its video a moment after the last viewer leaves, and a camera-on sent straight away was lost to that (a camera
// change then waited for video for good; with 3 s it played within seconds).
const CAM_REJOIN_GAP_MS = 3000;
// "Weak link" thresholds, on the SDK's own numbers (4.24.9): packetLossRate and freezeRate are ratios 0..1, and the
// network quality is 0 (unknown) to 6 (down), where 4 is "poor". getRemoteNetworkQuality()[uid] describes that remote
// user's own connection, so for the robot its uplink is the video's source link; its downlink is irrelevant to viewing.
const CAM_WEAK_LOSS = 0.05;                        // 5% of the robot's packets lost
const CAM_WEAK_FREEZE = 0.1;                       // frozen for 10% of the time
const CAM_WEAK_QUALITY = 4;                        // the robot's uplink network quality "poor" or worse
const CAM_SLOW_DRAW = 0.7;                         // drawn frames under 70% of received ones: the browser, not the link
const CAM_CONTROL_OFF_HINT = "Control is off.";                // short on screen; CAM_HELP says the rest
const CAM_ENDED = "Stopped. Open to watch again.";             // the panel's time limit, or the robot went offline
// STOPGAP (AgoraIO/agora-rtc-web#15). The Web SDK wraps the stream's secret with a built-in 1024-bit RSA key, which
// holds at most 62 bytes; the robot's secret is 64, so join() throws OperationError. This placeholder (NOT a secret)
// is what the SDK is given; camerasJoin writes the real secret into the join message in its place. Remove both once
// Agora lifts the limit.
const CAM_PLACEHOLDER_SECRET = "groundskeeper-placeholder";
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
               slowTimer: null, beatTimer: null, statsTimer: null, stats: "", statsWeak: false, statsEl: null,
               controlOff: false, leaving: Promise.resolve(), settings: null,
               settingsAt: 0, card: null, stage: null, sdk: null, subscribed: 0, leftAt: 0 };

// The hints on screen are short; what each one means is its tooltip (and docs/user/live.md).
const CAM_HELP = {
  "Control is off.": "The panel cannot switch the cameras on or keep the robot awake while Control is off. They play " +
                     "if the robot already has them on. Switch Control on, then close and open the tile again.",
  "Asleep, or cameras off.": "The robot may be asleep, or its cameras are off: open the Yarbo app, or switch Control " +
                             "on, then close and open the tile again.",
  "Cameras off, or asleep.": "Its cameras may have switched off, or the robot went to sleep.",
};

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
    try {
      cams.settings = await getJSON("/api/settings");
      cams.settingsAt = Date.now();
    } catch { /* keep the last answer */ }
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
    cams.statsEl = h("p", { class: "small muted camstats", style: "margin:6px 0 0",
      title: "Measured in this browser from the video library's own counters. Nothing is sent anywhere." });
    const demo = CAM_DEMO_STATS;
    camerasStatsShow(cams.demo ? camerasStatsLine(demo.video, demo.rtc, demo.quality) : { text: "", weak: false });
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
  // Open, Close and Try again come and go, so each names where focus goes when it disappears (keepFocus, app.js).
  const after = { "cam-open": "cam-close", "cam-retry": "cam-close", "cam-close": "cam-open" }[
    document.activeElement?.dataset?.key];
  keepFocus(cams.card, () => fill(cams.card,
    h("div", { class: "row gap", style: "justify-content:space-between" },
      h("p", { class: "label", style: "margin:0" }, "Cameras ",
        h("span", { class: "tip", tabindex: "0", "data-key": "cam-tip",
          title: "The robot's own cameras, not the yard cameras. They come " +
          "through Yarbo's cloud and Agora's network, one at a time, and only while this is open." }, "ⓘ")),
      cams.demo ? h("span", { class: "small muted" }, "Demo")
                : h("span", { class: "small", style: `color:${colour}` }, dot(colour), " ", label)),
    cams.demo ? [h("p", { class: "muted small", style: "margin:6px 0 0",
                          title: "The demo robot has no cameras. A real robot shows its video here." },
                   "No cameras in the demo"),
                 cams.statsEl]
    : cams.open ? [
      h("div", { class: "camchips", role: "group", "aria-label": "Robot camera" },
        CAM_CHOICES.map(([k, name]) => h("button", { type: "button", class: `btn slim${cams.camera === k ? " on" : ""}`,
          "data-key": `cam-${k}`,
          "aria-pressed": String(cams.camera === k), title: `The robot's ${name.toLowerCase()} camera`,
          onclick: () => camerasPick(k) }, name)),
        h("button", { type: "button", class: "btn slim", "data-key": "cam-close",
          title: "Stop the video and leave the channel",
          onclick: () => camerasStop() }, "Close")),
      cams.stage,
      cams.statsEl,
      cams.message ? h("p", { class: "small", style: `margin:6px 0 0;color:${colour}`, role: "status" },
                       cams.message) : null,
      cams.hint ? h("p", { class: "muted small", style: "margin:4px 0 0", title: CAM_HELP[cams.hint] || null },
                    cams.hint) : null,
      cams.phase === "error" ? h("button", { type: "button", class: "btn slim", "data-key": "cam-retry",
                                             style: "margin-top:6px",
                                             onclick: () => camerasStart({ on: true }) }, "Try again") : null]
    : [
      h("div", { class: "row gap", style: "margin-top:6px" },
        h("button", { type: "button", class: "btn slim", "data-key": "cam-open", onclick: () => camerasOpen(),
          title: "Switch the robot's cameras on and show one here. Needs Control on." }, "Open")),
      cams.message ? h("p", { class: "muted small", style: "margin:6px 0 0", role: "status" }, cams.message) : null]),
    after);
}

// Made-up numbers for the demo, which never loads the SDK: it shows what the line looks like on a real robot.
const CAM_DEMO_STATS = { video: { receiveResolutionWidth: 640, receiveResolutionHeight: 480, renderFrameRate: 15,
                                  receiveFrameRate: 15, receiveBitrate: 1234000, packetLossRate: 0.02, freezeRate: 0,
                                  totalFreezeTime: 0, end2EndDelay: 0 },
                         rtc: { RTT: 180 }, quality: { uplinkNetworkQuality: 2 } };

// The line under the video, from the SDK's local counters (read by camerasStatsStart). Returns { text, weak }; text
// is "" when there are no numbers yet. Only numbers are used: no ids, names, tokens or addresses can get in.
function camerasStatsLine(video, rtc, quality) {
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  const v = video || {};
  const w = num(v.receiveResolutionWidth), ht = num(v.receiveResolutionHeight);
  const bps = num(v.receiveBitrate), loss = num(v.packetLossRate);
  const freezeRate = num(v.freezeRate), froze = num(v.totalFreezeTime);
  const received = num(v.receiveFrameRate);
  // A real 0 stays 0: a stalled renderer is not masked by the receive rate. Only a missing counter falls back.
  const drawn = typeof v.renderFrameRate === "number" && Number.isFinite(v.renderFrameRate)
    ? v.renderFrameRate : received;
  const rtt = num(rtc && rtc.RTT), delay = num(v.end2EndDelay);
  if (!w && !bps && !drawn) return { text: "", weak: false };      // nothing received yet
  const weak = loss >= CAM_WEAK_LOSS || freezeRate >= CAM_WEAK_FREEZE
    || num(quality && quality.uplinkNetworkQuality) >= CAM_WEAK_QUALITY;
  const slow = !weak && received >= 5 && drawn < received * CAM_SLOW_DRAW;
  const pct = loss * 100;
  const parts = [
    w && ht ? `${Math.round(w)}×${Math.round(ht)}` : "",
    `${Math.round(drawn)} fps`,
    bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} Mbps` : `${Math.round(bps / 1e3)} kbps`,
    pct > 0 && pct < 1 ? "<1% loss" : `${Math.round(pct)}% loss`,
    rtt > 0 ? `RTT ${Math.round(rtt)} ms` : "",
    delay > 0 ? `delay ${Math.round(delay)} ms` : "",
    froze >= 1 ? `froze ${Math.round(froze)} s` : "",
    // weak link: the robot's side or the network; slow to draw: this browser
    weak ? "weak link" : slow ? "slow to draw" : ""];
  return { text: parts.filter(Boolean).join(" · "), weak };
}

// Puts the line on the tile. It updates the one paragraph in place (no redraw), so the playing video is not touched.
function camerasStatsShow({ text, weak }) {
  cams.stats = text;
  cams.statsWeak = weak;
  if (!cams.statsEl) return;
  cams.statsEl.textContent = text;
  cams.statsEl.hidden = !text;
  cams.statsEl.className = `small muted camstats${weak ? " weak" : ""}`;
}

const camerasStatsClear = () => camerasStatsShow({ text: "", weak: false });

// Reads the SDK's local counters for the robot, every CAM_STATS_MS while its video plays. Nothing is sent anywhere. A
// read that throws shows nothing and logs nothing.
function camerasStatsStart(client, ticket) {
  clearInterval(cams.statsTimer);
  const read = () => {
    if (ticket !== cams.ticket || !cams.open || cams.client !== client) return;
    try {
      const video = (client.getRemoteVideoStats() || {})[CAM_ROBOT_UID];
      const quality = (client.getRemoteNetworkQuality() || {})[CAM_ROBOT_UID];
      camerasStatsShow(camerasStatsLine(video, client.getRTCStats(), quality));
    } catch { camerasStatsClear(); }
  };
  cams.statsTimer = setInterval(read, CAM_STATS_MS);
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
  camerasStart({ on: true });                       // leaving stopped the robot's video: switch it on again
}

// Leaves the channel and forgets everything about the stream. Sends nothing to the robot.
function camerasStop(message) {
  cams.open = false;
  if (cams.client) cams.leftAt = Date.now();        // the robot may still be shutting its video down: an Open soon
                                                    // after waits out the rejoin gap too
  camerasLeave();
  camerasSet("closed", message || "");
}

function camerasLeave() {
  cams.ticket++;
  clearTimeout(cams.slowTimer);
  clearInterval(cams.beatTimer);                    // no more keep-awake beats, on every way of stopping
  cams.beatTimer = null;
  clearInterval(cams.statsTimer);                   // and no more stats reads
  cams.statsTimer = null;
  camerasStatsClear();
  cams.controlOff = false;
  if (camSwapRemove) camSwapRemove();               // the secret swap lives as long as the client
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

// One keep-awake beat now and one every CAM_BEAT_MS while this start is live. The panel sends exactly one command per
// accepted request and never repeats, so what stops this stops the beats. `fresh`: a person pressed Open, a camera or
// Try again, so the first beat asks the panel for a new window (it keeps the robot awake at most 15 min per press; a
// token renewal is not a press). When the panel ends the window (time up, robot offline) the tile closes and says so.
// A beat that fails otherwise shows nothing (it must not break playback); only "Control is off" is said, as a hint.
function camerasBeats(ticket, fresh) {
  const live = () => ticket === cams.ticket && cams.open;
  let first = fresh;
  let inFlight = false;
  const beat = async () => {
    if (!live()) return;
    if (inFlight) return;                         // one at a time: a slow panel is not asked again on top
    inFlight = true;
    const body = first ? { start: true } : {};
    try {
      const r = await postJSON("/api/video/keepalive", body);
      first = false;                              // answered: the window is open (or the answer says why not)
      if (!live()) return;
      if (r && r.ended) {
        camerasStop(CAM_ENDED);
        return;
      }
      cams.controlOff = !!(r && r.control_off);       // the panel's word on it, which may be newer than the last status
      if (cams.controlOff && cams.phase !== "error" && cams.hint !== CAM_CONTROL_OFF_HINT) {
        cams.hint = CAM_CONTROL_OFF_HINT;
        camerasDraw();
      } else if (!cams.controlOff && cams.hint === CAM_CONTROL_OFF_HINT) {
        cams.hint = "";                               // Control came back on: only this hint goes, not an unrelated one
        camerasDraw();
      }
    } catch { /* quiet: the next beat tries again */ } finally { inFlight = false; }
  };
  clearInterval(cams.beatTimer);
  cams.beatTimer = setInterval(beat, CAM_BEAT_MS);
  beat();
}

// Swaps the real secret into one JSON message in place of the SDK's wrapped placeholder, and drops the field that says
// it was wrapped. Works on the text, not a parsed copy, so nothing else in the message is touched or re-rounded.
// Returns the message and whether a secret field was found.
function camerasSwapBody(body, secret) {
  if (typeof body !== "string" || !/"(?:aes_secret|aesSecret)"/.test(body)) return { body, swapped: false };
  let out = body.replace(/,\s*"(?:aes_encrypt|aesEncrypt)"\s*:\s*(?:true|false)/g, "")
    .replace(/"(?:aes_encrypt|aesEncrypt)"\s*:\s*(?:true|false)\s*,?/g, "");
  let n = 0;
  out = out.replace(/"(aes_secret|aesSecret)"\s*:\s*"(?:[^"\\]|\\.)*"/g, (_m, key) => {
    n++;
    return `"${key}":${JSON.stringify(secret)}`;
  });
  return n ? { body: out, swapped: true } : { body, swapped: false };
}

// STOPGAP (AgoraIO/agora-rtc-web#15). There is no supported way to give the Web SDK this secret: 4.24.9 always wraps
// it with a built-in 1024-bit RSA-OAEP key (at most 62 bytes) before join, with no option to skip that, and the robot's
// secret is 64 bytes, so join() throws. So the SDK is given a placeholder (CAM_PLACEHOLDER_SECRET), wraps that, and the
// real secret is written into its join message in its place, on the SDK's own connections only:
// - Only sockets and requests to Agora's hosts (CAM_AGORA_HOSTS, over wss/https) are touched. While a client lives,
//   the global WebSocket and XMLHttpRequest are subclasses whose instances for any other host are the browser's
//   own, unchanged; the prototypes are never patched, so the panel's own requests never pass through this.
// - In those, only a JSON message with the secret field is rewritten (camerasSwapBody), and nothing else in it.
// - It lives as long as the client, not only for join(): when the SDK's signalling reconnects it sends join_v3 again,
//   built from the same stored placeholder, and asks the access point again. camerasLeave() removes it, and installing
//   a new one removes any old one first, so a left client's secret never goes into another start's messages.
// The connection is TLS to Agora's servers, which decrypt the stream for the viewer anyway. The secret lives only in
// this closure. A join whose WebSocket frames had no secret field fails visibly: the library has changed.
const CAM_AGORA_HOSTS = [".agora.io", ".sd-rtn.com"];
function camerasAgoraUrl(url) {
  try {
    const u = new URL(String(url), location.href);
    return (u.protocol === "wss:" || u.protocol === "https:") && CAM_AGORA_HOSTS.some((d) => u.hostname.endsWith(d));
  } catch {
    return false;
  }
}

// Plain http (not a secure context): the browser has no crypto.subtle, and the SDK's join wraps the placeholder secret
// with RSA-OAEP before it sends anything, so join() threw "Cannot read properties of undefined (reading 'importKey')".
// The wrapped placeholder never matters: camerasSwapBody writes the real secret in its place. So while the swap is in,
// and only where the browser has no Web Crypto of its own, a stand-in answers exactly that wrap (an RSA-OAEP public
// key, then encrypt with it) with filler bytes and refuses every other call. It goes with the swap (camerasLeave).
const CAM_RSA_BYTES = 128;                          // what a 1024-bit RSA-OAEP key gives, as the SDK's built-in one is
const CAM_SUBTLE_CALLS = ["decrypt", "deriveBits", "deriveKey", "digest", "exportKey", "generateKey", "sign",
  "unwrapKey", "verify", "wrapKey"];
function camerasStandInCrypto() {
  const c = globalThis.crypto;
  if (!c || c.subtle) return () => {};              // the browser's own (https, localhost): never replaced
  const refuse = () => Promise.reject(new Error("This needs the panel over https."));
  const rsa = (alg) => alg === "RSA-OAEP" || (!!alg && alg.name === "RSA-OAEP");
  const standInKey = Object.freeze({ standIn: true });
  const subtle = {
    importKey: (format, _data, alg) => (format === "spki" && rsa(alg) ? Promise.resolve(standInKey) : refuse()),
    encrypt: (alg, key) => (rsa(alg) && key === standInKey
      ? Promise.resolve(new Uint8Array(CAM_RSA_BYTES).buffer) : refuse()),
  };
  for (const name of CAM_SUBTLE_CALLS) subtle[name] = refuse;
  Object.defineProperty(c, "subtle", { configurable: true, value: subtle });
  return () => { if (c.subtle === subtle) delete c.subtle; };
}

let camSwapRemove = null;
function camerasSwapInstall(secret) {
  if (camSwapRemove) camSwapRemove();
  const WS = globalThis.WebSocket;
  const XHR = globalThis.XMLHttpRequest;
  let active = true;
  let frames = 0;
  // Only a WebSocket frame counts as the join having carried the secret: an access-point request that still has the
  // field must not hide a join frame that no longer does.
  class AgoraSocket extends WS {
    constructor(url, protocols) {
      super(url, protocols);
      if (!camerasAgoraUrl(url)) return;          // anyone else's socket stays the browser's own
      const send = this.send;
      this.send = (data) => {
        if (!active) return send.call(this, data);
        const r = camerasSwapBody(data, secret);
        if (r.swapped) frames++;
        return send.call(this, r.body);
      };
    }
  }
  class AgoraRequest extends XHR {
    #agora = false;
    open(method, url, ...rest) {
      this.#agora = camerasAgoraUrl(url);
      return super.open(method, url, ...rest);
    }
    send(data) {
      return super.send(active && this.#agora ? camerasSwapBody(data, secret).body : data);
    }
  }
  const removeCrypto = camerasStandInCrypto();
  const remove = () => {
    active = false;                                 // instances made meanwhile go back to plain sends
    removeCrypto();
    if (globalThis.WebSocket === AgoraSocket) globalThis.WebSocket = WS;
    if (globalThis.XMLHttpRequest === AgoraRequest) globalThis.XMLHttpRequest = XHR;
    if (camSwapRemove === remove) camSwapRemove = null;
  };
  globalThis.WebSocket = AgoraSocket;
  globalThis.XMLHttpRequest = AgoraRequest;
  camSwapRemove = remove;
  return { frames: () => frames };
}

// Joins run one at a time. A start that is no longer live never reaches the wrapper.
let camJoinTail = Promise.resolve();
function camerasJoin(client, args, secret, live) {
  const run = camJoinTail.then(() => {
    if (!live()) throw new Error("This start was closed.");     // the caller is not live either, and says nothing
    return camerasJoinSwapped(client, args, secret);
  });
  camJoinTail = run.catch(() => {});
  return run;
}

async function camerasJoinSwapped(client, args, secret) {
  const swap = camerasSwapInstall(secret);
  let timer;
  try {
    await Promise.race([client.join(...args),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("The join timed out.")), CAM_JOIN_TIMEOUT_MS);
      })]);
  } catch (e) {
    if (camSwapRemove) camSwapRemove();             // no client to keep it for
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (!swap.frames()) {
    if (camSwapRemove) camSwapRemove();
    throw new Error("video library changed");
  }
}

const camerasBytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

// A short, safe reason for the screen, with the step that failed: our own errors say what they mean; an SDK error is
// shown by its (string) code only, not a DOMException's legacy number; an error with no message (a DOMException from
// the browser's crypto, say) by its name.
// Never the token, key, salt, channel or App ID, and never more than CAM_WHY_MAX characters.
const CAM_WHY_MAX = 100;
function camerasWhy(e, step) {
  let reason = "";
  if (e && typeof e.code === "string" && e.code && !e.status) reason = String(e.code);
  else if (e && e.message) reason = String(e.message);
  else if (e && e.name) reason = String(e.name);
  const text = reason ? `${step} failed: ${reason}` : `${step} failed.`;
  return text.length > CAM_WHY_MAX ? `${text.slice(0, CAM_WHY_MAX - 1)}…` : text;
}

async function camerasStart({ on, fresh = true }) {
  if (cams.client) cams.leftAt = Date.now();      // leaving a channel to join another: the rejoin gap runs from here
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
    // Counted from the last leave, not this start: a second camera change within the gap waits out the rest of it.
    const gap = cams.leftAt ? CAM_REJOIN_GAP_MS - (Date.now() - cams.leftAt) : 0;
    if (gap > 0) {
      await new Promise((r) => { setTimeout(r, gap); });
      if (!live()) return;
    }
    camerasBeats(ticket, fresh);                  // keep the robot awake from now on: it streams only while kept so
    if (on) hint = await camerasSwitchOn();
    if (!live()) return;
    const join = await postJSON("/api/video/token", { camera });
    if (!live()) return;
    step = "Loading the video library";
    const AgoraRTC = await camerasLoadSdk();      // only after the panel has given a token
    if (!live()) return;
    // false: skip the camera and microphone check. The tile only watches, and a browser hides navigator.mediaDevices
    // on a page that is not a secure context (the panel over plain http on the LAN), which fails the full check.
    if (AgoraRTC.checkSystemRequirements && !AgoraRTC.checkSystemRequirements(false)) {
      throw new Error("This browser cannot play the robot's video.");
    }
    step = "Setting up";
    camerasQuietSdk(AgoraRTC);
    const client = AgoraRTC.createClient({ mode: "live", codec: "h264" });
    cams.client = client;
    // Host, so the robot sees a viewer and publishes. Nothing is ever published from here: the tile makes no
    // microphone or camera track and never calls publish (tests/test_cameras_js.py checks).
    await client.setClientRole("host");
    if (!live()) return;
    // The placeholder, not the key: camerasJoin puts the real one in (see camerasSwapInstall).
    client.setEncryptionConfig("aes-128-gcm2", CAM_PLACEHOLDER_SECRET, camerasBytes(join.salt));
    camerasListen(client, ticket);
    step = "Joining";
    await camerasJoin(client, [join.app_id, join.channel, join.token, join.uid], join.key, live);
    if (!live()) return;
    // The robot may have been publishing already: user-published can have started the video before join returned.
    if (cams.phase !== "playing") {
      camerasSet("waiting", "Waiting for the robot's video…",
        hint || (cams.controlOff ? CAM_CONTROL_OFF_HINT : "The cameras may be off, or the robot asleep."));
      cams.slowTimer = setTimeout(() => {
        if (live() && cams.phase === "waiting") {
          camerasSet("waiting", "No video from the robot yet.", "Asleep, or cameras off.");
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
  if (!controlOn) return CAM_CONTROL_OFF_HINT;
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
      clearInterval(cams.statsTimer);
      cams.statsTimer = null;
      camerasStatsClear();
      if (cams.stage) cams.stage.replaceChildren();
      camerasSet("waiting", "The robot stopped sending video.", "Cameras off, or asleep.");
    }
  });
  // A new token needs a new viewer uid, which only a new request gives, so renewing is a quick rejoin. Leaving stops
  // the robot's video, so the rejoin switches the cameras on again; it is not a press, so no new keep-awake window.
  const renew = () => { if (live()) camerasStart({ on: true, fresh: false }); };  client.on("token-privilege-will-expire", renew);
  client.on("token-privilege-did-expire", renew);
  client.on("crypt-error", () => {
    if (live()) { camerasLeave(); camerasSet("error", "Could not decrypt the stream."); }
  });
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
  camerasStatsStart(client, ticket);
  camerasSet("playing", "");
}

// Video only while it can be seen: leave when the tab is hidden, when Live is left and when the page closes.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && cams.open) camerasStop("Stopped while the tab was hidden.");
});
window.addEventListener("pagehide", () => { if (cams.open) camerasStop(); });
// After the shell's own hashchange handler has set state.view (a timer, not a microtask: which listener runs first
// is not fixed).
window.addEventListener("hashchange", () => {
  setTimeout(() => { if (cams.open && state.view !== "live") camerasStop(); }, 0);
});
