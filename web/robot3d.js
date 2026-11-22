/* The robot as a 3D model on the map's 3D view (2026-10-10).
   A three.js custom layer, built from simple shapes about the robot's size (forward is +Y, up is +Z, in metres). It
   stands on the drawn ground, turns to the robot's compass heading and tilts by its pitch and roll: on run 4 the
   pitch read positive nose-down and the roll positive left side up. On a ground drawn taller than it is, the tilt is
   drawn taller to match (atan(height x tan(angle))). Zoomed out it keeps a minimum size on screen. It is lit from
   the north-north-west whichever way it faces, as the ground's shading is, and stands on a soft shadow. The model is
   a Yarbo core with the Lawn Mower Pro; the mower hangs on the core's hitch and lies on the ground under it. With the
   Snow Blower as the head (type 1: live the attached head, in a replay the run's) the core carries that instead.
   Live, it takes every position sample the robot sends (the stream's pose events, #35: ~1 a second while the phone
   app is open, else the panel's own reads),
   in the robot's own time order: a late one from the backlog after a dropout is dropped instead of drawn as a detour.
   A single sample further away than the robot could have driven is held until the next one confirms or drops it.
   Between samples it carries on at the reported speed and heading for up to 2.5 s, then catches up with the next
   real position, faster the further behind it is but never in a jump. In a replay it follows the replay point.
   three.js is loaded the first time the 3D view is switched on.
   Follow (2026-10-11): the camera stays with the model, behind it and R3D_FOLLOW_SIDE° to one side, and turns with
   its heading at most R3D_FOLLOW_TURN°/s, so the U-turn at the end of each stripe swings it round instead of
   spinning it. Right-drag (two fingers) moves the camera round the robot and it keeps that angle; zoom and tilt
   stay as set; a pan or Fit yard lets go. */
const R3D = {
  THREE: null, loading: null, layer: null,
  pose: null,         // what is drawn: {lon, lat, heading, pitch, roll}
  fix: null,          // the last live position: {lon, lat, heading, pitch, roll, speed, t (robot time), at}
  held: null,         // a far sample waiting for the next one to confirm it (the speed gate): {s, at}
  pace: 0,            // metres a second between recent samples, smoothed (the speed gate)
  glide: null,        // a replay step: {from, to, t0, ms}
  mode: "live",       // live (the robot now) | replay (a selected run's replay point)
  colour: null,
  head: null,         // the attached head's type, from the last live status (null: none heard)
  raf: 0, last: 0,
  follow: { on: false, offset: 45, cam: null, turning: false, hands: false, wheelAt: 0 },   // cam: the heading the camera has turned to
};
const R3D_DEG = Math.PI / 180;
const R3D_LEN = 1.40;            // metres, the mower's front to the back of the tracks
const R3D_MIN_PX = 34;           // the model is never drawn shorter than this on screen
const R3D_AHEAD_S = 2.5;         // keep moving on the last speed and heading for this long, then wait
const R3D_EASE_S = 0.5;          // how quickly it closes on where it should be
const R3D_JUMP_M = 25;           // further behind than this (a new run, a long gap): put it there at once
const R3D_RESET_S = 30;          // nothing newer by the robot's clock this long: take the next sample anyway
const R3D_GATE_MPS = 2;          // faster than this between two samples is a stray reading, not the robot (it does ~1 m/s)
const R3D_GATE_PACE = 2.5;       // ...or than this many times the pace it has been keeping (a sped-up demo is faster)
const R3D_GATE_M = 3;            // ...beyond this much room for noise, whatever the time between them
const R3D_GATE_HOLD_S = 6;       // a far sample nothing confirms is dropped; one this much later is taken anyway
const R3D_MOWER_PITCH = 10;      // degrees the mower turns about the hitch at most, nose up or down...
const R3D_MOWER_ROLL = 6;        // ...and side to side
const R3D_FOLLOW_SIDE = 45;     // the camera sits behind the robot and this far round to one side
const R3D_FOLLOW_TURN = 40;      // degrees a second it turns at most to stay behind the robot
const R3D_FOLLOW_LEAD = 1.5;     // metres ahead of the robot the camera looks at

const r3dWanted = () => typeof want3d === "function" && want3d() && state.mapReady;
const r3dMetres = (lat) => [111320 * Math.cos(lat * R3D_DEG), 111320];
const r3dAngle = (a) => ((a % 360) + 540) % 360 - 180;            // -180..180

function r3dBuild(THREE) {
  const g = new THREE.Group();
  const mat = (c) => new THREE.MeshLambertMaterial({ color: c });
  // A soft shadow on the ground under it, so it stands on the lawn instead of floating over it: three see-through
  // ellipses, darker toward the middle. They are drawn through the ground (the map's ground hides anything level
  // with it) but before the robot, so it covers them: blended in the opaque pass, which keeps renderOrder.
  // Both sides drawn, so the way they face never hides them. (A faded texture or per-vertex alpha did not show here.)
  [[0.80, 1.0], [0.66, 0.86], [0.54, 0.72]].forEach(([rx, ry], i) => {
    const m = new THREE.Mesh(new THREE.CircleGeometry(1, 40), new THREE.MeshBasicMaterial({
      color: 0x000000, opacity: 0.28, transparent: false, blending: THREE.CustomBlending,
      blendSrc: THREE.SrcAlphaFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
      depthTest: false, depthWrite: false, side: THREE.DoubleSide }));
    m.scale.set(rx, ry, 1);
    m.position.set(0, 0.04, 0.01);
    m.renderOrder = -1 - i;
    g.add(m);
  });
  // A Yarbo core with the Lawn Mower Pro, about life size, from simple solids (it has to read at 34 px as well as
  // close up). Two parts: the core (tracks, shell, antennas, stop button, beacon) and the mower, which hangs on the
  // core's front hitch and turns about it (R3D.mower.rotation: x pitch, y roll), so it can lie on the ground while
  // the core tilts. Parts are placed in the robot's own frame; `madd` moves them into the mower's.
  const core = new THREE.Group(), mower = new THREE.Group();
  const HITCH = [0, 0.13, 0.17];
  mower.position.set(...HITCH);
  g.add(core, mower);
  R3D.mower = mower;
  const black = mat(MAP.body3d), graphite = mat(MAP.frame3d), yellow = mat(MAP.trim3d), tread = mat(MAP.track3d), red = mat(MAP.stop3d);
  const put = (to, off) => (geo, m, x, y, z) => {
    const o = new THREE.Mesh(geo, m);
    o.position.set(x - off[0], y - off[1], z - off[2]);
    to.add(o);
    return o;
  };
  const add = put(core, [0, 0, 0]), madd = put(mower, HITCH);
  // A rounded rectangle, w across and l long, centred on 0,0.
  const rounded = (w, l, r) => {
    const s = new THREE.Shape(), x = w / 2, y = l / 2;
    s.moveTo(-x + r, -y);
    s.lineTo(x - r, -y); s.quadraticCurveTo(x, -y, x, -y + r);
    s.lineTo(x, y - r); s.quadraticCurveTo(x, y, x - r, y);
    s.lineTo(-x + r, y); s.quadraticCurveTo(-x, y, -x, y - r);
    s.lineTo(-x, -y + r); s.quadraticCurveTo(-x, -y, -x + r, -y);
    return s;
  };
  // A slab of that outline, `h` tall from its base, edges softened by `bevel`.
  const slab = (w, l, h, r, bevel = 0.012, seg = 2) => {
    const geo = new THREE.ExtrudeGeometry(rounded(w - 2 * bevel, l - 2 * bevel, Math.max(0.001, r - bevel)), {
      depth: h - 2 * bevel, bevelEnabled: true, bevelSize: bevel, bevelThickness: bevel, bevelSegments: seg, curveSegments: 10 });
    geo.translate(0, 0, bevel);
    return geo;
  };
  // A flat-faced hip shape: a w x l base at z 0 narrowing to a wt x lt top at z h (the top moved `dy` along y).
  const hip = (w, l, wt, lt, h, dy = 0) => {
    const b = [[-w / 2, -l / 2], [w / 2, -l / 2], [w / 2, l / 2], [-w / 2, l / 2]];
    const t = [[-wt / 2, -lt / 2 + dy], [wt / 2, -lt / 2 + dy], [wt / 2, lt / 2 + dy], [-wt / 2, lt / 2 + dy]];
    const v = [];
    const quad = (p, q, r, s) => v.push(...p, ...q, ...r, ...p, ...r, ...s);
    const B = (i) => [b[i][0], b[i][1], 0], T = (i) => [t[i][0], t[i][1], h];
    for (let i = 0; i < 4; i++) quad(B(i), B((i + 1) % 4), T((i + 1) % 4), T(i));
    quad(T(0), T(1), T(2), T(3));
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(v, 3));
    geo.computeVertexNormals();
    return geo;
  };
  const upright = (geo) => { geo.rotateX(Math.PI / 2); return geo; };       // three's cylinders stand along y

  // The core: two rubber tracks, the chassis between them, the rounded shell over them.
  const TL = 0.78, TH = 0.24, TW = 0.14, TX = 0.27, CY = -0.27, R = TH / 2;
  const belt = new THREE.ExtrudeGeometry(rounded(TL, TH, R - 0.001), {
    depth: TW - 0.02, bevelEnabled: true, bevelSize: 0.01, bevelThickness: 0.01, bevelSegments: 2, curveSegments: 14 });
  belt.rotateY(Math.PI / 2);                               // the outline's across -> along, its depth -> across
  belt.rotateX(Math.PI / 2);                               // its height -> up
  belt.translate(-(TW - 0.02) / 2, 0, R);
  const plate = new THREE.ExtrudeGeometry(rounded(TL - 0.15, TH - 0.11, 0.06), { depth: 0.012, bevelEnabled: false, curveSegments: 10 });
  plate.rotateY(Math.PI / 2);
  plate.rotateX(Math.PI / 2);
  plate.translate(-0.006, 0, R);
  const lug = new THREE.BoxGeometry(TW + 0.014, 0.03, 0.016), stripe = new THREE.BoxGeometry(0.004, 0.26, 0.007);
  const half = TL / 2 - R;                                 // the straight run's half length
  for (const side of [1, -1]) {
    add(belt, tread, side * TX, CY, 0);
    add(plate, graphite, side * (TX + TW / 2), CY, 0);                      // the plate inside the belt
    add(stripe, yellow, side * (TX + TW / 2 + 0.012), CY - 0.04, R - 0.03); // its yellow line
    for (let y = -half; y <= half + 1e-6; y += 0.054) add(lug, tread, side * TX, CY + y, TH + 0.005);   // tread blocks: top
    for (const end of [1, -1]) {                                            // ...and round both ends
      for (let a = -60; a <= 60; a += 30) {
        const f = a * R3D_DEG;
        const o = add(lug, tread, side * TX, CY + end * (half + (R + 0.005) * Math.cos(f)), R + (R + 0.005) * Math.sin(f));
        o.rotation.x = end * (f - Math.PI / 2);
      }
    }
  }
  add(slab(0.40, 0.70, 0.13, 0.04), graphite, 0, CY, 0.11);                 // chassis, between the tracks
  add(slab(0.50, 0.64, 0.15, 0.13, 0.04, 4), black, 0, CY - 0.01, 0.235);   // the shell, over them
  add(hip(0.40, 0.52, 0.30, 0.40, 0.028, 0), black, 0, CY - 0.01, 0.378);   // its crown
  // Yellow shoulders on the far sides, each with a stubby antenna (round foot, thin neck, thick grip).
  const foot = upright(new THREE.CylinderGeometry(0.03, 0.036, 0.03, 16)), neck = upright(new THREE.CylinderGeometry(0.011, 0.013, 0.07, 10));
  const grip = upright(new THREE.CapsuleGeometry(0.023, 0.10, 4, 12));
  for (const side of [1, -1]) {
    add(slab(0.11, 0.32, 0.10, 0.035, 0.015, 3), yellow, side * 0.275, CY + 0.13, 0.235);
    add(foot, graphite, side * 0.285, CY + 0.10, 0.345);
    add(neck, black, side * 0.285, CY + 0.10, 0.395);
    add(grip, black, side * 0.285, CY + 0.10, 0.50);
  }
  add(upright(new THREE.CylinderGeometry(0.004, 0.005, 0.26, 6)), black, -0.19, CY - 0.26, 0.47);       // thin whip, back left
  add(slab(0.13, 0.085, 0.012, 0.012, 0.004), graphite, 0, CY - 0.19, 0.402);                           // stop button's plate
  add(slab(0.10, 0.058, 0.032, 0.012, 0.006), red, 0, CY - 0.19, 0.410);                                // the red stop button, top back
  // The beacon on top, in the status colour (the real one is orange).
  R3D.lamp = add(upright(new THREE.CylinderGeometry(0.026, 0.03, 0.022, 16)),
                 new THREE.MeshBasicMaterial({ color: R3D.colour || MAP.robot }), 0, CY + 0.06, 0.416);

  // The Lawn Mower Pro: a hitch box, a wide two-step skirt and a faceted housing with its yellow handles.
  const MY = 0.44;                                                          // the skirt's middle
  madd(slab(0.28, 0.20, 0.19, 0.03, 0.012), black, 0, 0.13, 0.12);          // hitch box, on the core's front
  madd(slab(0.16, 0.05, 0.03, 0.012, 0.006), graphite, 0, 0.12, 0.31);      // its grab handle
  madd(slab(0.82, 0.60, 0.075, 0.15, 0.02, 3), black, 0, MY, 0.03);         // skirt: the wide lip over the blades
  madd(slab(0.75, 0.53, 0.07, 0.14, 0.02, 3), graphite, 0, MY, 0.095);      // ...and the step above it
  madd(slab(0.60, 0.42, 0.07, 0.10, 0.02, 3), black, 0, MY - 0.02, 0.155);  // housing base
  madd(hip(0.54, 0.36, 0.28, 0.18, 0.12, -0.03), black, 0, MY - 0.02, 0.215);    // housing roof, falling to the front
  madd(upright(new THREE.CylinderGeometry(0.04, 0.04, 0.008, 20)), yellow, 0, MY - 0.05, 0.337);      // the badge on the roof
  madd(upright(new THREE.CylinderGeometry(0.03, 0.03, 0.012, 20)), black, 0, MY - 0.05, 0.338);
  const knob = new THREE.SphereGeometry(0.055, 16, 10);
  knob.scale(1, 1, 0.6);
  const bar = slab(0.045, 0.24, 0.04, 0.015, 0.008), leg = slab(0.045, 0.045, 0.06, 0.012, 0.006);
  for (const side of [1, -1]) {
    madd(knob, yellow, side * 0.215, MY + 0.07, 0.235);                     // yellow knobs on the housing's shoulders
    madd(bar, yellow, side * 0.335, MY + 0.02, 0.205);                      // yellow carry handles on the far sides
    for (const e of [1, -1]) madd(leg, yellow, side * 0.335, MY + 0.02 + e * 0.0975, 0.155);
  }
  const light = new THREE.MeshBasicMaterial({ color: MAP.light3d });
  for (const side of [1, -1]) madd(new THREE.BoxGeometry(0.15, 0.012, 0.026), light, side * 0.17, MY + 0.262, 0.128);   // headlights
  madd(slab(0.13, 0.06, 0.055, 0.012, 0.006), black, 0, MY + 0.225, 0.16);  // the sensor box between them
  madd(new THREE.BoxGeometry(0.46, 0.012, 0.014), red, 0, MY + 0.297, 0.062);   // red strip under the front

  // The Snow Blower, drawn instead of the mower when it is the head (r3dHead). It is fixed to the core's front and
  // does not turn about the hitch. An impeller box on the hitch, the auger housing open to the front between two
  // side plates with their yellow panels, the guard bar round its top, the wire guard before the mouth, the chute.
  const snow = new THREE.Group();
  snow.visible = false;
  g.add(snow);
  R3D.snow = snow;
  const sadd = put(snow, [0, 0, 0]);
  // A flat plate cut to an outline of [y, z] points, `t` thick across the robot.
  const cut = (pts, t) => {
    const o = new THREE.Shape();
    pts.forEach(([y, z], i) => (i ? o.lineTo(y, z) : o.moveTo(y, z)));
    const geo = new THREE.ExtrudeGeometry(o, { depth: t, bevelEnabled: false });
    geo.rotateY(Math.PI / 2);                              // as the belts: the outline's across -> along...
    geo.rotateX(Math.PI / 2);                              // ...its height -> up, its depth -> across
    geo.translate(-t / 2, 0, 0);
    return geo;
  };
  // A round bar through [x, y, z] points, the corners eased.
  const tube = (pts, r) => new THREE.TubeGeometry(
    new THREE.CatmullRomCurve3(pts.map((q) => new THREE.Vector3(...q)), false, "catmullrom", 0.15), 64, r, 8);
  const across = (geo) => { geo.rotateZ(Math.PI / 2); return geo; };        // three's cylinders stand along y
  const SY = 0.36, SZ = 0.17;                                               // the auger's shaft
  sadd(slab(0.34, 0.22, 0.315, 0.04, 0.012), black, 0, 0.12, 0.08);         // impeller box, on the core's front
  sadd(new THREE.BoxGeometry(0.64, 0.03, 0.34), tread, 0, 0.225, 0.20);     // housing: the back wall
  sadd(new THREE.BoxGeometry(0.64, 0.26, 0.02), tread, 0, 0.35, 0.02);      // ...the floor
  sadd(slab(0.66, 0.24, 0.05, 0.02, 0.01), black, 0, 0.32, 0.345);          // ...the top
  sadd(slab(0.66, 0.09, 0.10, 0.02, 0.012), black, 0, 0.455, 0.30);         // ...and the beam over the mouth
  const cheek = cut([[0.19, 0.04], [0.44, 0], [0.51, 0.05], [0.50, 0.33], [0.43, 0.40], [0.19, 0.40]], 0.03);
  const shield = cut([[0.34, 0.05], [0.45, 0.03], [0.487, 0.07], [0.48, 0.27], [0.42, 0.33], [0.34, 0.27]], 0.012);
  const flight = across(new THREE.CylinderGeometry(0.13, 0.13, 0.012, 20));
  for (const side of [1, -1]) {
    sadd(cheek, black, side * 0.335, 0, 0);                                 // side plates
    sadd(shield, yellow, side * 0.353, 0, 0);                               // their yellow panels
    sadd(new THREE.BoxGeometry(0.12, 0.012, 0.03), light, side * 0.2, 0.502, 0.35);     // headlights, in the beam
    // auger flights, leaning to the middle
    for (const x of [0.07, 0.17, 0.27]) sadd(flight, graphite, side * x, SY, SZ).rotation.z = side * 0.45;
  }
  sadd(across(new THREE.CylinderGeometry(0.02, 0.02, 0.60, 10)), graphite, 0, SY, SZ);
  sadd(tube([[-0.35, 0.17, 0.30], [-0.35, 0.19, 0.45], [-0.35, 0.47, 0.46], [-0.29, 0.53, 0.46], [0.29, 0.53, 0.46],
             [0.35, 0.47, 0.46], [0.35, 0.19, 0.45], [0.35, 0.17, 0.30]], 0.019), graphite, 0, 0, 0);   // guard bar
  const wire = tube([[-0.33, 0.47, 0], [-0.31, 0.57, 0], [-0.22, 0.61, 0],
                     [0.22, 0.61, 0], [0.31, 0.57, 0], [0.33, 0.47, 0]], 0.006);
  for (const z of [0.05, 0.10, 0.15]) sadd(wire, graphite, 0, 0, z);        // wire guard before the mouth
  sadd(slab(0.50, 0.035, 0.025, 0.012, 0.006), yellow, 0, 0.455, 0.40);     // the yellow scraper bar, across the front
  // The chute: a collar, the column leaning back and the deflector on top, with its yellow joint. Drawn thrown to
  // the left front; the real one turns, and which reading gives its angle is not known yet (#132).
  const chute = new THREE.Group();
  chute.position.set(0, 0.16, 0.39);
  chute.rotation.z = 0.5;
  snow.add(chute);
  const cadd = put(chute, [0, 0, 0]);
  cadd(upright(new THREE.CylinderGeometry(0.085, 0.10, 0.05, 20)), graphite, 0, 0, 0.025);
  cadd(slab(0.13, 0.11, 0.27, 0.04, 0.012), black, 0, -0.01, 0.04).rotation.x = 0.22;
  cadd(slab(0.14, 0.12, 0.13, 0.04, 0.012), black, 0, -0.065, 0.285).rotation.x = -0.6;
  cadd(across(new THREE.CylinderGeometry(0.036, 0.036, 0.05, 16)), yellow, -0.085, -0.07, 0.30);
  cadd(across(new THREE.CylinderGeometry(0.02, 0.02, 0.012, 12)), graphite, -0.114, -0.07, 0.30);
  return g;
}

// Which head is drawn: the Snow Blower when it is the head (type 1), else the mower. Live that is the attached
// head, in a replay the head the run was driven with.
function r3dHead() {
  const t = R3D.mode === "replay" && state.run ? (state.run.info || {}).head_type : R3D.head;
  const snow = Number(t) === 1;
  if (!R3D.snow || R3D.snow.visible === snow) return;
  R3D.snow.visible = snow;
  R3D.mower.visible = !snow;
  if (state.map) state.map.triggerRepaint();
}

function r3dLayer(THREE) {
  return {
    id: "robot3d", type: "custom", renderingMode: "3d",
    // The layer comes off and goes back on with the 3D view; the scene and renderer are made once and kept, so
    // switching does not leave a renderer and its buffers behind each time.
    onAdd(map, gl) {
      this.map = map;
      if (this.renderer) return;
      this.camera = new THREE.Camera();
      this.scene = new THREE.Scene();                 // x east, y north, z up, in metres: the light stays put as the robot turns
      const sky = new THREE.HemisphereLight(0xffffff, 0x444444, 1.6);
      sky.position.set(0, 0, 1);                      // the sky is up
      this.scene.add(sky);
      const sun = new THREE.DirectionalLight(0xffffff, 1.2);
      sun.position.set(-1, 2, 3);                    // from 333°, about where the ground's shading is lit from (335°)
      this.scene.add(sun);
      this.robot = r3dBuild(THREE);
      this.scene.add(this.robot);
      r3dHead();
      this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
      this.renderer.autoClear = false;
    },
    render(gl, matrix) {
      const p = R3D.pose, map = this.map;
      if (!p) return;
      const at = [p.lon, p.lat];
      const mc = maplibregl.MercatorCoordinate.fromLngLat(at, map.queryTerrainElevation(at) || 0);  // includes the height x
      const mpp = (40075016.686 * Math.cos(p.lat * R3D_DEG)) / (512 * 2 ** map.getZoom());
      const s = mc.meterInMercatorCoordinateUnits() * Math.max(1, (R3D_MIN_PX * mpp) / R3D_LEN);
      const x = (map.getTerrain() && map.getTerrain().exaggeration) || 1;
      const nose = Math.atan(x * Math.tan(-(p.pitch || 0) * R3D_DEG));
      const left = Math.atan(x * Math.tan((p.roll || 0) * R3D_DEG));
      const M = THREE.Matrix4;
      this.robot.rotation.set(nose, left, -(p.heading || 0) * R3D_DEG, "ZXY");      // heading, then pitch, then roll
      r3dMowerOnGround(map, p, s / mc.meterInMercatorCoordinateUnits());
      const place = new M().makeTranslation(mc.x, mc.y, mc.z).scale(new THREE.Vector3(s, -s, s));
      this.camera.projectionMatrix = new M().fromArray(matrix).multiply(place);
      this.renderer.resetState();
      this.renderer.render(this.scene, this.camera);
    },
  };
}

// The mower hangs on the core's hitch, so it lies on the ground under it rather than along the core: where the
// drawn ground's slope under the mower differs from the slope under the tracks (a crest, a dip, the foot of a bank)
// it turns about the hitch by the difference, at most R3D_MOWER_PITCH / R3D_MOWER_ROLL degrees. Worked out from
// the ground alone (the robot reports no hitch angle), eased so it does not twitch. `k` is how much larger than
// life the model is drawn (zoomed out), so the ground is read where the drawn parts are.
function r3dMowerOnGround(map, p, k) {
  const m = R3D.mower;
  if (!m || !m.visible) return;                                             // the snow blower is fixed to the core
  const h = (p.heading || 0) * R3D_DEG, [kx, ky] = r3dMetres(p.lat);
  const ground = (x, y) => map.queryTerrainElevation(                       // at x right, y ahead of the robot (m)
    [p.lon + (k * (x * Math.cos(h) + y * Math.sin(h))) / kx, p.lat + (k * (y * Math.cos(h) - x * Math.sin(h))) / ky]);
  const back = ground(0, -0.66), hitch = ground(0, 0.13), front = ground(0, 0.74);
  const cl = ground(-0.3, -0.27), cr = ground(0.3, -0.27), ml = ground(-0.35, 0.44), mr = ground(0.35, 0.44);
  let pitch = 0, roll = 0;
  if ([back, hitch, front, cl, cr, ml, mr].every((v) => typeof v === "number")) {
    const lim = (v, deg) => Math.max(-deg * R3D_DEG, Math.min(deg * R3D_DEG, v));
    pitch = lim(Math.atan2(front - hitch, 0.61 * k) - Math.atan2(hitch - back, 0.79 * k), R3D_MOWER_PITCH);   // nose up +
    roll = lim(Math.atan2(ml - mr, 0.7 * k) - Math.atan2(cl - cr, 0.6 * k), R3D_MOWER_ROLL);                 // left up +
  }
  m.rotation.x += (pitch - m.rotation.x) * 0.25;
  m.rotation.y += (roll - m.rotation.y) * 0.25;
}

// The dots the model stands in for: the live robot's, or the replay point's.
function r3dDots(hideLive, hideReplay) {
  paint("robot", "circle-opacity", hideLive ? 0 : 1);
  paint("robot", "circle-stroke-opacity", hideLive ? 0 : 1);
  paint("robot-heading", "icon-opacity", hideLive ? 0 : 1);
  paint("robot-halo", "circle-opacity", hideLive ? 0 : 0.25);
  paint("replay", "circle-opacity", hideReplay ? 0 : 1);
  paint("replay", "circle-stroke-opacity", hideReplay ? 0 : 1);
}

// Show or hide the model to match the map (3D on, view), and point it at the live robot or the replay.
async function robot3dSync() {
  const map = state.map;
  if (!map) return;
  const want = r3dWanted();
  const mode = state.run ? "replay" : "live";
  if (mode !== R3D.mode) { R3D.mode = mode; R3D.pose = null; R3D.glide = null; }
  if (!want) {
    robot3dFollow(false);
    if (map.getLayer("robot3d")) map.removeLayer("robot3d");
    r3dDots(false, false);
    return;
  }
  if (!R3D.THREE) {
    try {
      R3D.loading ||= import("/vendor/three.module.min.js");
      R3D.THREE = await R3D.loading;
    } catch (e) {
      R3D.loading = null;
      toast(`3D robot unavailable: ${e.message}`);
      return;
    }
    if (!r3dWanted()) return;
  }
  if (!map.getLayer("robot3d")) map.addLayer(R3D.layer ||= r3dLayer(R3D.THREE));        // on top of everything
  if (!R3D.bound) {
    R3D.bound = true;
    map.on("dragstart", (e) => { if (e.originalEvent && R3D.follow.on) robot3dFollow(false); });   // a pan lets go
    map.on("rotatestart", (e) => { if (e.originalEvent) R3D.follow.turning = true; });
    map.on("zoomend", (e) => { if (e.originalEvent && R3D.follow.on) r3dKick(); });    // a zoom about the cursor: back on the robot
    // Moving the camera stops any gesture under way, so it holds still while a finger or button is on the map (and
    // just after a wheel zoom): a pan then gets through (and lets go), a right-drag sets a new angle. Every finger
    // is counted, so lifting one of two does not move the camera under the other, and a touch the browser takes
    // over (pointercancel) lets go like a lifted one.
    const box = map.getCanvasContainer(), down = new Set();
    const free = () => { if (!R3D.follow.hands) return; R3D.follow.hands = false; if (R3D.follow.on) r3dKick(); };
    const lift = (e) => { down.delete(e.pointerId); if (!down.size) free(); };
    box.addEventListener("pointerdown", (e) => { down.add(e.pointerId); R3D.follow.hands = true; });
    box.addEventListener("wheel", () => { R3D.follow.wheelAt = performance.now(); }, { passive: true });
    window.addEventListener("pointerup", lift);
    window.addEventListener("pointercancel", lift);
    window.addEventListener("blur", () => { down.clear(); free(); });        // a lift the page never hears of
    map.on("rotateend", (e) => {
      if (!R3D.follow.turning) return;
      R3D.follow.turning = false;
      if (R3D.follow.on && R3D.follow.cam !== null) R3D.follow.offset = r3dAngle(map.getBearing() - R3D.follow.cam);
    });
  }
  r3dDots(mode === "live", mode === "replay");
  r3dHead();
  if (mode === "replay" && state.run) robot3dReplay(state.run.rows[Math.min(state.replayIndex, state.run.rows.length - 1)]);
  else if (R3D.fix && !R3D.pose) R3D.pose = { ...R3D.fix };
  r3dKick();
}

// A live status: where the robot is now. Called with status.robot.
function robot3dLive(robot) {
  if (robot) { R3D.head = robot.head_type ?? null; r3dHead(); }             // no head heard: back to the mower
  const p = robot && robot.position;
  if (!p) return;
  const sp = (robot.speed || []).filter((v) => typeof v === "number");
  r3dTake({ ...p, pitch: robot.pitch, roll: robot.roll, speed: sp.length ? sp.reduce((a, b) => a + b, 0) / sp.length : 0 });
}

// The pose stream's samples (/api/stream "pose", ~1 a second while it moves), oldest first.
function robot3dPoses(list) {
  for (const s of list || []) r3dTake(s);
}

// One position sample: {lon, lat, heading, pitch, roll, speed, t} with t the robot's own time for it. The same time
// again is the same sample (the status repeats the last one every 1.5 s, also through a dropout) and is dropped. An
// older one is late (the backlog after a dropout) and dropped too, unless nothing newer has come for R3D_RESET_S
// (the robot's clock was set back).
function r3dTake(s) {
  if (!s || s.lon === null || s.lon === undefined) return;
  const now = performance.now(), f = R3D.fix;
  if (f && f.t != null && s.t != null && (s.t === f.t || (s.t < f.t && now - f.at < R3D_RESET_S * 1000))) return;
  if (f && s.t == null && s.lon === f.lon && s.lat === f.lat) return;     // no robot time: the same sample again
  if (!r3dGate(s, now)) return;
  R3D.fix = { lon: s.lon, lat: s.lat, heading: s.heading ?? (f ? f.heading : 0), pitch: s.pitch || 0, roll: s.roll || 0,
              speed: s.speed || 0, t: s.t ?? null, at: now };
  if (!state.map || !state.map.getLayer("robot3d") || R3D.mode !== "live") return;      // kept for when 3D comes on
  if (!R3D.pose) R3D.pose = { ...R3D.fix };
  r3dKick();
}

// The speed gate: whether to take a sample. One that is further from the last than the robot could have driven in
// the time between them is held, not drawn: a single stray position would otherwise send the model off and back.
// The next sample decides. Near the held one, the robot really moved (it was carried, or a long gap ended) and the
// jump is taken; near the last one, the held one was a stray and is dropped. A held sample that nothing has
// confirmed for R3D_GATE_HOLD_S is dropped, and after that long with nothing taken the next sample is taken as it is.
// The pace it allows is the larger of R3D_GATE_MPS and R3D_GATE_PACE times the pace kept between recent samples.
function r3dGate(s, now) {
  const f = R3D.fix;
  if (!f || now - f.at > R3D_GATE_HOLD_S * 1000) { R3D.held = null; R3D.pace = 0; return true; }
  const dist = (a, b) => { const [kx, ky] = r3dMetres(b.lat); return Math.hypot((a.lon - b.lon) * kx, (a.lat - b.lat) * ky); };
  const gap = (a, b, aAt, bAt) => Math.max(0, a.t != null && b.t != null ? a.t - b.t : (aAt - bAt) / 1000);
  const fits = (d, secs) => d <= R3D_GATE_M + Math.max(R3D_GATE_MPS, R3D_GATE_PACE * (R3D.pace || 0)) * secs;
  const take = (d, secs) => {          // a sample taken: the pace between it and the one before, smoothed
    if (secs > 0.2 && secs <= 3) R3D.pace = 0.7 * (R3D.pace || 0) + 0.3 * Math.min(10, d / secs);
    R3D.held = null;
    return true;
  };
  const df = dist(s, f), gf = gap(s, f, now, f.at);
  if (fits(df, gf)) return take(df, gf);
  const h = R3D.held;
  // The held sample again (the status repeats the newest pose, which the pose stream also sends): not a second
  // sample, so it confirms nothing and the hold stands.
  if (h && (s.t != null ? s.t === h.s.t : s.lon === h.s.lon && s.lat === h.s.lat)) return false;
  if (h && now - h.at <= R3D_GATE_HOLD_S * 1000) {
    const dh = dist(s, h.s), gh = gap(s, h.s, now, h.at);
    if (fits(dh, gh)) return take(dh, gh);
  }
  R3D.held = { s, at: now };           // too far from the last: wait for the next one to confirm it
  return false;
}

// A replay step: glide from where the model is to the replay point (row, or `frac` of the way on to `next`) over the
// next timer tick.
function robot3dReplay(row, next = null, frac = 0) {
  if (!row || !state.run || R3D.mode !== "replay" || !state.map || !state.map.getLayer("robot3d")) return;
  const f = Object.fromEntries(state.run.fields.map((k, i) => [k, i]));
  const pose = (r) => ({ lon: r[f.lon], lat: r[f.lat], heading: r[f.heading] ?? (R3D.pose ? R3D.pose.heading : 0),
                         pitch: r[f.pitch] || 0, roll: r[f.roll] || 0 });
  const to = pose(row);
  if (next && frac > 0) {
    const b = pose(next);
    to.lon += (b.lon - to.lon) * frac;
    to.lat += (b.lat - to.lat) * frac;
    to.heading = (to.heading + r3dAngle(b.heading - to.heading) * frac + 360) % 360;
    to.pitch += (b.pitch - to.pitch) * frac;
    to.roll += (b.roll - to.roll) * frac;
  }
  if (!R3D.pose) { R3D.pose = to; r3dKick(); return; }
  const [kx, ky] = r3dMetres(to.lat);
  const far = Math.hypot((to.lon - R3D.pose.lon) * kx, (to.lat - R3D.pose.lat) * ky) > R3D_JUMP_M * 4;
  R3D.glide = far ? null : { from: { ...R3D.pose }, to, t0: performance.now(), ms: state.replayTimer ? 55 : 120 };
  if (far) R3D.pose = to;
  r3dKick();
}

function robot3dColour(c) {
  R3D.colour = c;
  if (R3D.lamp) R3D.lamp.material.color.set(c);
  if (state.map && state.map.getLayer("robot3d")) state.map.triggerRepaint();
}

function r3dKick() {
  if (!R3D.raf) { R3D.last = performance.now(); R3D.raf = requestAnimationFrame(r3dTick); }
}

// One frame: move the drawn pose toward where the robot should be; stop asking for frames once it is there.
function r3dTick(now) {
  R3D.raf = 0;
  const map = state.map;
  if (!map || !map.getLayer("robot3d") || !R3D.pose) return;
  const dt = Math.max(0, Math.min(0.25, (now - R3D.last) / 1000));   // a frame's time can be a little before the request
  R3D.last = now;
  let busy = false;
  const p = R3D.pose;
  if (R3D.mode === "replay" && R3D.glide) {
    const g = R3D.glide, t = Math.min(1, (now - g.t0) / g.ms);
    p.lon = g.from.lon + (g.to.lon - g.from.lon) * t;
    p.lat = g.from.lat + (g.to.lat - g.from.lat) * t;
    p.heading = (g.from.heading + r3dAngle(g.to.heading - g.from.heading) * t + 360) % 360;
    p.pitch = g.from.pitch + (g.to.pitch - g.from.pitch) * t;
    p.roll = g.from.roll + (g.to.roll - g.from.roll) * t;
    if (t < 1) busy = true; else R3D.glide = null;
  } else if (R3D.mode === "live" && R3D.fix) {
    const f = R3D.fix, [kx, ky] = r3dMetres(f.lat);
    const ahead = Math.min(R3D_AHEAD_S, (now - f.at) / 1000) * (Math.abs(f.speed) > 0.03 ? f.speed : 0);
    const tx = f.lon + (Math.sin(f.heading * R3D_DEG) * ahead) / kx, ty = f.lat + (Math.cos(f.heading * R3D_DEG) * ahead) / ky;
    const ex = (tx - p.lon) * kx, ey = (ty - p.lat) * ky, err = Math.hypot(ex, ey);
    if (err > R3D_JUMP_M) { p.lon = tx; p.lat = ty; }
    else {
      const k = 1 - Math.exp(-dt / R3D_EASE_S), cap = (0.9 + err * 0.6) * dt;
      let sx = ex * k, sy = ey * k;
      const m = Math.hypot(sx, sy);
      if (m > cap && m > 1e-9) { sx *= cap / m; sy *= cap / m; }
      p.lon += sx / kx;
      p.lat += sy / ky;
    }
    const turn = r3dAngle(f.heading - p.heading), e = 1 - Math.exp(-dt * 4);
    p.heading += turn * e;
    p.pitch += (f.pitch - p.pitch) * e;
    p.roll += (f.roll - p.roll) * e;
    busy = err > 0.01 || Math.abs(turn) > 0.3 || ((now - f.at) / 1000 < R3D_AHEAD_S && Math.abs(f.speed) > 0.03);
  }
  if (R3D.follow.on && r3dFollowFrame(dt)) busy = true;
  map.triggerRepaint();
  if (busy) R3D.raf = requestAnimationFrame(r3dTick);
}

// Follow on or off. On: fly behind the robot (and to one side), then stay with it; off: leave the camera where it is.
function robot3dFollow(on) {
  const map = state.map, f = R3D.follow;
  on = !!on && !!map && !!map.getLayer("robot3d") && !!R3D.pose;
  if (on === f.on) return syncFollowButton();
  f.on = on;
  syncFollowButton();
  if (!on) return;
  const p = R3D.pose;
  f.cam = p.heading;
  f.offset = R3D_FOLLOW_SIDE;
  map.easeTo({ center: r3dAhead(p), bearing: p.heading + f.offset, zoom: Math.max(map.getZoom(), 21.5), pitch: 62, duration: 900 });
  setTimeout(r3dKick, 950);
}

function syncFollowButton() {
  const b = typeof $ === "function" && $("#follow3d");
  if (!b) return;
  b.classList.toggle("on", R3D.follow.on);
  b.setAttribute("aria-pressed", String(R3D.follow.on));
}

const r3dAhead = (p) => {
  const [kx, ky] = r3dMetres(p.lat);
  return [p.lon + (Math.sin(p.heading * R3D_DEG) * R3D_FOLLOW_LEAD) / kx, p.lat + (Math.cos(p.heading * R3D_DEG) * R3D_FOLLOW_LEAD) / ky];
};

// One frame of following: turn toward the robot's heading (at most R3D_FOLLOW_TURN°/s) and keep it in view. True
// while the camera is still turning.
function r3dFollowFrame(dt) {
  const map = state.map, f = R3D.follow, p = R3D.pose;
  if (!map || !p || map.isEasing() || f.hands || performance.now() - f.wheelAt < 400) return true;
  if (f.cam === null) f.cam = p.heading;
  const d = r3dAngle(p.heading - f.cam), cap = R3D_FOLLOW_TURN * dt;
  f.cam += Math.max(-cap, Math.min(cap, d * (1 - Math.exp(-dt * 2.5))));
  map.jumpTo(f.turning ? { center: r3dAhead(p) } : { center: r3dAhead(p), bearing: f.cam + f.offset });
  return Math.abs(d) > 0.5;
}

