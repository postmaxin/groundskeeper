"""Wi-Fi survey (get_wifi_list during runs) and the Wi-Fi Signal map, 2026-10-10."""
import asyncio

import pytest
from fastapi import HTTPException

from app import coverage, main
from app import recorder as rec_mod
from app.recorder import GAP_S, WIFI_POLL_S, WIFI_TEST_MIN_S, WIFI_TIMEOUT_S, Recorder
from app.runs import RunStats
from app.store import Store
from app.commands import check_read_only

from .test_settings_api import fake_request


class FakeTransport:
    def __init__(self):
        self.connected, self.sent, self.last_message_at = True, [], 0.0

    def request(self, cmd, payload=None):
        check_read_only(cmd)
        self.sent.append(cmd)


def bare(store, run_id=None):
    r = Recorder.__new__(Recorder)
    r.store, r.run_id, r.t, r.update_at, r._sent = store, run_id, {"x": 1.0, "y": 2.0}, 0.0, {}
    r.transport, r.wifi_survey, r._wifi_asked, r._wifi_waiting = FakeTransport(), None, 0.0, None
    r._wifi_foreign_at, r._robot_at, r.activity, r._wifi_answer_at = 0.0, 0.0, "mowing", 0.0
    r._signal_cache, r.gps_ref = {}, (39.0, -76.5)
    r.link, r.reads_sent = None, 0              # reads go to the fake transport, as without Ops
    return r


def reply(*nets, state=0):
    return {"topic": "get_wifi_list", "state": state, "msg": "",
            "data": {"wifi_list": [{"wifi_name": n, "signal": str(s), "security": "WPA2", "saved": sv} for n, s, sv in nets]}}


def tick(r, t0, t1):
    for now in range(int(t0), int(t1)):
        r._poll_wifi(float(now))


def test_get_wifi_list_is_an_allowed_read():
    check_read_only("get_wifi_list")


def test_no_polling_when_off_between_runs_or_parked(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, run_id=None)
    st.set_setting("wifi_survey", "on")
    r._poll_wifi(1000.0)                                            # no run
    r.run_id = 1
    r.activity = "charging_break"
    r._poll_wifi(1001.0)                                            # parked mid-plan
    r.activity = "mowing"
    st.set_setting("wifi_survey", "off")
    r._poll_wifi(1002.0)
    assert r.transport.sent == [] and r.wifi_survey is None


def test_one_ask_at_a_time_and_each_network_stored_mid_scan(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "next")
    tick(r, 1000, 1000 + WIFI_POLL_S + 5)
    assert r.transport.sent == ["get_wifi_list"]                    # still waiting: no second scan stacked on it
    r.t, r.update_at = {"x": 3.0, "y": 2.0}, 1004.0
    r._on_wifi(reply(("Home", 62, False), ("Home", 40, True), ("Next door", 30, False), ("", 90, False)), 1004.4)
    s = r.wifi_survey
    assert s["answered"] == 1 and s["reply_s"] == [pytest.approx(4.4)] and s["scans"] == 1
    nets = {n["name"]: n for n in st.wifi_networks(0)}
    assert set(nets) == {"Home", "Next door"}                       # hidden networks are left out
    assert nets["Home"]["avg"] == 62 and nets["Home"]["saved"]      # strongest of the same name, saved if any was
    assert st.wifi_points(0, "Home") == [{"x": 2.0, "y": 2.0, "signal": 62.0}]   # between ask (1, 2) and answer (3, 2)
    tick(r, 1035, 1036)
    assert r.transport.sent == ["get_wifi_list"] * 2


def test_an_unanswered_ask_times_out_before_the_next(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "on")
    tick(r, 1000, 1000 + WIFI_TIMEOUT_S + 1)
    assert r.transport.sent == ["get_wifi_list"] * 2 and r.wifi_survey["unanswered"] == 1


def test_off_stores_nothing_and_other_clients_answers_are_neither_timed_nor_kept(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    r.update_at = 1000.0
    r._on_wifi(reply(("Home", 62, False)), 1001.0)                  # off: nothing is kept
    assert st.wifi_stamp(0)[0] == 0
    st.set_setting("wifi_survey", "on")
    tick(r, 1000, 1002)
    r._wifi_foreign_at = 1001.5                                     # someone else asked after us
    r.update_at = 1004.0
    r._on_wifi(reply(("Home", 62, False)), 1004.5)
    assert r.wifi_survey["answered"] == 0 and r.wifi_survey["scans"] == 0   # whose answer is it? not placed


def test_late_and_repeated_answers_are_counted_not_placed(tmp_path):
    """Run 4, 2026-10-10: answers 30-60 s late while mowing, sometimes twice ~0.1 s apart."""
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "on")
    tick(r, 1000, 1001)                                             # ask at 1000
    r.update_at = 1040.0
    r._on_wifi(reply(("Home", 62, False)), 1040.0)                  # 40 s later: the robot has moved on
    r._on_wifi(reply(("Home", 62, False)), 1040.1)                  # the same answer again
    s = r.wifi_survey
    assert (s["answered"], s["late"], s["repeats"], s["scans"]) == (1, 1, 1, 0) and s["reply_s"] == [40.0]
    tick(r, 1040, 1100)                                             # the next ask goes out once the last is answered
    assert r.transport.sent == ["get_wifi_list"] * 2


def test_answers_without_a_fresh_position_or_refused_are_not_stored(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "on")
    tick(r, 1000, 1001)
    r._on_wifi(reply(("Home", 62, False)), 1100.0)                  # last position 1100 s old
    r.update_at = 1200.0
    r._on_wifi(reply(("Home", 62, False), state=-1), 1201.0)         # the robot said no
    assert st.wifi_stamp(0)[0] == 0


def test_pauses_in_the_robots_messages_are_split_by_whether_a_scan_was_running(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "on")
    r._robot_heard(999.5)
    tick(r, 1000, 1001)                                             # asks at 1000
    for ts in (1000.5, 1001.0, 1006.0, 1006.5):                     # 5 s pause starting 1 s after the ask
        r._robot_heard(ts)
    for ts in range(1007, 1050):                                    # normal traffic, about one a second
        r._robot_heard(float(ts))
    for ts in (1050.0, 1050.5, 1055.0, 1055.4):                     # 4.5 s pause with no scan running
        r._robot_heard(ts)
    s = r.wifi_survey
    assert (s["gaps_near"], s["gaps_other"]) == (1, 1)
    assert s["gap_max_near"] == pytest.approx(5.0) and s["gap_max_other"] == pytest.approx(4.5)
    r._robot_heard(1055.4 + GAP_S - 0.1)                            # normal spacing is not a pause
    assert s["gaps_other"] == 1


def test_next_run_only_switches_itself_off_after_ten_minutes_of_driving(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "next")
    tick(r, 1000, 1100)                                             # a short drive
    r._finish_wifi_survey(r.run_id)
    assert st.setting("wifi_survey") == "next" and any("waits for the next run" in e["detail"] for e in st.events(5))
    tick(r, 2000, 2000 + WIFI_TEST_MIN_S + 5)                       # answered or not, the drive counts
    r.activity = "charging_break"
    tick(r, 3000, 9000)                                             # parked time adds nothing
    assert r.wifi_survey["moving_s"] < WIFI_TEST_MIN_S + 10
    r._finish_wifi_survey(r.run_id)
    done = next(e["detail"] for e in st.events(5) if "switched off" in e["detail"])
    assert st.setting("wifi_survey") == "off" and "unanswered" in done
    st.set_setting("wifi_survey", "on")
    r.activity = "mowing"
    tick(r, 10000, 10000 + WIFI_TEST_MIN_S + 5)
    r._finish_wifi_survey(r.run_id)
    assert st.setting("wifi_survey") == "on"                        # every run stays on


def test_networks_rank_saved_first_then_heard_best_and_silence_counts_as_zero(tmp_path):
    st = Store(str(tmp_path))
    rid = st.start_run(1000, 5, None)
    st.add_wifi_scan(1000, rid, 0.0, 0.0, [("Corner", 90, False), ("Everywhere", 50, False)])
    st.add_wifi_scan(1030, rid, 10.0, 0.0, [("Everywhere", 50, False)])
    st.add_wifi_scan(1060, rid, 20.0, 0.0, [("Everywhere", 50, False), ("Robot's", 10, True)])
    st.add_wifi_scan(1090, rid, 30.0, 0.0, [])                     # heard nothing at all
    assert [n["name"] for n in st.wifi_networks(0)] == ["Robot's", "Everywhere", "Corner"]
    pts = st.wifi_points(0, "Corner")
    assert sorted((p["x"], p["signal"]) for p in pts) == [(0.0, 90.0), (10.0, 0.0), (20.0, 0.0), (30.0, 0.0)]
    assert st.prune_wifi(0) == 6 and st.wifi_stamp(0)[0] == 0


def test_merging_runs_moves_the_survey_too(tmp_path):
    st = Store(str(tmp_path))
    a = st.start_run(1000, 5, None)
    st.end_run(a, 1100, "paused too long", RunStats())
    b = st.start_run(1200, 5, None)
    st.add_wifi_scan(1250, b, 0.0, 0.0, [("Home", 50, False)])
    st.merge_runs(a, b)
    assert st.db.execute("SELECT DISTINCT run_id FROM wifi_seen").fetchall()[0][0] == a


def test_the_wifi_signal_map_uses_4_m_cells_and_the_chosen_network(tmp_path, monkeypatch):
    st = Store(str(tmp_path))
    rid = st.start_run(1000, 5, None)
    for i in range(4):
        st.add_wifi_scan(1000 + 30 * i, rid, 0.5 + i * 0.5, 0.5, [("A", 60, False), ("B", 20, False)])
    r = bare(st, rid)
    monkeypatch.setattr(rec_mod.time, "time", lambda: 2000.0)
    d = asyncio.run(r.wifi_cells(30, None))
    assert d["network"] == "A" and len(d["cells"]) == 1 and d["cells"][0]["cell_m"] == 4.0 and d["cells"][0]["value"] == 60
    assert len(d["cells"][0]["ring"]) == 5
    assert asyncio.run(r.wifi_cells(30, "B"))["cells"][0]["value"] == 20
    assert asyncio.run(r.wifi_cells(30, "nobody"))["network"] == "A"
    st.add_wifi_scan(1200, rid, 1.0, 0.5, [("C", 99, True)])
    assert asyncio.run(r.wifi_cells(30, None))["network"] == "C"     # a new scan refreshes the cached list
    assert coverage.signal_grid([{"x": 1, "y": 1, "halow": -60}] * 5, "halow")[0]["cell_m"] == 2.0   # others unchanged


def test_the_setting_takes_three_values_only(tmp_path):
    req, r, _ = fake_request(tmp_path)
    assert asyncio.run(main.put_setting(req, "wifi_survey", "next")) == {"wifi_survey": "next"}
    assert r.store.setting("wifi_survey") == "next"
    for bad in ("always", True, None):
        with pytest.raises(HTTPException):
            asyncio.run(main.put_setting(req, "wifi_survey", bad))
    assert r.store.setting("wifi_survey") == "next"


def test_a_quick_answer_after_a_late_one_is_not_taken_for_a_repeat(tmp_path):
    st = Store(str(tmp_path))
    r = bare(st, st.start_run(1000, 5, None))
    st.set_setting("wifi_survey", "on")
    tick(r, 1000, 1001)                                             # ask N at 1000
    r.update_at = 1029.0
    r._on_wifi(reply(("Home", 62, False)), 1029.0)                  # late
    tick(r, 1030, 1032)                                             # not within 3 s of that answer: no ask yet
    assert r.transport.sent == ["get_wifi_list"]
    tick(r, 1032, 1033)                                             # ask N+1 at 1032
    r.update_at = 1035.0
    r._on_wifi(reply(("Home", 62, False)), 1035.5)                  # a quick answer: counted, placed
    s = r.wifi_survey
    assert r.transport.sent == ["get_wifi_list"] * 2 and s["repeats"] == 0 and s["scans"] == 1


def test_the_survey_is_kept_through_a_restart_and_judged_on_the_whole_run(tmp_path):
    st = Store(str(tmp_path))
    first = st.start_run(1000, 5, None)
    r = bare(st, first)
    st.set_setting("wifi_survey", "next")
    tick(r, 1000, 1000 + 400)                                      # ~6.7 min moving, then the panel restarts
    assert st.setting(f"wifi_survey_run:{first}")["moving_s"] >= 300
    st.end_run(first, 1400, "recorder restarted", RunStats())
    r2 = bare(st, st.start_run(1500, 5, None))                      # the new process: a new run, joined to the first
    tick(r2, 1500, 1500 + 300)
    piece = r2.run_id
    r2.run_id = first                                              # as _on_plan does: the earlier run carries on
    r2._join_survey(first, piece)
    assert r2.wifi_survey["moving_s"] >= WIFI_TEST_MIN_S - 30
    tick(r2, 1800, 1830)
    r2._finish_wifi_survey(first)
    ev = next(e for e in st.events(20) if e["kind"] == "wifi_survey")
    assert "test run done" in ev["detail"] and st.setting("wifi_survey") == "off"
    assert st.setting(f"wifi_survey_run:{first}") is None and st.setting(f"wifi_survey_run:{piece}") is None


def test_adding_two_pieces_of_survey_counters():
    a = {"asked": 3, "reply_s": [1.0], "gap_max_near": 4.0, "moving_s": 100, "_tick": 1}
    b = {"asked": 2, "reply_s": [2.0], "gap_max_near": 9.0, "moving_s": 50, "_tick": 5}
    s = Recorder._add_survey(a, b)
    assert s == {"asked": 5, "reply_s": [1.0, 2.0], "gap_max_near": 9.0, "moving_s": 150, "_tick": 5}
    assert Recorder._add_survey(None, b) == b and Recorder._add_survey(None, None) is None


def test_a_survey_cut_off_by_a_restart_and_never_joined_is_still_reported(tmp_path):
    from app.runs import PAUSE_CAP_S
    st = Store(str(tmp_path))
    rid = st.start_run(1000, 5, None)
    st.end_run(rid, 2000, "recorder restarted", RunStats())
    st.set_setting(f"wifi_survey_run:{rid}", {"asked": 4, "answered": 3, "unanswered": 1, "late": 0, "repeats": 0,
                                              "reply_s": [2.0, 3.0, 4.0], "scans": 3, "moving_s": 900, "gaps_near": 0,
                                              "gap_s_near": 0.0, "gap_max_near": 0.0, "gaps_other": 0, "gap_s_other": 0.0,
                                              "gap_max_other": 0.0})
    st.set_setting("wifi_survey", "next")
    r = bare(st)
    r._settle_old_surveys(2000 + PAUSE_CAP_S - 10)                   # it could still be joined: left alone
    assert st.setting(f"wifi_survey_run:{rid}") is not None
    r._settle_old_surveys(2000 + PAUSE_CAP_S + 10)
    ev = next(e for e in st.events(20) if e["kind"] == "wifi_survey")
    assert f"run {rid}" in ev["detail"] and "3 answers" in ev["detail"] and st.setting(f"wifi_survey_run:{rid}") is None
