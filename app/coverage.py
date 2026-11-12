"""Coverage and gap detection for a run (spec §12), in the robot's local metres (x = west, y = north).

Rule that matters most: coverage is only *known* where RTK was fixed. Track segments with float or single-point
positioning carry metre-scale error, so the cells they sweep are "unknown", never covered and never gaps.
"""
import math
from collections import deque

CELL_M = 0.25
FIXED = 4
MAX_GAP_S = 5      # consecutive samples further apart than this are not joined
MAX_STEP_M = 3     # nor are pose jumps larger than this
# Width of each head (snow blower, mower, mower pro). Every deck of a type is the same, so it is fixed, never a
# setting; the snow blower's is still to be measured.
DEFAULT_DECK_M = {1: 0.6, 3: 0.53, 5: 0.53}
VERSION = 3        # a cached analysis of an older version is worked out again (2: earlier runs of the plan session;
                   # 3: the deck width setting is gone)


def polygon_cells(poly: list[tuple[float, float]], cell: float = CELL_M) -> set[tuple[int, int]]:
    """Cells whose centres lie inside the polygon (even-odd scanline)."""
    if len(poly) < 3:
        return set()
    ys = [p[1] for p in poly]
    out = set()
    n = len(poly)
    for j in range(math.floor(min(ys) / cell), math.ceil(max(ys) / cell) + 1):
        yc = (j + 0.5) * cell
        xs = []
        for i in range(n):
            (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
            if (y1 <= yc < y2) or (y2 <= yc < y1):
                xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2]):
            for i in range(math.ceil(a / cell - 0.5), math.floor(b / cell - 0.5) + 1):
                out.add((i, j))
    return out


def mark_segment(target: set, p0: tuple[float, float], p1: tuple[float, float], radius: float,
                 cell: float = CELL_M) -> None:
    """Add every cell whose centre is within `radius` of the segment p0-p1."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    for i in range(math.floor((min(x0, x1) - radius) / cell), math.ceil((max(x0, x1) + radius) / cell) + 1):
        cx = (i + 0.5) * cell
        for j in range(math.floor((min(y0, y1) - radius) / cell), math.ceil((max(y0, y1) + radius) / cell) + 1):
            cy = (j + 0.5) * cell
            t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((cx - x0) * dx + (cy - y0) * dy) / length2))
            px, py = x0 + t * dx - cx, y0 + t * dy - cy
            if px * px + py * py <= radius * radius:
                target.add((i, j))


def _components(cells: set) -> list[list[tuple[int, int]]]:
    seen, out = set(), []
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        comp, queue = [], deque([start])
        while queue:
            c = queue.popleft()
            comp.append(c)
            for n in ((c[0] + 1, c[1]), (c[0] - 1, c[1]), (c[0], c[1] + 1), (c[0], c[1] - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    queue.append(n)
        out.append(comp)
    return out


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _boundary_cells(area: set) -> set:
    return {c for c in area if any(n not in area for n in ((c[0] + 1, c[1]), (c[0] - 1, c[1]),
                                                           (c[0], c[1] + 1), (c[0], c[1] - 1)))}


def sweep(points: list[dict], radius: float, cell: float = CELL_M) -> tuple[set, set]:
    """The cells a track swept: (with an RTK fix, without one)."""
    from .runs import pose_order
    points = pose_order(points)         # the robot's own order, one row per pose (a burst after a gap can arrive mixed)
    covered, unknown = set(), set()
    for a, b in zip(points, points[1:]):
        if None in (a.get("x"), a.get("y"), b.get("x"), b.get("y")):
            continue
        both = a.get("odom_ts") is not None and b.get("odom_ts") is not None
        gap = b["odom_ts"] - a["odom_ts"] if both else b["ts"] - a["ts"]
        if gap > MAX_GAP_S or math.hypot(b["x"] - a["x"], b["y"] - a["y"]) > MAX_STEP_M:
            continue
        fixed = a.get("rtk_status") == FIXED and b.get("rtk_status") == FIXED
        mark_segment(covered if fixed else unknown, (a["x"], a["y"]), (b["x"], b["y"]), radius, cell)
    return covered, unknown - covered


def analyze(points: list[dict], areas: list[dict], nogos: list[list[tuple[float, float]]], deck_m: float,
            cell: float = CELL_M, min_gap_m2: float = 1.0, before: list[list[dict]] | None = None) -> dict:
    """points: telemetry rows with ts, x, y, rtk_status. areas: [{id, name, poly}] in local metres. before: the
    tracks of earlier runs of the same plan session (a resume that was not joined onto them, e.g. after an overnight
    pause): what they mowed with a fix is done, so it is neither a gap nor unknown here, and it is left out of this
    run's coverage share (covered_before_m2 says how much). Run 7 (2026-10-18) showed run 6's 10 % as gaps."""
    radius = deck_m / 2
    covered, unknown = sweep(points, radius, cell)
    done_before = set()
    for track in before or []:
        done_before |= sweep(track, radius, cell)[0]
    done_before -= covered
    unknown -= done_before

    blocked = set()
    for poly in nogos:
        blocked |= polygon_cells(poly, cell)

    cell_m2 = cell * cell
    results = []
    for area in areas:
        eligible = polygon_cells(area["poly"], cell) - blocked
        if not eligible:
            continue
        cov, unk = eligible & covered, eligible & unknown
        if len(cov | unk) < 0.10 * len(eligible):
            continue  # this run did not work this area
        gap_cells = eligible - covered - unknown - done_before
        edge = _boundary_cells(eligible)
        unknown_halo = {(c[0] + di, c[1] + dj) for c in unk for di in (-2, -1, 0, 1, 2) for dj in (-2, -1, 0, 1, 2)}
        gaps = []
        for comp in _components(gap_cells):
            m2 = len(comp) * cell_m2
            if m2 < min_gap_m2:
                continue
            on_edge = sum(1 for c in comp if c in edge) / len(comp)
            near_unknown = any(c in unknown_halo for c in comp)
            cause = ("next to RTK loss" if near_unknown else "boundary margin" if on_edge > 0.3 else "missed")
            corners = [((i + dx) * cell, (j + dy) * cell) for i, j in comp for dx in (0, 1) for dy in (0, 1)]
            cx = sum((i + 0.5) * cell for i, _ in comp) / len(comp)
            cy = sum((j + 0.5) * cell for _, j in comp) / len(comp)
            gaps.append({"area_m2": round(m2, 2), "cause": cause, "centroid": [round(cx, 2), round(cy, 2)],
                         "hull": [[round(x, 3), round(y, 3)] for x, y in _hull(corners)]})
        gaps.sort(key=lambda g: -g["area_m2"])
        known = len(cov) + len(gap_cells)
        results.append({
            "id": area["id"], "name": area["name"],
            "eligible_m2": round(len(eligible) * cell_m2, 1),
            "covered_m2": round(len(cov) * cell_m2, 1),
            "unknown_m2": round(len(unk) * cell_m2, 1),
            "covered_before_m2": round(len(eligible & done_before) * cell_m2, 1),
            "gap_m2": round(sum(g["area_m2"] for g in gaps), 1),
            "uncovered_m2": round(len(gap_cells) * cell_m2, 1),  # every known-but-unmowed cell, incl. slivers < min gap
            "coverage_pct_of_known": round(100 * len(cov) / known, 1) if known else None,
            "gaps": gaps,
        })
    return {"v": VERSION, "cell_m": cell, "deck_m": deck_m, "min_gap_m2": min_gap_m2, "areas": results,
            "covered_m2": round(len(covered) * cell_m2, 1), "unknown_m2": round(len(unknown) * cell_m2, 1),
            "covered_before_m2": round(len(done_before) * cell_m2, 1)}


SIGNAL_METRICS = {
    "halow": {"field": "halow", "label": "HaLow signal", "unit": "dBm", "lo": -95, "hi": -45},
    "rtk": {"field": "rtk_status", "label": "RTK fixed", "unit": "%", "lo": 0, "hi": 100},
    "sats": {"field": "sats", "label": "Satellites", "unit": "", "lo": 10, "hi": 30},
    # One network's signal (the robot's 0-100 scale) from the Wi-Fi survey: a scan every 30 s, so bigger cells.
    "wifi": {"field": "signal", "label": "Wi-Fi signal", "unit": "%", "lo": 0, "hi": 80, "cell_m": 4.0, "min_samples": 2},
}


def signal_grid(points: list[dict], metric: str, cell: float | None = None, min_samples: int | None = None) -> list[dict]:
    """Average of a signal per grid cell across runs: HaLow dBm, share of samples with an RTK fix, satellites, or
    one Wi-Fi network's signal."""
    m = SIGNAL_METRICS[metric]
    field = m["field"]
    cell = cell or m.get("cell_m", 2.0)
    min_samples = min_samples or m.get("min_samples", 5)
    cells: dict[tuple[int, int], list] = {}
    for p in points:
        x, y, v = p.get("x"), p.get("y"), p.get(field)
        if x is None or y is None or v is None:
            continue
        if metric == "rtk":
            v = 100.0 if v == FIXED else 0.0
        c = cells.setdefault((math.floor(x / cell), math.floor(y / cell)), [0, 0.0])
        c[0] += 1
        c[1] += v
    return [{"i": i, "j": j, "cell_m": cell, "n": n, "value": round(total / n, 1)}
            for (i, j), (n, total) in cells.items() if n >= min_samples]
