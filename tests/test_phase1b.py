import math
import sqlite3

import pytest

from app import coverage, geo
from app.conditions import ConditionsConfig, evaluate, next_sunrise_ts, sunrise_ts
from app.tempest import TempestListener, dew_point_c, parse_obs_st

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def stripes(rtk_for=lambda x, y: 4, skip=lambda x, y: False, spacing=0.5, y0=0.25, x_step=0.5):
    """Boustrophedon track over the 10 m square at 1 Hz."""
    pts, ts, row = [], 0.0, 0
    y = y0
    while y < 10:
        xs = [0.25 + i * x_step for i in range(int(9.5 / x_step) + 1)]
        if row % 2:
            xs.reverse()
        for x in xs:
            if skip(x, y):
                ts += 10  # a time gap so the segment is not bridged
                continue
            ts += 1
            pts.append({"ts": ts, "x": x, "y": y, "rtk_status": rtk_for(x, y)})
        y += spacing
        row += 1
    return pts


# -- coverage --------------------------------------------------------------------------------------------------
def test_polygon_cells_area():
    cells = coverage.polygon_cells(SQUARE)
    assert len(cells) * coverage.CELL_M ** 2 == pytest.approx(100, rel=0.02)


def test_full_fixed_coverage_has_no_gaps():
    res = coverage.analyze(stripes(), [{"id": 1, "name": "sq", "poly": SQUARE}], [], deck_m=0.53)
    area = res["areas"][0]
    assert area["coverage_pct_of_known"] > 97
    assert area["gaps"] == []


def test_skipped_patch_is_a_gap():
    skip = lambda x, y: 4 <= x <= 6 and 4 <= y <= 6  # noqa: E731
    res = coverage.analyze(stripes(skip=skip), [{"id": 1, "name": "sq", "poly": SQUARE}], [], deck_m=0.53)
    gaps = res["areas"][0]["gaps"]
    assert len(gaps) == 1
    assert 2 <= gaps[0]["area_m2"] <= 6
    assert gaps[0]["cause"] == "missed"


def test_float_rtk_is_unknown_never_a_gap():
    rtk = lambda x, y: 5 if 4 <= x <= 6 and 4 <= y <= 6 else 4  # noqa: E731
    res = coverage.analyze(stripes(rtk_for=rtk), [{"id": 1, "name": "sq", "poly": SQUARE}], [], deck_m=0.53)
    area = res["areas"][0]
    assert area["gaps"] == []
    assert area["unknown_m2"] > 2


def test_gap_next_to_rtk_loss_is_labelled():
    rtk = lambda x, y: 5 if 4 <= x <= 5 and 4 <= y <= 6 else 4  # noqa: E731
    skip = lambda x, y: 5.5 <= x <= 7 and 4 <= y <= 6  # noqa: E731
    res = coverage.analyze(stripes(rtk_for=rtk, skip=skip), [{"id": 1, "name": "sq", "poly": SQUARE}], [], deck_m=0.53)
    assert any(g["cause"] == "next to RTK loss" for g in res["areas"][0]["gaps"])


def test_what_an_earlier_run_of_the_plan_mowed_is_neither_a_gap_nor_in_this_runs_share():
    """Run 7 (2026-10-18) resumed run 6's plan the next morning: run 6's 10 % showed as gaps next to RTK loss."""
    early = lambda x, y: y < 3                                     # noqa: E731  run 6 mowed the bottom...
    later = stripes(skip=early, rtk_for=lambda x, y: 5 if 3 <= y < 3.6 else 4)    # ...run 7 the rest, on float at first
    square = [{"id": 1, "name": "sq", "poly": SQUARE}]
    alone = coverage.analyze(later, square, [], deck_m=0.53)["areas"][0]
    assert alone["gap_m2"] > 20 and any(g["cause"] == "next to RTK loss" for g in alone["gaps"])
    res = coverage.analyze(later, square, [], deck_m=0.53, before=[stripes(skip=lambda x, y: not early(x, y))])
    area = res["areas"][0]
    assert area["gaps"] == [] and area["covered_before_m2"] > 20 and area["coverage_pct_of_known"] > 97
    assert area["covered_m2"] == alone["covered_m2"] and area["unknown_m2"] <= alone["unknown_m2"]
    assert res["v"] == coverage.VERSION and res["covered_before_m2"] > 20


def test_no_go_zones_are_not_gaps():
    nogo = [(3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0)]
    skip = lambda x, y: 3 <= x <= 7 and 3 <= y <= 7  # noqa: E731
    res = coverage.analyze(stripes(skip=skip), [{"id": 1, "name": "sq", "poly": SQUARE}], [nogo], deck_m=0.53)
    assert res["areas"][0]["gaps"] == []
    assert res["areas"][0]["eligible_m2"] == pytest.approx(84, abs=2)


def test_untouched_area_is_skipped():
    far = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]
    res = coverage.analyze(stripes(), [{"id": 1, "name": "sq", "poly": SQUARE}, {"id": 2, "name": "far", "poly": far}],
                           [], deck_m=0.53)
    assert [a["id"] for a in res["areas"]] == [1]


# -- geometry ---------------------------------------------------------------------------------------------------
def test_to_local_round_trip():
    ref = (40.0, -100.0)
    x, y = geo.to_local(ref, *geo.to_lonlat(ref, 12.3, -4.5))
    assert x == pytest.approx(12.3, abs=0.01) and y == pytest.approx(-4.5, abs=0.01)


# -- tempest ----------------------------------------------------------------------------------------------------
def test_parse_obs_st_and_dew_point():
    msg = {"type": "obs_st", "serial_number": "ST-1", "obs": [[1700000000, 0.1, 1.2, 2.0, 180, 3, 990.1, 18.0, 98,
                                                                0, 0, 0, 0.25, 1, 0, 0, 2.7, 1]]}
    row = parse_obs_st(msg)[0]
    assert row["temp_c"] == 18.0 and row["rain_mm"] == 0.25 and row["precip_type"] == 1
    assert row["dew_c"] == pytest.approx(17.7, abs=0.2)
    assert dew_point_c(20.0, 50.0) == pytest.approx(9.3, abs=0.2)


def test_listener_tracks_rain_start():
    seen = []
    t = TempestListener(0, on_obs=seen.append, on_event=lambda k, m: seen.append(k))
    t.handle({"type": "evt_precip", "evt": [1700000500]})
    assert t.last_rain_start == 1700000500 and "rain_started" in seen


def test_a_repeated_rain_broadcast_is_one_rain_start():
    seen = []
    t = TempestListener(0, on_obs=seen.append, on_event=lambda k, m: seen.append(k))
    for _ in range(2):                                   # the hub sends the same event twice (2026-10-09 15:01)
        t.handle({"type": "evt_precip", "evt": [1700000500]})
    t.handle({"type": "evt_precip", "evt": [1700009000]})   # a later shower is a new one
    assert seen.count("rain_started") == 2


# -- conditions -------------------------------------------------------------------------------------------------
NOW = 1_780_000_000.0  # a fixed instant; lat/lon below are a neutral test point


def rows(minutes, rain_at=None, rain_mm=0.0, temp=20.0, dew=10.0):
    out = []
    for m in range(minutes, 0, -1):
        ts = NOW - m * 60
        out.append({"ts": ts, "temp_c": temp, "dew_c": dew, "rh": 50, "solar": 400, "precip_type": 0,
                    "rain_mm": rain_mm if rain_at and rain_at[0] <= m <= rain_at[1] else 0.0})
    return out


def test_dry_day():
    res = evaluate(NOW, rows(600), None, None, 40.0, -100.0, ConditionsConfig())
    assert res["state"] == "Dry" and res["dry_by"] is None


def test_raining_now_is_wet():
    res = evaluate(NOW, rows(600, rain_at=(1, 5), rain_mm=0.2), None, None, None, None, ConditionsConfig())
    assert res["state"] == "Wet" and res["raining"] and res["dry_by"] is None


def test_rain_two_hours_ago_decays_with_a_dry_by_time():
    res = evaluate(NOW, rows(600, rain_at=(120, 180), rain_mm=0.1), None, None, None, None, ConditionsConfig())
    assert res["state"] in ("Wet", "Damp")
    assert res["dry_by"] and res["dry_by"] > NOW


def test_old_light_rain_has_dried():
    res = evaluate(NOW, rows(600, rain_at=(500, 505), rain_mm=0.05), None, None, None, None, ConditionsConfig())
    assert res["state"] == "Dry"


def test_dew_forming_now_is_damp():
    res = evaluate(NOW, rows(600, temp=12.0, dew=11.0), None, None, 40.0, -100.0, ConditionsConfig())
    assert any(r["rule"] == "Dew" and r["state"] == "Damp" for r in res["rules"])


def test_robot_rain_veto_only_when_calibrated():
    uncalibrated = evaluate(NOW, rows(60), None, 500, None, None, ConditionsConfig())
    assert uncalibrated["state"] == "Dry"
    calibrated = evaluate(NOW, rows(60), None, 500, None, None, ConditionsConfig(robot_rain_wet_below=1000))
    assert calibrated["state"] == "Wet"


def test_sunrise_helpers_bracket_now():
    rise, nxt = sunrise_ts(NOW, 40.0, -100.0), next_sunrise_ts(NOW, 40.0, -100.0)
    assert rise <= NOW < nxt and 20 * 3600 < nxt - rise < 28 * 3600


# -- storage migration ------------------------------------------------------------------------------------------
def test_old_database_gains_new_columns(tmp_path):
    db = sqlite3.connect(tmp_path / "yarbo.db")
    db.execute("CREATE TABLE telemetry (ts REAL, run_id INTEGER, x REAL)")
    db.commit()
    db.close()
    from app.store import Store
    st = Store(str(tmp_path))
    cols = {r["name"] for r in st.db.execute("PRAGMA table_info(telemetry)")}
    assert {"odo_l", "odo_r", "battery"} <= cols
    st.add_weather({"ts": 1.0, "temp_c": 10.0})
    st.set_setting("wifi_survey", "next")
    assert st.setting("wifi_survey") == "next" and st.weather_since(0)[0]["temp_c"] == 10.0
