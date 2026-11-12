"""DEMO=1: a made-up Yarbo in a made-up yard, for screenshots, for trying the panel without a robot, and for tests.

`DemoTransport` is the made-up robot's side: it answers the read requests the recorder makes and publishes what the
robot does on its own (heart_beat, and DeviceMSG as when the phone app is open), in the shapes the recorder parses.
`DemoCloud` stands in for `CloudTransport` (app/cloud.py), so the link (app/link.py) works the same: it passes reads
to the demo robot, refuses anything outside READ_ONLY_COMMANDS and takes no commands. Neither opens a socket.

The yard: three lawns, two no-go zones (a flower bed and a tree), a no-vision zone, two pathways and a dock, laid
out on the open lawn of Delta Park (East), a public city park in Portland, Oregon, just west of its softball fields
(grass bounded by trees, no houses), so the bundled keyless city imagery shows real grass under the shapes. The
robot frame is the SDK's: metres from REF, x = WEST, y = NORTH.

The robot mows plans for real: an edge lap, then back-and-forth stripes ~0.45 m apart around the no-go zones, at
0.5 m/s (DEMO_SPEED speeds it up), battery draining, RTK fixed except for a float/single patch next to the tree,
progress through get_plan_feedback, then home to the dock to charge, and after a while it goes out again.
"""
import base64
import bisect
import json
import logging
import math
import queue
import random
import threading
import time
import zlib
from collections.abc import Callable

from . import geo
from .commands import check_read_only

log = logging.getLogger(__name__)

DEMO_SN = "YB0000DEMO0001"
# Delta Park (East), Portland, Oregon: the lawn west of the softball fields, between the trees and the I-5 trees.
REF = (45.6013, -122.6818)
REF_HEIGHT = 9.0              # metres; the park is on the Columbia floodplain
SPEED = 0.5                   # m/s, the robot's default plan speed
SPACING = 0.45                # m between stripes (the Mower Pro deck is 0.53 m)
EDGE_INSET = 0.25             # the edge lap runs this far inside the boundary
STRIPE_INSET = 0.5            # stripes stop this far from it (the edge lap covers the rest)
NOGO_BUFFER = 0.35            # stripes stop this far from a no-go zone
DETOUR_BUFFER = 0.6           # and drive round it this far out
TICK_S = 0.25                 # the transport's loop
DEVICE_EVERY_S = 1.0          # DeviceMSG rate (the phone app's rate is 1-2 Hz)
HEARTBEAT_EVERY_S = 1.5
DOCK_WAIT_S = 1800            # charging on the dock before the next mow (sim seconds)

# id, name, outline (x west, y north), stripe angle (degrees from +x)
AREAS = [
    (11, "Front lawn", [(26.0, 4.0), (20.0, 16.5), (-2.5, 24.0), (-8.5, 18.0), (-14.0, 8.0), (-12.0, 1.0), (8.0, -1.0)], 0),
    (12, "Back lawn", [(4.0, -8.5), (-15.0, -5.5), (-27.5, -10.0), (-37.5, -18.0), (-40.0, -28.5), (-35.0, -35.0),
                       (-24.0, -32.5), (-8.0, -33.0), (-1.0, -29.0), (3.0, -19.0)], 30),
    (13, "Side strip", [(63.0, 28.5), (47.0, 32.0), (37.0, 31.5), (36.5, 27.0), (62.0, 22.5)], -10),
]
NOGOS = [
    (21, "Flower bed", 11, [(-2.5, 17.5), (-5.5, 18.6), (-7.5, 16.6), (-6.0, 14.3), (-3.0, 14.8)]),
    (22, "Tree", 12, [(-28.5, -24.5), (-31.0, -27.0), (-28.5, -30.0), (-25.5, -27.0)]),
]
NOVISION = [(31, "Under the trees", [(-6.5, -26.0), (-13.5, -27.5), (-11.5, -32.0), (-5.5, -30.5)])]
PATHWAYS = [(41, "Front to back", 11, 12, [(0.0, -0.5), (0.0, -4.5), (0.5, -8.0)]),
            (42, "To the side strip", 11, 13, [(24.0, 8.0), (33.0, 17.0), (38.0, 27.5)])]
DOCK = (2.0, 21.0)
DOCK_PHI = -math.pi / 2       # facing south, into the front lawn
PLANS = [{"id": 1, "name": "Mow All", "areaIds": [11, 12, 13], "enable_self_order": True},
         {"id": 2, "name": "Front lawn", "areaIds": [11], "enable_self_order": True}]
TREE = (-27.0, -23.0)         # where RTK suffers: under the tree's canopy, on its north side
FIRMWARE = {"BodyVersionMsg": {"body_fw_version": "3.2.35"},
            "HeadAndVersionCheck": {"head_firmware_version": "1.4.10"},
            "chassis_version_msg": {"firmware_version": "2.1.7"}, "dc_version": "1.6.3", "ble_version": "1.0.9"}


# -- geometry (local metres) ------------------------------------------------------------------------------------
def inside(poly, x, y) -> bool:
    hit, n = False, len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def area_m2(poly) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))) / 2


def offset(poly, d: float):
    """The outline moved d metres inwards (negative: outwards), corner by corner."""
    ccw = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1])) > 0
    n, out = len(poly), []
    for i in range(n):
        (ax, ay), (bx, by), (cx, cy) = poly[i - 1], poly[i], poly[(i + 1) % n]
        lines = []
        for (px, py), (qx, qy) in (((ax, ay), (bx, by)), ((bx, by), (cx, cy))):
            dx, dy = qx - px, qy - py
            L = math.hypot(dx, dy) or 1.0
            nx, ny = (-dy / L, dx / L) if ccw else (dy / L, -dx / L)       # the inward normal
            lines.append((px + nx * d, py + ny * d, dx, dy))
        (p1x, p1y, d1x, d1y), (p2x, p2y, d2x, d2y) = lines
        den = d1x * d2y - d1y * d2x
        if abs(den) < 1e-9:
            out.append((p2x, p2y))
        else:
            t = ((p2x - p1x) * d2y - (p2y - p1y) * d2x) / den
            out.append((p1x + t * d1x, p1y + t * d1y))
    return out


def _crossings(poly, v):
    """Where the horizontal line y = v crosses the outline, sorted."""
    xs, n = [], len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 <= v < y2) or (y2 <= v < y1):
            xs.append(x1 + (v - y1) * (x2 - x1) / (y2 - y1))
    return sorted(xs)


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0)


def crosses(poly, a, b) -> bool:
    n = len(poly)
    if any(_segments_cross(a, b, poly[i], poly[(i + 1) % n]) for i in range(n)):
        return True
    return inside(poly, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def detour(a, b, zones) -> list:
    """Corners to drive through so a -> b keeps clear of the no-go zones (convex shapes): round the nearer side."""
    for z, ring in zones:
        if not crosses(z, a, b):
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1.0
        sides = {1: [], -1: []}
        for p in ring:
            side = 1 if dx * (p[1] - a[1]) - dy * (p[0] - a[0]) > 0 else -1
            sides[side].append(p)

        def length(chain):
            pts = [a, *sorted(chain, key=lambda p: ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2), b]
            return sum(math.dist(p, q) for p, q in zip(pts, pts[1:])), pts[1:-1]
        options = [length(c) for c in sides.values() if c]
        return min(options)[1] if options else []
    return []


# -- the yard as the robot reports it -----------------------------------------------------------------------------
def _ref() -> dict:
    return {"latitude": REF[0], "longitude": REF[1]}


def _range(pts, closed=True) -> list[dict]:
    out = []
    for i, (x, y) in enumerate(pts):
        nx, ny = pts[(i + 1) % len(pts)] if closed or i + 1 < len(pts) else pts[i - 1]
        phi = math.atan2(ny - y, nx - x) if closed or i + 1 < len(pts) else math.atan2(y - ny, x - nx)
        out.append({"x": x, "y": y, "phi": round(phi, 4)})
    return out


def area_record(aid: int) -> dict:
    _, name, pts, _ = next(a for a in AREAS if a[0] == aid)
    return {"id": aid, "name": name, "ref": _ref(), "range": _range(pts), "algorithm_type": 0, "labels": [],
            "leaf_piles": [], "push_snow_dir": 9999, "slope_degrees": 9999, "snowPiles": [], "trimming_edges": [],
            "humps": [], "enable": True}


def yard_map() -> dict:
    """What get_map returns (decoded)."""
    return {
        "areas": [area_record(a[0]) for a in AREAS],
        "nogozones": [{"id": i, "name": n, "ref": _ref(), "enable": True, "area_id": a, "type": 0,
                       "trimming_edges": [], "range": _range(p)} for i, n, a, p in NOGOS],
        "novisionzones": [{"id": i, "name": n, "ref": _ref(), "enable": True, "area_id": -1, "by_pass_level": 1,
                           "range": _range(p)} for i, n, p in NOVISION],
        "pathways": [{"id": i, "name": n, "ref": _ref(), "start_id": s, "end_id": e, "snowPiles": [],
                      "range": _range(p, closed=False)} for i, n, s, e, p in PATHWAYS],
        "sidewalks": [], "deadends": [], "elec_fence": [],
        "allchargingData": [{"id": 1, "name": "Dock", "ref": _ref(), "hasChargingStation": True,
                             "chargingPoint": {"x": DOCK[0], "y": DOCK[1], "phi": DOCK_PHI},
                             "startPoint": {"x": DOCK[0], "y": DOCK[1] - 1.5, "phi": DOCK_PHI}}],
    }


def backup_map(bid: int, name: str, ts: float, auto: bool) -> dict:
    """get_map_buckup_from_id's answer: the same map with singular keys."""
    m = yard_map()
    return {"area": m["areas"], "nogozone": m["nogozones"], "novisionzone": m["novisionzones"],
            "pathway": m["pathways"], "sidewalk": [], "deadend": [], "elec_fence": [],
            "chargingPoints": m["allchargingData"], "chargingPoint": m["allchargingData"][0]["chargingPoint"],
            "id": bid, "name": name, "timestamp": int(ts), "is_auto_backup": auto, "ref": _ref()}


def area_params(aid: int) -> dict:
    """read_mower_area_params for one area (every key this firmware reports)."""
    angle = next((a[3] for a in AREAS if a[0] == aid), 0)
    clean = {"blade_height": 25, "blade_speed": 80, "land_push_pod_place": 50, "plan_speed": SPEED,
             "route_angle": angle % 180, "route_dis": 0.0, "turn_type": 1}
    return {"auto_step_angle": 90, "avoidance_level": 1, "buffer_dis": 0.0, "clean_times": 1, "clean_times_dis": 0.0001,
            "contract_dis": 0.0254, "current_blade_height": 25, "current_index": 0,
            "double_clean_params": {**clean, "route_angle": (angle + 90) % 180}, "edge_area_first": True,
            "edge_bypass_sw": True, "edge_circles": 1, "edge_direction_mode": 0, "en_trimmer": False,
            "enable_clean_times_dis": False, "enable_explore": False, "first_clean_params": clean, "gap": 0.3,
            "id": aid, "m1pro_sensor_switch": 139, "move2area_max_offset": 2.0, "move2area_offset_count": {},
            "move2area_offset_enable": True, "move2area_step_offset": 0.1, "mower_ngz_edge_sw": True,
            "mower_target_height_count": 5, "ngz_expand_dis": 0.102, "parallel_angle_max": 90,
            "parallel_angle_min": -90, "parallel_offset_mode": 2, "route_order": 0, "sensor_switch": 8,
            "side_enable": True}


def pathway_params(pid: int) -> dict:
    return {"blade_height": 0, "blade_speed": 80, "current_offset": 0.0, "en_blade": False, "en_trimmer": False,
            "id": pid, "land_push_pod_place": 50, "offset_enable": False, "offset_range": [-1.0, 1.0],
            "offset_step": 0.1, "pathway_obs_perception_sw": True, "plan_speed": SPEED, "sensor_switch": 11,
            "turn_type": 0}


def low_cfd_config() -> dict:
    return {"area_low_cfd_configs": {str(a[0]): {"area": round(area_m2(offset(a[2], 0.3)), 1),
                                                 "area_max_low_cfd_dis": 50.0, "path_max_low_cfd_dis": 10.0}
                                     for a in AREAS},
            "area_max_low_cfd_dis": 50.0, "path_max_low_cfd_dis": 10.0, "draw_path_max_low_cfd_dis": 5.0,
            "enable_advanced_fusion": True, "use_global_low_cfd_dis": True}


def ground_height(x: float, y: float) -> float:
    """A gentle swale: the yard's 3D view has something to show."""
    return REF_HEIGHT + 0.018 * y - 0.01 * x + 0.35 * math.sin(x / 11.0) * math.cos(y / 9.0)


# -- routes ------------------------------------------------------------------------------------------------------
MOW, EDGE, TRANSIT, RETURN, DOCKING = "mow", "edge", "transit", "return", "docking"
FLAGS = {MOW: (1, 0), EDGE: (1, 0), TRANSIT: (3, 0), RETURN: (0, 1), DOCKING: (0, 3)}   # (planning, recharging)


def _rot(angle):
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (lambda x, y: (x * c + y * s, -x * s + y * c)), (lambda u, v: (u * c - v * s, u * s + v * c))


def stripes(poly, angle: float, nogos) -> list[tuple]:
    """Back-and-forth stripes over the outline, around the no-go zones: [(start, end), ...] in mowing order."""
    fwd, back = _rot(angle)
    P = [fwd(*p) for p in offset(poly, STRIPE_INSET)]
    N = [[fwd(*p) for p in offset(z, -NOGO_BUFFER)] for z in nogos]
    vs = [p[1] for p in P]
    v, k, out = min(vs) + SPACING / 2, 0, []
    while v < max(vs):
        xs = _crossings(P, v)
        ivs = [[a, b] for a, b in zip(xs[::2], xs[1::2])]
        for z in N:
            zx = _crossings(z, v)
            for za, zb in zip(zx[::2], zx[1::2]):
                cut = []
                for a, b in ivs:
                    if zb <= a or za >= b:
                        cut.append([a, b])
                    else:
                        if za > a:
                            cut.append([a, za])
                        if zb < b:
                            cut.append([zb, b])
                ivs = cut
        ivs = [iv for iv in ivs if iv[1] - iv[0] > 0.3]
        if k % 2:
            ivs = [[b, a] for a, b in reversed(ivs)]
        out += [(back(a, v), back(b, v)) for a, b in ivs]
        v += SPACING
        k += 1
    return out


class Route:
    """A plan's drive as one polyline with a mode and an area per vertex, measured along its length."""

    def __init__(self, plan: dict, angle_offset: float = 0.0):
        self.plan = plan
        self.pts: list[tuple[float, float]] = []
        self.modes: list[str] = []
        self.areas: list[int | None] = []
        self.zones = [(offset(p, -0.3), offset(p, -DETOUR_BUFFER)) for _, _, _, p in NOGOS]
        self._add(DOCK, TRANSIT, None)
        self._go((DOCK[0], DOCK[1] - 1.5), TRANSIT, None)              # back off the dock
        here = 11
        self.area_end: dict[int, float] = {}
        for aid in plan["areaIds"]:
            _, _, poly, angle = next(a for a in AREAS if a[0] == aid)
            lanes = stripes(poly, angle + angle_offset, [p for _, _, _, p in NOGOS])
            edge = offset(poly, EDGE_INSET)
            start = min(range(len(edge)), key=lambda i: math.dist(edge[i], lanes[0][0] if lanes else edge[0]))
            for p in self._between(here, aid, edge[start]):
                self._go(p, TRANSIT, None)
            for i in range(len(edge) + 1):                                 # the edge lap first
                self._go(edge[(start + i) % len(edge)], EDGE, aid)
            for a, b in lanes:
                self._go(a, MOW, aid)
                self._go(b, MOW, aid)
            here = aid
            self.area_end[aid] = self.length()
        for p in self._between(here, 11, (DOCK[0], DOCK[1] - 1.5)):
            self._go(p, RETURN, None)
        self._go((DOCK[0], DOCK[1] - 1.5), RETURN, None)
        self._go(DOCK, DOCKING, None)
        self._measure()

    @staticmethod
    def _path(pid):
        return list(next(p for p in PATHWAYS if p[0] == pid)[4])

    def _between(self, a: int, b: int, target) -> list:
        """Waypoints from area a to the point `target` in area b, along the pathways."""
        legs = {(11, 12): self._path(41), (12, 11): self._path(41)[::-1], (11, 13): self._path(42),
                (13, 11): self._path(42)[::-1]}
        if a == b:
            way = []
        elif (a, b) in legs:
            way = legs[(a, b)]
        else:                                                         # 12 <-> 13 through the front lawn
            way = legs[(a, 11)] + legs[(11, b)]
        return [*way, target]

    def _add(self, p, mode, area):
        self.pts.append((round(p[0], 3), round(p[1], 3)))
        self.modes.append(mode)
        self.areas.append(area)

    def _go(self, p, mode, area):
        if self.pts:
            for q in detour(self.pts[-1], p, self.zones):
                self._add(q, mode, area)
        self._add(p, mode, area)

    def length(self) -> float:
        return sum(math.dist(p, q) for p, q in zip(self.pts, self.pts[1:]))

    def _measure(self):
        self.cum = [0.0]
        self.cum_mow = [0.0]
        for i in range(1, len(self.pts)):
            d = math.dist(self.pts[i - 1], self.pts[i])
            self.cum.append(self.cum[-1] + d)
            self.cum_mow.append(self.cum_mow[-1] + (d if self.modes[i] in (MOW, EDGE) else 0.0))
        self.total = self.cum[-1]
        self.total_mow = self.cum_mow[-1]

    def at(self, s: float) -> dict:
        """Pose and state `s` metres along the route."""
        s = max(0.0, min(self.total, s))
        i = max(1, min(len(self.pts) - 1, bisect.bisect_right(self.cum, s)))
        (x0, y0), (x1, y1) = self.pts[i - 1], self.pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        f = (s - self.cum[i - 1]) / seg if seg > 0 else 1.0
        x, y = x0 + f * (x1 - x0), y0 + f * (y1 - y0)
        phi = math.atan2(y1 - y0, x1 - x0) if seg > 0 else DOCK_PHI
        mow_done = self.cum_mow[i - 1] + (f * seg if self.modes[i] in (MOW, EDGE) else 0.0)
        return {"x": x, "y": y, "phi": phi, "mode": self.modes[i], "area": self.areas[i], "mow_done": mow_done}

    def s_for_progress(self, frac: float) -> float:
        """The distance along the route where `frac` of the mowing is done."""
        target = frac * self.total_mow
        i = max(1, bisect.bisect_left(self.cum_mow, target))
        return self.cum[i]

    def planned_path(self, aid: int) -> list[dict]:
        idx = [i for i, a in enumerate(self.areas) if a == aid and self.modes[i] == MOW]
        return [{"x": round(self.pts[i][0], 2), "y": round(self.pts[i][1], 2), "phi": 0.0} for i in idx]


def total_m2(area_ids) -> float:
    return round(sum(area_m2(offset(a[2], 0.3)) for a in AREAS if a[0] in area_ids), 1)


# -- the robot ---------------------------------------------------------------------------------------------------
class DemoRobot:
    """The robot's state, advanced in (simulated) time. `now` is wall-clock seconds; `speed` scales how fast the
    simulation runs against it."""

    def __init__(self, now: float, plan_id: int = 1, progress: float = 0.4, session_start: float | None = None,
                 speed: float = 1.0, seed: int = 1):
        self.rng = random.Random(seed)
        self.speed = speed
        self.battery = 100.0
        self.odo = 18420.0
        self.work_s = 0.0
        self.route: Route | None = None
        self.plan: dict | None = None
        self.phase = "docked"
        self.dock_until = now
        self.last = now
        self.s = 0.0
        self.session_start = 0
        self.runs = 0
        self.pose = {"x": DOCK[0], "y": DOCK[1], "phi": DOCK_PHI, "mode": None, "area": None, "mow_done": 0.0}
        if plan_id:
            self.start_plan(plan_id, now, progress, session_start)

    def start_plan(self, plan_id: int, now: float, progress: float = 0.0, session_start: float | None = None) -> None:
        self.plan = next(p for p in PLANS if p["id"] == plan_id)
        self.route = Route(self.plan, angle_offset=90 if self.runs % 2 else 0)
        self.runs += 1
        self.s = self.route.s_for_progress(progress) if progress > 0 else 0.0
        self.session_start = int(session_start if session_start is not None else now - self.s / SPEED)
        self.battery = max(20.0, 100.0 - 0.42 * (self.s / SPEED) / 60)
        self.work_s = self.s / SPEED
        self.phase = "route"
        self.pose = self.route.at(self.s)

    def advance(self, now: float) -> None:
        dt = max(0.0, now - self.last) * self.speed
        self.last = now
        if self.phase == "route":
            self.s += SPEED * dt
            self.odo += SPEED * dt
            self.work_s += dt
            self.battery = max(5.0, self.battery - 0.42 * dt / 60)
            self.pose = self.route.at(self.s)
            if self.s >= self.route.total:
                self.phase, self.dock_until = "docked", now + DOCK_WAIT_S / self.speed
                self.pose = {**self.pose, "x": DOCK[0], "y": DOCK[1], "phi": DOCK_PHI, "mode": None, "area": None}
        else:
            self.battery = min(100.0, self.battery + 1.0 * dt / 60)
            if now >= self.dock_until and self.battery >= 95:
                self.start_plan(1, now)

    # what the robot reports -------------------------------------------------------------------------------------
    def rtk(self, x: float, y: float) -> int:
        d = math.dist((x, y), TREE)
        return 1 if d < 1.2 else 5 if d < 2.3 else 4

    def snapshot(self, now: float, full: bool = True) -> dict:
        """get_device_msg's data (full) or a DeviceMSG (the changing parts)."""
        p, r = self.pose, self.rng
        working = self.phase == "route"
        mode = p.get("mode") if working else None
        planning, recharging = FLAGS.get(mode, (0, 4))
        mowing = mode in (MOW, EDGE)
        status = self.rtk(p["x"], p["y"]) if working else 1
        sats = 27 + round(3 * math.sin(now / 900)) - (7 if status != 4 and working else 0) + r.randint(-1, 1)
        lon, lat = geo.to_lonlat(REF, p["x"], p["y"])
        speed = SPEED if working and mode != DOCKING else 0.0
        frac = min(1.0, self.work_s / 9000)
        halow = -44 - 0.3 * math.dist((p["x"], p["y"]), DOCK) - r.randint(0, 3)
        snap = {
            "CombinedOdom": {"x": round(p["x"], 3), "y": round(p["y"], 3), "phi": round(p["phi"], 4),
                             "timestamp": int(now * 1000)},
            "combined_odom_confidence": 0.98 if status == 4 else 0.62 if status == 5 else 0.35,
            "RTKMSG": {"status": status, "sat_num": sats, "heading_dop": round(1.0 + 0.5 * r.random(), 2),
                       "lan": round(lat, 8), "lon": round(lon, 8), "heading_obs": sats - 3,
                       "heading_multi": sats - 6, "hgt": round(ground_height(p["x"], p["y"]) + 0.02 * r.random(), 3)},
            "rtcm_age": round(0.8 + 0.6 * r.random(), 1) if working else -1,
            "BatteryMSG": {"capacity": round(self.battery), "health": 98,
                           "voltage": round(25600 + 3600 * self.battery / 100),
                           "current": -8600 - r.randint(0, 900) if working else (11800 if self.battery < 99.5 else 300),
                           **{f"temperature{i}": round(24 + 7 * frac + 0.4 * i, 1) for i in range(1, 7)}},
            "StateMSG": {"charging_status": 0 if working else 2, "working_state": 1, "on_going_planning": planning,
                         "on_going_recharging": recharging if not working or mode in (RETURN, DOCKING) else 0,
                         "planning_paused": 0, "car_controller": working, "error_code": 0, "stuck": 0,
                         "obstacle": 0, "pedestrian": 0, "schedule_id": -1,
                         "plan_msg": self.plan["name"] if working and self.plan else ""},
            "WheelSpeedMSG": {"left": round(speed + (0.02 * r.random() if speed else 0), 3),
                              "right": round(speed + (0.02 * r.random() if speed else 0), 3),
                              "dist_left": round(self.odo, 2), "dist_right": round(self.odo + 0.7, 2)},
            "EletricMSG": {"lwheel_current": round(1.4 + r.random(), 2) if speed else 0.0,
                           "rwheel_current": round(1.4 + r.random(), 2) if speed else 0.0},
            "RunningStatusMSG": {"gyro_pitch": round(2.2 * math.sin(p["y"] / 7) + 0.3 * r.random(), 2),
                                 "gyro_roll": round(1.6 * math.sin(p["x"] / 6) + 0.3 * r.random(), 2),
                                 "rain_sensor_data": 3900 + r.randint(0, 60), "chute_angle": 0},
            "halow_status": {"strength": round(halow)},
            "HeadMsg": {"head_type": 5},
            "mower_head_info03": {"left_blade_motor_rpm": 3100 + r.randint(-60, 60) if mowing else 0,
                                  "left_blade_motor_current": round(2.4 + 1.6 * r.random(), 2) if mowing else 0.0,
                                  "left_blade_motor_temp": round(38 + 12 * frac, 1)},
            "mower_head_info04": {"right_blade_motor_rpm": 3100 + r.randint(-60, 60) if mowing else 0,
                                  "right_blade_motor_current": round(2.4 + 1.6 * r.random(), 2) if mowing else 0.0,
                                  "right_blade_motor_temp": round(37 + 12 * frac, 1)},
            "system_info": {"cpu": {"Temperature": f"{48 + 6 * frac:.3f} °C"}},
        }
        if full:
            snap.update(FIRMWARE)
        return snap

    def plan_feedback(self, now: float) -> dict:
        """get_plan_feedback's reply."""
        if self.phase != "route" or self.plan is None:
            return {"state": -1, "msg": "no plan working", "data": None}
        R, p = self.route, self.pose
        total = total_m2(self.plan["areaIds"])
        done = total * min(1.0, p["mow_done"] / R.total_mow) if R.total_mow else 0.0
        finished = [aid for aid in self.plan["areaIds"] if self.s >= R.area_end.get(aid, 1e12)]
        current = p.get("area") or next((a for a in self.plan["areaIds"] if a not in finished), None)
        left = max(0.0, (R.total - self.s) / SPEED)
        elapsed = max(0, int(now) - self.session_start)
        progress = []
        if current:
            path = R.planned_path(current)
            progress = [{"id": current, "clean_index": sum(1 for i, a in enumerate(R.areas)
                                                            if a == current and R.modes[i] == MOW and R.cum[i] <= self.s),
                         "path": path}]
        return {"state": 0, "msg": "", "data": {
            "planId": self.plan["id"], "startTime": self.session_start, "areaIds": list(self.plan["areaIds"]),
            "cleanAreaId": current, "finishIds": [{"id": a, "clean_times": 1, "move_type": 1} for a in finished],
            "finishCleanArea": round(done, 1), "actualCleanArea": round(done * 1.02, 1), "totalCleanArea": total,
            "leftTime": round(left), "duration": elapsed, "totalTime": round(elapsed + left), "state": 1,
            "cleanPathProgress": progress}}

    def wifi(self) -> dict:
        d = math.dist((self.pose["x"], self.pose["y"]), DOCK)
        nets = [("DemoNet", max(5, 78 - 1.1 * d), "WPA2", True), ("DemoNet-IoT", max(3, 64 - 1.0 * d), "WPA2", False),
                ("Park-Guest", 18 + 8 * math.sin(self.pose["x"] / 9), "", False)]
        return {"wifi_list": [{"wifi_name": n, "signal": str(round(s)), "security": sec, "saved": saved}
                              for n, s, sec, saved in sorted(nets, key=lambda n: -n[1])]}

    def preview(self, payload: dict) -> dict:
        """preview_snowbot_area_path: the stripes for an outline, with that area's saved angle."""
        pts = [(p["x"], p["y"]) for p in (payload or {}).get("range") or [] if "x" in p and "y" in p]
        if len(pts) < 3:
            return {"state": -1, "msg": "invalid area", "data": None}
        angle = next((a[3] for a in AREAS if a[0] == (payload or {}).get("id")), 0)
        lanes = stripes(pts, angle, [p for _, _, _, p in NOGOS])
        path = [{"x": round(x, 3), "y": round(y, 3), "phi": 0.0} for a, b in lanes for x, y in (a, b)]
        length = sum(math.dist(a, b) for a, b in zip(lanes, lanes[1:])) if False else \
            sum(math.dist((p["x"], p["y"]), (q["x"], q["y"])) for p, q in zip(path, path[1:]))
        return {"state": 0, "msg": "", "data": {"path": path, "total_time": round(length / SPEED)}}


# -- the transport ------------------------------------------------------------------------------------------------
class DemoTransport:
    """The made-up robot's side, for DemoCloud. A thread advances the robot, publishes heart_beat and DeviceMSG,
    and answers the read requests queued by request(). Nothing here opens a socket."""

    def __init__(self, sn: str, on_message: Callable[[str, object, float], None], robot: DemoRobot,
                 backups: list[dict] | None = None):
        self.host, self.port, self.sn = "demo", 0, sn
        self.robot = robot
        self._on_message = on_message
        self.connected = False
        self.connected_since = 0.0
        self.last_message_at = 0.0
        self.published = 0
        self.received = 0
        self.last_error = ""
        self._requests: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device_at = self._beat_at = 0.0
        now = time.time()
        self.backups = backups if backups is not None else [
            {"id": 7, "name": "Auto Saved", "timestamp": int(now - 2 * 86400), "is_auto_backup": True},
            {"id": 3, "name": "Manually Saved 1", "timestamp": int(now - 9 * 86400), "is_auto_backup": False}]

    def start(self) -> None:
        self.connected, self.connected_since = True, time.time()
        self._thread = threading.Thread(target=self._loop, name="demo-robot", daemon=True)
        self._thread.start()
        log.info("demo robot %s started (synthetic yard, no network)", self.sn)

    def stop(self) -> None:
        self._stop.set()
        self.connected = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def request(self, cmd: str, payload: dict | None = None) -> None:
        check_read_only(cmd)
        if not self.connected:
            raise ConnectionError("the demo robot is not running")
        self._requests.put((cmd, payload or {}))
        self.published += 1

    # -- the robot's side ----------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(time.time())
            except Exception:
                log.exception("demo robot tick failed")
            self._stop.wait(TICK_S)

    def tick(self, now: float) -> None:
        """One step: advance, answer what was asked, publish what is due (tests call this directly)."""
        self.robot.advance(now)
        while True:
            try:
                cmd, payload = self._requests.get_nowait()
            except queue.Empty:
                break
            state, msg, data = self.answer(cmd, payload, now)
            self._send("data_feedback", {"topic": cmd, "state": state, "msg": msg, "data": data}, now)
        if now - self._beat_at >= HEARTBEAT_EVERY_S:
            self._beat_at = now
            self._send("heart_beat", {"working_state": 1}, now)
        if now - self._device_at >= DEVICE_EVERY_S:
            self._device_at = now
            self._send("DeviceMSG", self.robot.snapshot(now, full=False), now)

    def _send(self, name: str, data, now: float) -> None:
        self.received += 1
        self.last_message_at = now
        try:
            self._on_message(f"snowbot/{self.sn}/device/{name}", data, now)
        except Exception:
            log.exception("handler failed for demo %s", name)

    def answer(self, cmd: str, payload: dict, now: float) -> tuple[int, str, object]:
        r = self.robot
        if cmd == "get_device_msg":
            return 0, "", r.snapshot(now)
        if cmd == "get_map":
            return 0, "", base64.b64encode(zlib.compress(json.dumps(yard_map()).encode())).decode()
        if cmd == "read_gps_ref":
            return 0, "", {"ref": {"latitude": REF[0], "longitude": REF[1], "height": REF_HEIGHT}, "rtkFixType": 4}
        if cmd == "read_all_plan":
            return 0, "", {"data": [dict(p) for p in PLANS]}
        if cmd == "read_low_cfd_config":
            return 0, "", low_cfd_config()
        if cmd == "read_schedules":
            return 0, "", []
        if cmd == "get_plan_feedback":
            fb = r.plan_feedback(now)
            return fb["state"], fb["msg"], fb["data"]
        if cmd == "read_all_clean_area":
            return 0, "", {"data": [area_record(a[0]) for a in AREAS]}
        if cmd == "read_mower_area_params":
            aid = payload.get("id")
            if aid not in {a[0] for a in AREAS}:
                return -1, "area not found", None
            return 0, "", area_params(aid)
        if cmd == "read_mower_pathway_params":
            pid = payload.get("id")
            if pid not in {p[0] for p in PATHWAYS}:
                return -1, "pathway not found", None
            return 0, "", pathway_params(pid)
        if cmd == "read_deadend_params":
            return -1, "dead end not found", None
        if cmd == "read_tow_params":
            return 0, "", {"enable": False, "move_speed": 0.3}
        if cmd == "get_all_map_backup":
            return 0, "", json.dumps(self.backups)
        if cmd == "get_map_buckup_from_id":
            b = next((b for b in self.backups if b["id"] == payload.get("id")), None)
            if b is None:
                return -1, "backup not found", None
            return 0, "", backup_map(b["id"], b["name"], b["timestamp"], b["is_auto_backup"])
        if cmd == "get_wifi_list":
            return 0, "", r.wifi()
        if cmd == "get_connect_wifi_name":
            return 0, "", "DemoNet"
        if cmd == "preview_snowbot_area_path":
            p = r.preview(payload)
            return p["state"], p["msg"], p["data"]
        return -1, f"{cmd} is not simulated", None


class DemoCloud:
    """CloudTransport's interface for DEMO=1: reads go to the demo robot (`rec.transport`, a DemoTransport), commands
    are refused (`takes_commands`), and there is no account, session or network. The demo never signs in to Yarbo."""

    takes_commands = False
    _thread = None               # no worker thread (Ops.robot_changed looks for one)

    def __init__(self, rec):
        self.rec = rec
        self.sn = DEMO_SN
        self.username = None
        self.broker = "demo robot"
        self.error = ""
        self.published = 0
        self.connected_since = time.time()
        self.devices: list[dict] = []
        self.on_sn = None

    @property
    def connected(self) -> bool:
        t = getattr(self.rec, "transport", None)
        return bool(t and t.connected)

    @property
    def state(self) -> str:
        return "connected" if self.connected else "connecting"

    def publish(self, cmd: str, payload: dict) -> None:
        check_read_only(cmd)                       # the demo robot only answers reads
        t = getattr(self.rec, "transport", None)
        if not (t and t.connected):
            raise ConnectionError("the demo robot is not running")
        t.request(cmd, payload)
        self.published += 1

    def restore(self) -> bool:
        return False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def restart(self) -> None:
        pass

    def login(self, email: str, password: str) -> None:
        raise PermissionError("this is a demo instance (DEMO=1): it never signs in to the Yarbo cloud")

    def logout(self) -> None:
        pass

    def session_jwt(self) -> None:
        return None

    def refresh_session(self, stale_jwt: str | None = None) -> str:
        return "expired"

    def status(self) -> dict:
        return {"state": self.state, "username": None, "broker": self.broker, "error": "", "commands_sent": 0,
                "devices": [], "demo": True}
