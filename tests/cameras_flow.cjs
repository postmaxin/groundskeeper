// Drives web/cameras.js against a stubbed DOM, panel and Agora SDK, and prints the calls it made as JSON.
// Run by tests/test_cameras_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

const calls = [];
const log = (...a) => calls.push(a);
const node = () => ({ replaceChildren() {}, append() {} });
const listeners = {};
const SDK_USER = { uid: 1, videoTrack: { play: (...a) => log("play", a[1]) } };
let client;
const AgoraRTC = {
  checkSystemRequirements: () => true,
  setParameter: (k, v) => log("setParameter", k, v),
  setLogLevel: (n) => log("setLogLevel", n),
  enableLogUpload: () => log("enableLogUpload"),
  createClient: (cfg) => {
    log("createClient", cfg.mode);
    const handlers = {};
    client = {
      remoteUsers: [],
      on: (ev, fn) => { handlers[ev] = fn; },
      removeAllListeners: () => log("removeAllListeners"),
      setClientRole: async (r) => log("setClientRole", r),
      setEncryptionConfig: (mode, key, salt) => log("setEncryptionConfig", mode, key, Array.from(salt).join(",")),
      join: async (app, channel, token, uid) => log("join", app, channel, token, uid),
      subscribe: async (u, t) => log("subscribe", u.uid, t),
      leave: async () => log("leave"),
      handlers,
    };
    return client;
  },
};
const sandbox = {
  window: { AgoraRTC, addEventListener: (e, f) => { listeners[e] = f; } },
  document: { hidden: false, addEventListener: (e, f) => { listeners[e] = f; }, head: { append() {} } },
  localStorage: { getItem: () => null, setItem: () => {} },
  h: () => node(), fill: () => {}, dot: () => node(),
  state: { view: "live", status: {} }, AMBER: "a", GREEN: "g", CORAL: "c", MUTED: "m",
  getJSON: async () => ({ robot_video_enabled: true, robot_video_configured: true }),
  postJSON: async (url, body) => {
    log("post", url, JSON.stringify(body));
    if (url === "/api/video/token") {
      return { app_id: "0".repeat(32), channel: "YB0000DEMO0001_middle_image", token: "007tok", uid: 888881, key: "k", salt: "AQID" };
    }
    return { sent: true };
  },
  Promise, Uint8Array, atob, setTimeout, clearTimeout, Number, String, Date, Array, Object, JSON,
};
vm.createContext(sandbox);
const tail = "\nthis.cams = cams; this.camerasCard = camerasCard; this.camerasOpen = camerasOpen;" +
  " this.camerasPick = camerasPick; this.camerasStop = camerasStop;";
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + tail, sandbox);

const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 20; i++) await tick(); };
(async () => {
  const mark = (name) => { calls.push(["--", name]); };
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
  console.log(JSON.stringify(calls));
})();
