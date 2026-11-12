// Keyboard focus across redraws: runs keepFocus (web/app.js) and the Cameras tile (web/cameras.js) against a tiny fake
// DOM that, like a browser, drops focus to the body when the focused element is removed. Prints the results as JSON.
// Run by tests/test_focus_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.parent = null; this.on = {};
    this.style = {};
  }
  get dataset() { return { key: this.attrs["data-key"] }; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
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
    for (const c of this.children) {
      c.parent = null;
      if (doc.activeElement && c.contains(doc.activeElement)) doc.activeElement = doc.body;
    }
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
const doc = { body: new El("body"), activeElement: null, hidden: false, addEventListener() {}, head: { append() {} },
              createElement: (t) => new El(t) };
doc.activeElement = doc.body;

const h = (tag, attrs, ...kids) => {
  const el = new El(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "value") el.value = String(v);
    if (k.startsWith("on") && typeof v === "function") el.on[k.slice(2)] = v;
    else if (k !== "style") el.setAttribute(k, String(v));
  }
  el.append(...kids.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false && c !== ""));
  return el;
};

const sandbox = {
  window: { addEventListener() {} }, document: doc, h, dot: () => new El("span"),
  localStorage: { getItem: () => null, setItem() {} },
  state: { view: "live", status: {} }, AMBER: "a", GREEN: "g", CORAL: "c", MUTED: "m", Promise, Date, Object, Array,
  JSON, Math, String, Number,
  getJSON: async () => ({ robot_video_enabled: true, robot_video_configured: true }),
  // A start ends in the error phase: the token is refused, so no video library is involved.
  postJSON: async (url) => {
    if (url === "/api/video/token") throw new Error("The panel refused.");
    return { sent: true };
  },
  setInterval: () => 1, clearInterval() {}, setTimeout: () => ({ unref() {} }), clearTimeout() {},
};
vm.createContext(sandbox);
const app = fs.readFileSync(process.argv[2], "utf8");
const from = app.indexOf("/** replaceChildren"), to = app.indexOf("\n}\n", app.indexOf("function keepFocus")) + 3;
const helper = app.slice(from, to);                   // fill and keepFocus only: app.js as a whole needs a browser
vm.runInContext(helper + fs.readFileSync(process.argv[3], "utf8")
  + "\nthis.cams = cams; this.camerasCard = camerasCard; this.keepFocus = keepFocus; this.fill = fill;", sandbox);

const out = [], fill = (...a) => sandbox.fill(...a);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const here = () => { const a = doc.activeElement; return [a.tagName, a.attrs["data-key"] ?? a.attrs.id ?? null]; };
const byKey = (panel, key) => panel.querySelectorAll("[data-key]").find((e) => e.attrs["data-key"] === key);
const press = (panel, key) => { const el = byKey(panel, key); el.focus(); el.on.click(); };
(async () => {
  const panel = h("div"), outside = h("button", { id: "outside" });
  const other = h("div", null, h("button", { id: "plain-a" }), h("button", { id: "plain-b" }));
  doc.body.append(panel, outside);
  // Live's refresh: every card is replaced, the tile's card (kept between refreshes) among them.
  const refresh = async () => {
    const card = await sandbox.camerasCard({ ops: { control_enabled: false } });
    sandbox.fill(panel, other, card);
  };
  await refresh();
  out.push(["rendered", panel.querySelectorAll("[data-key]").map((e) => e.attrs["data-key"])]);
  press(panel, "cam-open"); await settle();
  out.push(["open", ...here(), sandbox.cams.phase]);                    // Open is gone: Close takes focus
  press(panel, "cam-left"); await settle();
  out.push(["pick-left", ...here(), sandbox.cams.camera]);              // the same chip, rebuilt
  const chip = doc.activeElement; await refresh(); out.push(["refresh", ...here(), doc.activeElement !== chip]);
  press(panel, "cam-retry"); await settle();
  out.push(["retry", ...here()]);                                       // Try again goes: Close takes focus
  press(panel, "cam-close"); await settle();
  out.push(["close", ...here(), sandbox.cams.phase]);                   // Close is gone: Open takes focus
  // Other cards. A keyed control keeps focus when controls before it come and go (the pending-run card); a summary is
  // focusable too; a keyed control that is now disabled is not guessed at; an unkeyed one is found again only if its
  // kind and text still single it out.
  const pending = h("div", null, h("button", { "data-key": "pending-start" }, "Start it"),
                    h("button", { "data-key": "pending-dismiss" }, "Not now"));
  const control = (off) => h("div", null, h("details", null, h("summary", { "data-key": "ready-summary" }, "ready")),
    h("button", { "data-key": "ctl-pause" }, "Pause"),
    h("button", { "data-key": "ctl-stop", ...(off ? { disabled: "" } : {}) }, "Stop"),
    h("button", { id: "plain-a" }, "Same"), h("button", { id: "plain-b" }, off ? "Same" : "Other"));
  const draw = (cards) => fill(panel, ...cards);
  const focusIn = (key) => byKey(panel, key).focus();
  draw([control(false)]); focusIn("ctl-pause"); draw([pending, control(false)]); out.push(["keyed-shift", ...here()]);
  focusIn("ready-summary"); draw([control(false)]); out.push(["summary", ...here()]);
  focusIn("ctl-stop"); draw([control(true)]); out.push(["disabled", ...here()]);
  draw([control(false)]);
  const plainB = () => panel.querySelectorAll("button").find((e) => e.attrs.id === "plain-b");
  plainB().focus(); draw([control(false)]); out.push(["unkeyed", ...here()]);
  draw([control(true)]);
  plainB().focus(); draw([control(true)]); out.push(["ambiguous", ...here()]);
  // Redraws that never go through a wrapper: any fill() keeps focus (the Camera layout's chips, readiness redrawn
  // by refreshReadiness), a link is told from another "→" by its href, and a typed value is the caller's to restore.
  const stage = h("div"); doc.body.append(stage);
  const chips = (names) => names.map((n) => h("button", { "data-key": `yardcam-${n}` }, n));
  fill(stage, chips(["a", "b"])); byKey(stage, "yardcam-b").focus();
  fill(stage, h("p"), chips(["a", "b"])); out.push(["any-fill", ...here()]);
  const layout = (cams) => [h("button", { "data-key": "yardcam-auto" }, "Auto"),
    ...cams.map((n) => h("button", { "data-key": `yardcam-cam-${n}` }, n))];
  fill(stage, layout(["auto", "b"])); byKey(stage, "yardcam-cam-auto").focus(); fill(stage, layout(["auto", "b"]));
  out.push(["camera-named-auto", ...here()]);
  const ready = h("div"); doc.body.append(ready);
  const readiness = (error) => h("div", null, error ? h("p", null, "could not check") : null,
    h("details", null, h("summary", { "data-key": "ready-summary" }, "ready")),
    h("button", { class: "linkbtn" }, "check again"));
  fill(ready, readiness(false)); byKey(ready, "ready-summary").focus(); fill(ready, readiness(true));
  out.push(["readiness-summary", ...here()]);
  ready.querySelectorAll("button")[0].focus(); fill(ready, readiness(false)); out.push(["readiness-again", ...here()]);
  const links = (hrefs) => h("ul", null, hrefs.map((x) => h("a", { href: x }, "→")));
  fill(stage, links(["#settings/a", "#settings/b"])); stage.querySelectorAll("a")[1].focus();
  fill(stage, links(["#settings/b"]));
  out.push(["link-by-href", ...here(), doc.activeElement.attrs.href]);
  fill(stage, links(["#settings/a", "#settings/a"])); stage.querySelectorAll("a")[1].focus();
  fill(stage, links(["#settings/a", "#settings/a"]));
  out.push(["link-same-href", ...here()]);
  // Two items, each with its own unkeyed "Delete": deleting one must not put focus on the other's (Enter would
  // delete it).
  const items = (names) => h("ul", null, names.map((n) => h("li", null, n, h("button", null, "Delete"))));
  fill(stage, items(["a", "b"])); stage.querySelectorAll("button")[0].focus(); fill(stage, items(["b"]));
  out.push(["deleted-item", ...here()]);
  // The emergency card's two-tap interlock: once armed, a redraw must not leave focus on the button (a stray Space or
  // Enter would be the second tap and send Resume). Unarmed, it keeps focus like any keyed control.
  const ecard = (armed) => h("div", null,
    h("button", { "data-key": "emerg-resume", "data-nofocus": armed ? "" : null }, armed ? "Tap again" : "Resume"),
    h("button", { "data-key": "emerg-stop" }, "Stop"));
  fill(stage, ecard(false)); byKey(stage, "emerg-resume").focus(); fill(stage, ecard(false));
  out.push(["emerg-unarmed", ...here()]);
  fill(stage, ecard(true)); out.push(["emerg-armed", ...here()]);
  fill(stage, ecard(true)); stage.querySelectorAll("button")[0].focus(); fill(stage, ecard(true));
  out.push(["emerg-armed-refresh", ...here()]);
  const field = (v) => h("input", { "data-key": "robot_host", value: v });
  fill(stage, field("old")); let box = byKey(stage, "robot_host"); box.focus(); box.value = "typed";
  box.setSelectionRange(2, 4);
  // what drawSettings keeps before its redraw
  const typed = { v: box.value, a: box.selectionStart, b: box.selectionEnd };
  fill(stage, field("saved")); box = byKey(stage, "robot_host"); box.value = typed.v;
  box.setSelectionRange(typed.a, typed.b);
  out.push(["typing", ...here(), box.value, box.selectionStart, box.selectionEnd]);
  outside.focus(); await refresh(); out.push(["outside", ...here()]);
  doc.activeElement = doc.body; await refresh(); out.push(["nothing-focused", ...here()]);
  console.log(JSON.stringify(out));
})();
