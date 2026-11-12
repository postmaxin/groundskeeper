// The emergency card's two-tap send (emergencyTap in web/ops.js), run with a synthetic clock and stubbed sends.
// Run by tests/test_focus_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(require("path").join(__dirname, "..", "web", "ops.js"), "utf8");
const start = src.indexOf("const ARM_MIN_GAP_MS");
const fnAt = src.indexOf("async function emergencyTap(", start);
const end = src.indexOf("\n}\n", fnAt) + 3;
if (start < 0 || fnAt < 0 || end < 3) throw new Error("emergencyTap not found in web/ops.js");
const cardAt = src.indexOf("function emergencyCard("), cardEnd = src.indexOf("\n}\n", cardAt) + 3;
if (cardAt < 0 || cardEnd < 3) throw new Error("emergencyCard not found in web/ops.js");

let now = 1000000;
const sent = [], timers = [];
let blurred = 0;
const focused = { closest: (sel) => (sel === ".ecard" ? {} : null), blur() { blurred++; } };
const sandbox = {
  ops: { arm: null, sending: null },
  Date: { now: () => now },
  document: { get activeElement() { return focused; } },
  refreshLive() {},
  setTimeout(f, ms) { timers.push({ f, at: now + ms }); },
  postJSON: async (url, body) => { sent.push([url, body.cmd, body.confirm]); return { sent: true, confirmed: true }; },
  reportSend() {}, toast() {},
  // What emergencyCard reads, so the real card can be rendered: a plain tree of tags, attributes and children.
  h: (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter((k) => k) }),
  HELD: new Set(), NOT_PAUSABLE: new Set(), NOT_DOCKABLE: new Set(), routeOk: () => true, noRouteWhy: () => "",
};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end) + src.slice(cardAt, cardEnd)
  + "\nthis.emergencyTap = emergencyTap; this.emergencyCard = emergencyCard;", sandbox);
const buttons = (n) => (n && n.tag === "button" ? [n] : []).concat(...((n && n.kids) || []).map(buttons));
// Which of the real card's buttons carry data-nofocus, for a robot that is mowing with Control on.
const noFocus = () => {
  const card = sandbox.emergencyCard({ ops: { control_enabled: true }, activity: "mowing", state: "mowing", run: {} });
  return buttons(card).filter((b) => b.attrs["data-nofocus"] != null).map((b) => b.attrs["data-cmd"]);
};
const tap = (cmd) => sandbox.emergencyTap(cmd, "Resume");
const runTimers = () => { for (const t of timers.splice(0)) if (t.at <= now) t.f(); else timers.push(t); };

(async () => {
  const out = {};
  out.cardUnarmed = noFocus();                                         // nothing armed: no button is marked
  sandbox.ops.arm = { cmd: "stop", at: now, until: now + 4000 };
  out.cardArmed = noFocus();                                           // only the armed one is
  sandbox.ops.arm = null;
  await tap("resume"); out.armed = !!sandbox.ops.arm && sent.length === 0;
  now += 500; await tap("resume"); out.tooQuick = { sent: sent.length, stillArmed: !!sandbox.ops.arm };
  now += 550; await tap("resume"); out.afterGap = { sent: sent.slice(), armAfter: sandbox.ops.arm };
  await tap("stop"); now += 4200; runTimers(); out.expired = { arm: sandbox.ops.arm };
  await tap("stop"); out.rearmNoSend = sent.length === 1 && !!sandbox.ops.arm;
  out.blurs = blurred;
  console.log(JSON.stringify(out));
})();
