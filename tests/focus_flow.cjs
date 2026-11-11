// Keyboard focus across redraws: runs keepFocus (web/app.js) and the Cameras tile (web/cameras.js) against a tiny fake
// DOM that, like a browser, drops focus to the body when the focused element is removed. Prints the results as JSON.
// Run by tests/test_focus_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.parent = null; this.on = {}; this.style = {}; }
  get dataset() { return { key: this.attrs["data-key"] }; }
  setAttribute(k, v) { this.attrs[k] = v; }
  hasAttribute(k) { return k in this.attrs; }
  get textContent() { return this.text ?? this.children.map((c) => c.textContent).join(""); }
  addEventListener(e, f) { this.on[e] = f; }
  append(...kids) {
    for (const k of kids) {
      const n = typeof k === "string" ? Object.assign(new El("#text"), { text: k }) : k;
      if (n.parent) n.parent.children.splice(n.parent.children.indexOf(n), 1);
      n.parent = this; this.children.push(n);
    }
  }
  replaceChildren(...kids) {
    for (const c of this.children) { c.parent = null; if (doc.activeElement && c.contains(doc.activeElement)) doc.activeElement = doc.body; }
    this.children = []; this.append(...kids);
  }
  remove() {}
  contains(n) { for (; n; n = n.parent) if (n === this) return true; return false; }
  focus() { doc.activeElement = this; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(sel) {
    return sel.split(",").some((part) => {
      const m = part.trim().match(/^([a-z]*)(?:\[([\w-]+)(?:=(?:'([^']*)'|"([^"]*)"))?\])?$/);
      if (!m || (m[1] && m[1].toUpperCase() !== this.tagName)) return false;
      if (!m[2]) return true;
      return m[2] in this.attrs && (m[3] ?? m[4] ?? this.attrs[m[2]]) === this.attrs[m[2]];
    });
  }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
}
const doc = { body: new El("body"), activeElement: null, hidden: false, addEventListener() {}, head: { append() {} }, createElement: (t) => new El(t) };
doc.activeElement = doc.body;

const h = (tag, attrs, ...kids) => {
  const el = new El(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.on[k.slice(2)] = v; else if (k !== "style") el.setAttribute(k, String(v));
  }
  el.append(...kids.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false && c !== ""));
  return el;
};
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));

const sandbox = {
  window: { addEventListener() {} }, document: doc, h, fill, dot: () => new El("span"), localStorage: { getItem: () => null, setItem() {} },
  state: { view: "live", status: {} }, AMBER: "a", GREEN: "g", CORAL: "c", MUTED: "m", Promise, Date, Object, Array, JSON, Math, String, Number,
  getJSON: async () => ({ robot_video_enabled: true, robot_video_configured: true }),
  // A start ends in the error phase: the token is refused, so no video library is involved.
  postJSON: async (url) => { if (url === "/api/video/token") throw new Error("The panel refused."); return { sent: true }; },
  setInterval: () => 1, clearInterval() {}, setTimeout: () => ({ unref() {} }), clearTimeout() {},
};
vm.createContext(sandbox);
const app = fs.readFileSync(process.argv[2], "utf8");
const from = app.indexOf("const FOCUSABLE"), to = app.indexOf("\n}\n", app.indexOf("function keepFocus")) + 3;
const helper = app.slice(from, to);                   // the helper only: app.js as a whole needs a browser
vm.runInContext(helper + fs.readFileSync(process.argv[3], "utf8") + "\nthis.cams = cams; this.camerasCard = camerasCard; this.keepFocus = keepFocus;", sandbox);

const out = [];
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const here = () => { const a = doc.activeElement; return [a.tagName, a.attrs["data-key"] ?? a.attrs.id ?? null]; };
const byKey = (panel, key) => panel.querySelectorAll("[data-key]").find((e) => e.attrs["data-key"] === key);
const press = (panel, key) => { const el = byKey(panel, key); el.focus(); el.on.click(); };
(async () => {
  const panel = h("div"), other = h("div", null, h("button", { id: "plain-a" }), h("button", { id: "plain-b" })), outside = h("button", { id: "outside" });
  doc.body.append(panel, outside);
  // Live's refresh: every card is replaced, the tile's card (kept between refreshes) among them.
  const refresh = async () => {
    const card = await sandbox.camerasCard({ ops: { control_enabled: false } });
    sandbox.keepFocus(panel, () => fill(panel, other, card));
  };
  await refresh();
  out.push(["rendered", panel.querySelectorAll("[data-key]").map((e) => e.attrs["data-key"])]);
  press(panel, "cam-open"); await settle(); out.push(["open", ...here(), sandbox.cams.phase]);          // Open is gone: Close takes focus
  press(panel, "cam-left"); await settle(); out.push(["pick-left", ...here(), sandbox.cams.camera]);    // the same chip, rebuilt
  const chip = doc.activeElement; await refresh(); out.push(["refresh", ...here(), doc.activeElement !== chip]);
  press(panel, "cam-retry"); await settle(); out.push(["retry", ...here()]);                          // Try again goes: Close takes focus
  press(panel, "cam-close"); await settle(); out.push(["close", ...here(), sandbox.cams.phase]);      // Close is gone: Open takes focus
  // Other cards. A keyed control keeps focus when controls before it come and go (the pending-run card); a summary is
  // focusable too; a keyed control that is now disabled is not guessed at; an unkeyed one is found again only if its
  // kind and text still single it out.
  const pending = h("div", null, h("button", { "data-key": "pending-start" }, "Start it"), h("button", { "data-key": "pending-dismiss" }, "Not now"));
  const control = (off) => h("div", null, h("details", null, h("summary", { "data-key": "ready-summary" }, "ready")),
    h("button", { "data-key": "ctl-pause" }, "Pause"), h("button", { "data-key": "ctl-stop", ...(off ? { disabled: "" } : {}) }, "Stop"),
    h("button", { id: "plain-a" }, "Same"), h("button", { id: "plain-b" }, off ? "Same" : "Other"));
  const draw = (cards) => sandbox.keepFocus(panel, () => fill(panel, ...cards));
  const focusIn = (key) => byKey(panel, key).focus();
  draw([control(false)]); focusIn("ctl-pause"); draw([pending, control(false)]); out.push(["keyed-shift", ...here()]);
  focusIn("ready-summary"); draw([control(false)]); out.push(["summary", ...here()]);
  focusIn("ctl-stop"); draw([control(true)]); out.push(["disabled", ...here()]);
  draw([control(false)]);
  panel.querySelectorAll("button").find((e) => e.attrs.id === "plain-b").focus(); draw([control(false)]); out.push(["unkeyed", ...here()]);
  draw([control(true)]);
  panel.querySelectorAll("button").find((e) => e.attrs.id === "plain-b").focus(); draw([control(true)]); out.push(["ambiguous", ...here()]);
  outside.focus(); await refresh(); out.push(["outside", ...here()]);
  doc.activeElement = doc.body; await refresh(); out.push(["nothing-focused", ...here()]);
  console.log(JSON.stringify(out));
})();
