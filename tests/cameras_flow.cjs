// Drives web/cameras.js against a stubbed DOM, panel and Agora SDK, and prints the calls it made as JSON.
// Run by tests/test_cameras_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

const calls = [];
const log = (...a) => calls.push(a);
const node = () => ({ replaceChildren() {}, append() {}, remove() { log("scriptRemoved"); } });
const listeners = {};
const ROBOT = { uid: 1, hasVideo: true, videoTrack: { play: (...a) => log("play", a[1]) } };
const SDK_USER = ROBOT;
let client;
const KEY_PREFIX = "SYNTHETICSECRET";                // the stream secret the stub panel hands out (64 characters, as the robot's is)
let tokens = 0;
const intervals = new Map();                         // the keep-awake timer: fired by the test, never by the clock
let nextInterval = 1;
let beats = 0;
const wsSendOriginal = function (d) { log("ws-send", typeof d === "string" ? d : "<binary>"); };
const xhrSendOriginal = function (d) { log("xhr-send", typeof d === "string" ? d : "<binary>"); };
const WRAPPED = '"aes_secret":"d3JhcHBlZC1iYXNlNjQ=","aes_encrypt":true';     // what the real SDK puts in the join message
const joinMessage = (inner) => `{"_id":7,"_type":"join","_message":{"aes_mode":"aes-128-gcm2",${inner},"aes_salt":"AQID","ts":1731234567890123456}}`;
const knobs = { beatFail: false, beatReply: { sent: true }, beatGate: null, joinMsg: "default", leaveGate: null, roleGate: null, joinGate: null, joinEmit: false, refuseToken: false, remote: [],
                subscribeFail: false, joinFail: null, tokenFail: null, tokenGate: null, writeOnLeave: false, sdkPresent: true, fastSdk: false, videoOff: false, scripts: [] };
const storage = {};
const AgoraRTC = {
  checkSystemRequirements: () => true,
  // The device id is random, so only its being set (and non-empty) is logged.
  setParameter: (k, v, force) => log("setParameter", k, k === "INSTALL_ID" ? String(v).length > 0 : v, !!force),
  setLogLevel: (n) => log("setLogLevel", n),
  enableLogUpload: () => log("enableLogUpload"),
  createClient: (cfg) => {
    log("createClient", cfg.mode);
    const handlers = {};
    client = {
      remoteUsers: knobs.remote,
      on: (ev, fn) => { handlers[ev] = fn; },
      removeAllListeners: () => log("removeAllListeners"),
      setClientRole: async (r) => { log("setClientRole", r); if (knobs.roleGate) await knobs.roleGate; },
      setEncryptionConfig: (mode, key, salt) => log("setEncryptionConfig", mode, key, Array.from(salt).join(",")),
      join: async (app, channel, token, uid) => {
        log("join", app, channel, token, uid);
        if (knobs.joinMsg !== "none") {              // what the real SDK sends while it joins
          const ws = new sandbox.WebSocket(), xhr = new sandbox.XMLHttpRequest();
          ws.send("ping"); ws.send(new Uint8Array(2)); ws.send('{"x":1}');
          ws.send(knobs.joinMsg === "default" ? joinMessage(WRAPPED) : knobs.joinMsg === "camel" ? joinMessage('"aesSecret":"d3JhcHBlZA==","aesEncrypt":true') : joinMessage('"y":2'));
          xhr.send(knobs.joinMsg === "default" ? `{"aesMode":"aes-128-gcm2","aesSecret":"d3JhcHBlZA==","aesEncrypt":true,"uid":5}` : "{}");
        }
        if (knobs.joinFail) throw knobs.joinFail;
        if (knobs.joinEmit) await handlers["user-published"](ROBOT, "video");   // the robot was already publishing
        if (knobs.joinGate) await knobs.joinGate;
      },
      subscribe: async (u, t) => { if (knobs.subscribeFail) throw { code: "SUBSCRIBE_FAILED", message: "token=007tok" }; log("subscribe", u.uid, t); },
      leave: async () => { log("leave"); if (knobs.leaveGate) await knobs.leaveGate; if (knobs.writeOnLeave) storage["websdk_ng_cache_parameter"] = "written-as-it-leaves"; },
      handlers,
    };
    return client;
  },
};
const sandbox = {
  window: { get AgoraRTC() { log("sdkRead"); return knobs.sdkPresent ? AgoraRTC : undefined; }, addEventListener: (e, f) => { listeners[e] = f; } },
  document: { hidden: false, addEventListener: (e, f) => { listeners[e] = f; }, head: { append: (el) => knobs.scripts.push(el) } },
  // Like a real Storage: its own keys are the stored items, and the methods are not among them.
  localStorage: new Proxy(storage, { get: (t, p) => ({
    getItem: (k) => (k in t ? t[k] : null),
    setItem: (k, v) => { t[k] = String(v); },
    removeItem: (k) => { delete t[k]; },
  })[p] ?? t[p] }),
  h: () => node(), fill: () => {}, dot: () => node(),
  state: { view: "live", status: {} }, AMBER: "a", GREEN: "g", CORAL: "c", MUTED: "m",
  getJSON: async () => ({ robot_video_enabled: !knobs.videoOff, robot_video_configured: true }),
  setInterval: (fn, ms) => { const id = nextInterval++; intervals.set(id, fn); log("interval", ms); return id; },
  clearInterval: (id) => { if (intervals.delete(id)) log("clear-interval"); },
  WebSocket: Object.assign(function WebSocket() {}, { prototype: { send: wsSendOriginal } }),
  XMLHttpRequest: Object.assign(function XMLHttpRequest() {}, { prototype: { send: xhrSendOriginal } }),
  postJSON: async (url, body) => {
    if (url === "/api/video/keepalive") {
      beats++; log("beat", JSON.stringify(body));
      if (knobs.beatGate) await knobs.beatGate;
      if (knobs.beatFail) throw new Error("beat refused");
      return knobs.beatReply;
    }
    log("post", url, JSON.stringify(body));
    if (url === "/api/video/token") {
      if (knobs.tokenGate) await knobs.tokenGate;
      if (knobs.tokenFail) throw knobs.tokenFail;
      if (knobs.refuseToken) throw new Error("The panel refused.");
      tokens++;
      return { app_id: "0".repeat(32), channel: "YB0000DEMO0001_middle_image", token: "007tok", uid: 888881,
               key: KEY_PREFIX + String(tokens).padStart(64 - KEY_PREFIX.length, "0"), salt: "AQID" };
    }
    return { sent: true };
  },
  // The script-load timeout (20 s) is shortened when a test wants to see it fire; no other timer is touched.
  setTimeout: (fn, ms) => { const t = setTimeout(fn, (knobs.fastSdk && ms === 20000) || (knobs.fastJoin && ms === 30000) ? 5 : ms); if (ms === 30000) t.unref(); return t; },
  Promise, Uint8Array, atob, clearTimeout, Number, String, Date, Array, Object, JSON, Math,
};
vm.createContext(sandbox);
const tail = "\nthis.cams = cams; this.camerasCard = camerasCard; this.camerasOpen = camerasOpen;" +
  " this.camerasPick = camerasPick; this.camerasStop = camerasStop; this.camerasStart = camerasStart;";
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + tail, sandbox);

const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 20; i++) await tick(); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const mark = (name) => { calls.push(["--", name]); };
  const phase = () => calls.push(["phase", sandbox.cams.phase, sandbox.cams.message]);
  const control = (on) => { sandbox.state.status = on ? { ops: { control_enabled: true } } : {}; };
  // Control on: opening switches the cameras on, then fetches the token, then joins.
  await sandbox.camerasCard({ ops: { control_enabled: true } });
  mark("open"); sandbox.camerasOpen(); await settle();
  await client.handlers["user-published"](SDK_USER, "video"); await tick();
  mark("pick-left"); sandbox.camerasPick("left"); await settle();
  mark("hide"); sandbox.document.hidden = true; listeners.visibilitychange(); await tick();
  mark("closed-state"); calls.push(["open", sandbox.cams.open, sandbox.cams.phase]);
  // Control off: no command goes out, the tile says so.
  sandbox.document.hidden = false;
  await sandbox.camerasCard({ ops: { control_enabled: false } });
  mark("open-control-off"); sandbox.camerasOpen(); await settle();
  calls.push(["hint", sandbox.cams.hint]);
  mark("leave-live"); sandbox.state.view = "runs"; listeners.hashchange();
  await new Promise((r) => setTimeout(r, 10)); calls.push(["open", sandbox.cams.open, sandbox.cams.phase]);
  sandbox.state.view = "live";
  mark("close"); sandbox.camerasStop(); await tick();
  // 1. A slow leave plus a fast token (the token-expiry rejoin) never gives two live clients.
  await sandbox.camerasCard({ ops: { control_enabled: false } });
  sandbox.camerasOpen(); await settle();
  let release; knobs.leaveGate = new Promise((r) => { release = r; });
  mark("slow-leave"); client.handlers["token-privilege-will-expire"](); await settle();
  mark("slow-leave-released"); knobs.leaveGate = null; release(); await settle();
  sandbox.camerasStop(); await settle();
  // 2. A refused token never loads the SDK.
  knobs.refuseToken = true;
  mark("refused-token"); sandbox.camerasOpen(); await settle(); phase();
  knobs.refuseToken = false; sandbox.camerasStop(); await settle();
  // 2b. Why a start failed, by step: a join that rejects with an empty-message DOMException, an SDK error with a code
  // (its message carries a secret and must not show), and the server's own phrase for a 502.
  knobs.joinFail = { name: "OperationError", message: "" };
  mark("join-operation-error"); sandbox.camerasOpen(); await settle(); phase();
  knobs.joinFail = { code: "INVALID_OPERATION", message: "token=007tok key=k" };
  mark("join-sdk-code"); sandbox.camerasOpen(); await settle(); phase();
  knobs.joinFail = { name: "AbortError", code: 20, message: "" };      // a DOMException also has a legacy numeric code
  mark("join-abort-error"); sandbox.camerasOpen(); await settle(); phase();
  knobs.joinFail = { name: "Error", message: "x".repeat(300) };
  mark("join-long"); sandbox.camerasOpen(); await settle(); phase();
  knobs.joinFail = null; sandbox.camerasStop(); await settle();
  knobs.tokenFail = new Error("The robot video service did not answer.");
  mark("token-502"); sandbox.camerasOpen(); await settle(); phase();
  knobs.tokenFail = null; sandbox.camerasStop(); await settle();
  // 3. The robot is present but not publishing: stay in Waiting, no subscribe, no leave.
  knobs.remote = [{ uid: 1, hasVideo: false }];
  mark("present-not-publishing"); sandbox.camerasOpen(); await settle(); phase();
  mark("cleanup"); sandbox.camerasStop(); await settle(); knobs.remote = [];
  // 4. A failed subscribe in user-published leaves and shows a sanitized error.
  sandbox.camerasOpen(); await settle();
  knobs.subscribeFail = true;
  mark("publish-fails"); try { await client.handlers["user-published"](SDK_USER, "video"); } catch { /* unhandled in the old code */ } await settle(); phase();
  knobs.subscribeFail = false; sandbox.camerasStop(); await settle();
  // 5. Control switched on after the last card render: the command still goes out (the latest status is read).
  await sandbox.camerasCard({ ops: { control_enabled: false } });
  sandbox.state.status = { ops: { control_enabled: true } };
  mark("control-just-on"); sandbox.camerasOpen(); await settle();
  mark("cleanup2"); sandbox.camerasStop(); await settle(); control(false);
  // 6. Renewing the token is a quiet rejoin: with Control on it must not send camera-on again, on either event.
  control(true);
  sandbox.camerasOpen(); await settle();
  mark("renew-will-expire"); client.handlers["token-privilege-will-expire"](); await settle();
  mark("renew-did-expire"); client.handlers["token-privilege-did-expire"](); await settle();
  mark("renewed"); sandbox.camerasStop(); await settle(); control(false);
  // 7. Only the robot's (uid 1) video is played; another uid, or the robot's audio, is not subscribed to.
  sandbox.camerasOpen(); await settle();
  mark("other-publisher");
  await client.handlers["user-published"]({ uid: 2, videoTrack: { play: () => log("play-other") } }, "video");
  await client.handlers["user-published"](SDK_USER, "audio"); await settle(); phase();
  // 8. The robot stops publishing: back to Waiting (another uid stopping changes nothing), and it can resume.
  mark("robot-plays"); await client.handlers["user-published"](SDK_USER, "video"); await settle();
  mark("unpublished"); phase();
  client.handlers["user-unpublished"]({ uid: 2 }, "video"); phase();
  client.handlers["user-unpublished"]({ uid: 1 }, "audio"); phase();   // audio stopping leaves the video playing
  client.handlers["user-unpublished"]({ uid: 1 }, "video"); phase();
  mark("republished"); await client.handlers["user-published"](SDK_USER, "video"); await settle(); phase();
  // 9. A decryption failure leaves and says so.
  mark("crypt-error"); client.handlers["crypt-error"](); phase();
  await settle(); sandbox.camerasStop(); await settle();
  // 10. Closing the page leaves the channel.
  sandbox.camerasOpen(); await settle();
  mark("pagehide"); listeners.pagehide(); await settle(); calls.push(["open", sandbox.cams.open, sandbox.cams.phase]);
  // 11. Video switched off in Settings while the tile is open: stop and say why.
  await sandbox.camerasCard({ ops: { control_enabled: false } });
  sandbox.camerasOpen(); await settle();
  knobs.videoOff = true; sandbox.cams.settingsAt = 0;
  mark("settings-off"); const card = await sandbox.camerasCard({ ops: { control_enabled: false } });
  calls.push(["card", card === null]); calls.push(["open", sandbox.cams.open, sandbox.cams.phase]); phase();
  knobs.videoOff = false; sandbox.cams.settingsAt = 0; await sandbox.camerasCard({ ops: { control_enabled: false } });
  // 12. Closed while the channel is being joined: nothing carries on (no join, no playing, no Waiting over Closed).
  let relRole; knobs.roleGate = new Promise((r) => { relRole = r; });
  sandbox.camerasOpen(); await settle();
  mark("close-during-role"); sandbox.camerasStop(); knobs.roleGate = null; relRole(); await settle(); phase();
  mark("join-open"); let relJoin; knobs.joinGate = new Promise((r) => { relJoin = r; });
  knobs.remote = [ROBOT];
  sandbox.camerasOpen(); await settle();
  mark("close-during-join"); sandbox.camerasStop(); knobs.joinGate = null; relJoin(); await settle(); phase();
  knobs.remote = [];
  // 12b. Closed while a leave is still finishing, while the token is awaited, and while the script loads.
  sandbox.camerasOpen(); await settle();
  knobs.leaveGate = new Promise((r) => { release = r; });
  sandbox.camerasPick("right"); await settle();                       // waits for the gated leave
  mark("close-during-leave"); sandbox.camerasStop(); knobs.leaveGate = null; release(); await settle(); phase();
  // Reopened (camera-on) while the last leave is still finishing, then closed again: camera-on is never sent.
  mark("reopen-setup"); control(true); sandbox.camerasOpen(); await settle();
  knobs.leaveGate = new Promise((r) => { release = r; });
  sandbox.camerasStop(); await settle();
  mark("reopen-during-leave"); sandbox.camerasOpen(); await settle(); sandbox.camerasStop();
  knobs.leaveGate = null; release(); await settle(); control(false);
  mark("token-open"); let relToken; knobs.tokenGate = new Promise((r) => { relToken = r; });
  sandbox.camerasOpen(); await settle();
  mark("close-during-token"); sandbox.camerasStop(); knobs.tokenGate = null; relToken(); await settle(); phase();
  knobs.sdkPresent = false; knobs.scripts.length = 0; sandbox.cams.sdk = null;
  mark("close-during-sdk-load"); sandbox.camerasOpen(); await settle(); sandbox.camerasStop(); await settle();
  knobs.sdkPresent = true; knobs.scripts[0].onload(); await settle(); phase();
  // 13. The robot is already publishing at join: one subscribe, one play, and the state ends on Live (not Waiting).
  knobs.remote = [ROBOT]; knobs.joinEmit = true;
  mark("already-publishing"); sandbox.camerasOpen(); await settle(); phase();
  knobs.remote = []; knobs.joinEmit = false; sandbox.camerasStop(); await settle();
  // 14. The SDK's own localStorage items are cleared after the script loads and on leaving; the panel's own stay.
  storage["yarbo.robotCamera"] = "front";
  storage["websdk_ng_global_parameter"] = "stale"; storage["websdk_ng_install_id"] = "old-device";
  knobs.sdkPresent = false;
  const keys = () => calls.push(["storage", Object.keys(storage).sort().join(",")]);
  mark("sdk-load"); sandbox.camerasOpen(); await settle(); keys();
  storage["websdk_ng_install_id"] = "new-device"; storage["websdk_ng_cache_parameter"] = "x";   // what the SDK does as it starts
  knobs.sdkPresent = true; knobs.scripts[0].onload(); await settle(); keys();
  storage["websdk_ng_global_parameter"] = "written-while-joined"; knobs.writeOnLeave = true;
  knobs.leaveGate = new Promise((r) => { release = r; });
  sandbox.camerasStop(); await settle(); keys();                      // the leave has not finished: cleared already
  knobs.leaveGate = null; release(); await settle(); keys(); knobs.writeOnLeave = false;
  // 15. A script load that hangs gives up after a while and the next try loads it again.
  knobs.sdkPresent = false; knobs.fastSdk = true; sandbox.cams.sdk = null; knobs.scripts.length = 0;
  mark("sdk-hangs"); sandbox.camerasOpen(); await sleep(80); await settle(); phase();
  calls.push(["sdk-cleared", sandbox.cams.sdk === null]);
  knobs.sdkPresent = true; knobs.fastSdk = false;
  mark("sdk-retry"); sandbox.camerasOpen(); await settle(); phase();
  sandbox.camerasStop(); await settle();
  // 16. A lost connection leaves and says so; a disconnect that is our own leave is not an error.
  sandbox.camerasOpen(); await settle();
  mark("own-leave"); client.handlers["connection-state-change"]("DISCONNECTED", "CONNECTED", "LEAVE"); phase();
  mark("connection-lost"); client.handlers["connection-state-change"]("DISCONNECTED", "CONNECTED", "NETWORK_ERROR"); phase();
  await settle(); sandbox.camerasStop(); await settle();
  // 17. The keep-awake: one beat as soon as the tile opens (before camera-on), then one per timer tick, from the start's
  // ticket; the test fires the timer by hand. Control on, so camera-on is sent too.
  const fire = async () => { for (const fn of [...intervals.values()]) fn(); await settle(); };
  control(true);
  mark("beats-open"); sandbox.camerasOpen(); await settle();
  await fire(); await fire();
  calls.push(["intervals", intervals.size]);
  await client.handlers["user-published"](SDK_USER, "video"); await settle();
  mark("beats-playing"); await fire(); phase();
  mark("beats-pick"); sandbox.camerasPick("left"); await settle(); calls.push(["intervals", intervals.size]);
  mark("beats-pick-done"); sandbox.camerasStop(); await settle(); control(false);
  // 17b. Every way of stopping stops the beats: nothing is sent after it, and no timer is left.
  const stopsBeats = async (name, stop, setup) => {
    if (setup) setup();
    sandbox.camerasOpen(); await settle();
    const before = beats; await stop(); await settle();
    await fire(); await new Promise((r) => setTimeout(r, 15)); await fire();
    calls.push(["stopped", name, beats - before, intervals.size]);
  };
  await stopsBeats("close", () => sandbox.camerasStop());
  await stopsBeats("hidden", () => { sandbox.document.hidden = true; listeners.visibilitychange(); });
  sandbox.document.hidden = false;
  await stopsBeats("pagehide", () => listeners.pagehide());
  await stopsBeats("leave-live", async () => { sandbox.state.view = "runs"; listeners.hashchange(); await sleep(10); });
  sandbox.state.view = "live";
  await stopsBeats("crypt-error", () => client.handlers["crypt-error"]());
  await stopsBeats("connection-lost", () => client.handlers["connection-state-change"]("DISCONNECTED", "CONNECTED", "NETWORK_ERROR"));
  await stopsBeats("settings-off", async () => {
    knobs.videoOff = true; sandbox.cams.settingsAt = 0; await sandbox.camerasCard({ ops: { control_enabled: false } });
  });
  knobs.videoOff = false; sandbox.cams.settingsAt = 0; await sandbox.camerasCard({ ops: { control_enabled: false } });
  await stopsBeats("join-error", () => {}, () => { knobs.joinFail = new Error("boom"); });
  knobs.joinFail = null;
  await stopsBeats("token-error", () => {}, () => { knobs.refuseToken = true; });
  knobs.refuseToken = false;
  // Try again after an error starts the beats again, and Close then stops them.
  knobs.joinFail = new Error("boom"); sandbox.camerasOpen(); await settle(); knobs.joinFail = null;
  mark("try-again"); sandbox.camerasStart({ on: true }); await settle(); calls.push(["intervals", intervals.size]);
  sandbox.camerasStop(); await settle(); calls.push(["intervals", intervals.size]);
  // A closed tile whose last beat is still in flight neither draws a hint nor sends another.
  knobs.beatReply = { sent: false, control_off: true };
  let relBeat; knobs.beatGate = new Promise((r) => { relBeat = r; });
  sandbox.camerasOpen(); await settle(); sandbox.camerasStop(); await settle();
  mark("beat-after-close"); knobs.beatGate = null; relBeat(); await settle(); calls.push(["hint", sandbox.cams.hint]);
  // A failed beat shows nothing and does not stop playback; "Control is off" from the server shows the existing hint.
  knobs.beatReply = { sent: true }; knobs.beatFail = true; control(true);
  mark("beat-fails"); sandbox.camerasOpen(); await settle(); await fire();
  await client.handlers["user-published"](SDK_USER, "video"); await settle(); phase(); calls.push(["hint", sandbox.cams.hint]);
  sandbox.camerasStop(); await settle(); knobs.beatFail = false;
  knobs.beatReply = { sent: false, control_off: true };
  mark("beat-control-off"); sandbox.camerasOpen(); await settle(); calls.push(["hint", sandbox.cams.hint]); phase();
  sandbox.camerasStop(); await settle(); knobs.beatReply = { sent: true }; control(false);
  // 18. The secret swap. The SDK is given the placeholder; the real secret goes into the join message only while the
  // join runs, and the prototypes are the originals again afterwards, however the join ended.
  const restored = () => calls.push(["restored", sandbox.WebSocket.prototype.send === wsSendOriginal, sandbox.XMLHttpRequest.prototype.send === xhrSendOriginal]);
  const afterJoin = () => { new sandbox.WebSocket().send(joinMessage(WRAPPED)); new sandbox.XMLHttpRequest().send("{\"aesSecret\":\"x\"}"); };
  mark("swap"); sandbox.camerasOpen(); await settle(); restored(); afterJoin();
  calls.push(["leak", JSON.stringify(Object.values(storage)).includes(KEY_PREFIX),
    Object.entries(sandbox.cams).filter(([k]) => k !== "client").some(([, v]) => { try { return String(JSON.stringify(v)).includes(KEY_PREFIX); } catch { return false; } }),
    Object.values(sandbox.cams).some((v) => typeof v === "string" && v.includes(KEY_PREFIX))]);
  sandbox.camerasStop(); await settle();
  mark("swap-camel"); knobs.joinMsg = "camel"; sandbox.camerasOpen(); await settle(); restored();
  sandbox.camerasStop(); await settle();
  mark("swap-changed"); knobs.joinMsg = "no-secret-field"; sandbox.camerasOpen(); await settle(); phase(); restored();
  sandbox.camerasStop(); await settle();
  mark("swap-none"); knobs.joinMsg = "none"; sandbox.camerasOpen(); await settle(); phase(); restored();
  sandbox.camerasStop(); await settle(); knobs.joinMsg = "default";
  mark("swap-join-fails"); knobs.joinFail = { name: "NetworkError", message: "" }; sandbox.camerasOpen(); await settle(); phase(); restored();
  knobs.joinFail = null; sandbox.camerasStop(); await settle();
  // A join that never settles gives up after its timeout; the wrapper is gone by then.
  mark("swap-hangs"); knobs.joinGate = new Promise(() => {}); knobs.fastJoin = true;
  sandbox.camerasOpen(); await sleep(80); await settle(); restored(); phase();
  sandbox.camerasStop(); await settle(); knobs.joinGate = null; knobs.fastJoin = false;
  // Overlapping starts: A is held in its join, B (another camera) starts behind it. B's join waits for A's to settle and
  // carries B's own secret; the wrapper is never shared and ends restored.
  tokens = 0;
  let relA; knobs.joinGate = new Promise((r) => { relA = r; });
  mark("swap-overlap"); sandbox.camerasOpen(); await settle();
  sandbox.camerasPick("right"); await settle();
  mark("swap-overlap-held"); calls.push(["intervals", intervals.size]);
  relA(); knobs.joinGate = null; await settle(); restored();
  sandbox.camerasStop(); await settle();
  console.log(JSON.stringify(calls));
})();
