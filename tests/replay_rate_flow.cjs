// The replay's per-sample rate (replayRate in web/app.js), run in node. Run by tests/test_replay_js.py. Synthetic.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(require("path").join(__dirname, "..", "web", "app.js"), "utf8");
const gap = src.match(/^const TRACK_GAP_S = [^;]+;/m);
const at = src.indexOf("function replayRate("), end = src.indexOf("\n}\n", at) + 3;
if (!gap || at < 0 || end < 3) throw new Error("replayRate or TRACK_GAP_S not found in web/app.js");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(gap[0].replace("const", "var") + src.slice(at, end) + "\nthis.replayRate = replayRate;", sandbox);
const r = sandbox.replayRate;
console.log(JSON.stringify({
  missing: [r(null, 1, 1), r(undefined, 1, 1)],
  still: r(0, 1, 1),
  moving: r(0.5, 1, 1),
  gap: r(0.5, 60, 1),
  fast: r(0, 1, 300),
}));
