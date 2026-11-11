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
};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end) + "\nthis.emergencyTap = emergencyTap;", sandbox);
const tap = (cmd) => sandbox.emergencyTap(cmd, "Resume");
const runTimers = () => { for (const t of timers.splice(0)) if (t.at <= now) t.f(); else timers.push(t); };

(async () => {
  const out = {};
  await tap("resume"); out.armed = !!sandbox.ops.arm && sent.length === 0;
  now += 100; await tap("resume"); out.tooQuick = { sent: sent.length, stillArmed: !!sandbox.ops.arm };
  now += 350; await tap("resume"); out.afterGap = { sent: sent.slice(), armAfter: sandbox.ops.arm };
  await tap("stop"); now += 4200; runTimers(); out.expired = { arm: sandbox.ops.arm };
  await tap("stop"); out.rearmNoSend = sent.length === 1 && !!sandbox.ops.arm;
  out.blurs = blurred;
  console.log(JSON.stringify(out));
})();
