// Which head the 3D robot draws (r3dHead and robot3dLive in web/robot3d.js), run against stubs: no browser, no
// three.js, no map. The two head groups are stand-ins with a `visible` flag, which is all r3dHead touches.
// Prints the results as JSON. Run by tests/test_robot3d_js.py (only when node is installed). Synthetic values only.
"use strict";
const fs = require("fs");
const vm = require("vm");

let repaints = 0;
const sandbox = { state: { run: null, map: { triggerRepaint() { repaints++; }, getLayer: () => null } } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.T = { R3D, r3dHead, robot3dLive };", sandbox);
const { R3D, r3dHead, robot3dLive } = sandbox.T;
const state = sandbox.state;

const out = {};
const drawn = () => (R3D.snow.visible === R3D.mower.visible ? "both or neither" : R3D.snow.visible ? "snow" : "mower");

// Before 3D has been switched on there is no model: a status must not throw, and the head is remembered.
robot3dLive({ head_type: 1 });
out.beforeBuild = R3D.head;
R3D.snow = { visible: false };
R3D.mower = { visible: true };
r3dHead();
out.afterBuild = drawn();

// Live: the attached head, from a status that carries no position.
const live = (robot) => { robot3dLive(robot); return drawn(); };
out.live = {
  mowerPro: live({ head_type: 5 }), snow: live({ head_type: 1 }), snowAsText: live({ head_type: "1" }),
  mower: live({ head_type: 3 }), leafBlower: live({ head_type: 2 }), none: live({ head_type: 0 }),
  snowAgain: live({ head_type: 1 }), notHeard: live({ head_type: null }),
  snowOnceMore: live({ head_type: 1 }), missing: live({}),
};
live({ head_type: 1 });
robot3dLive(null);                                         // no status at all: nothing changes
out.noStatus = drawn();

// A redraw is asked for only when the head drawn changes.
live({ head_type: 5 });
repaints = 0;
live({ head_type: 5 }); live({ head_type: 5 });
out.repaintsSameHead = repaints;
live({ head_type: 1 });
out.repaintsOnChange = repaints;

// Replay: the head the run was driven with, whatever is attached now.
const replay = (info, attached) => {
  R3D.mode = "replay";
  state.run = { info, rows: [] };
  robot3dLive({ head_type: attached });
  r3dHead();
  return drawn();
};
out.replay = {
  snowRunMowerNow: replay({ head_type: 1 }, 5), mowerRunSnowNow: replay({ head_type: 5 }, 1),
  runWithNoHead: replay({ head_type: null }, 1), runWithNoInfo: replay(undefined, 1),
};
R3D.mode = "live";                                         // back to live: the attached head again
state.run = null;
r3dHead();
out.backToLive = drawn();

process.stdout.write(JSON.stringify(out));
