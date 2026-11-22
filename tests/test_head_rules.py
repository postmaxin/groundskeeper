"""Run rules that depend on the attached head (ADR 0009): only a snow blower named in a recent message skips Lawn
dry, and it keeps every other rule. The rain guard's half is in test_runs.py, the recorder's in test_poses.py."""
import pytest

from app import control


# -- ADR 0009: only a fresh snow blower report relaxes a mowing rule -----------------------------------------------
# No head heard, no head, leaf blower, mower, Smart Cover, Mower Pro, and values that are not a head type at all.
NOT_THE_SNOW_BLOWER = [None, 0, 2, 3, 4, 5, 6, "1", True, 1.0, 1.5, [1]]
WET = {"plans": [{"id": 1}], "error_code": 0, "battery": 90, "running": False, "route_state": "connected",
       "yard": {"ok": True, "detail": "clear", "events": []}, "phone_app_active": False, "conditions": "Wet",
       "presence": True, "writing": False}
STARTS = [("start_plan", {"plan_id": 1}, "user"), ("start_plan", {"plan_id": 1}, "scheduler"),
          ("start_plan", {"plan_id": 1}, "mcp"), ("start_plan", {"plan_id": 1}, "token"),
          ("resume", {}, "guard"), ("resume", {}, "mcp"), ("resume", {}, "token")]


def _checked(cmd, args, actor, **ctx):
    ctx = {**WET, "snapshot_age_s": 5, "head_age_s": 5, **ctx}
    checks = control.check(cmd, args, ctx, control.GATE_DEFAULTS, actor)
    return control.verdict(checks, override=False), {c["name"]: c for c in checks}


def test_only_a_fresh_snow_blower_report_counts_as_the_snow_blower():
    assert control.snow_blower(1, 0) and control.snow_blower(1, control.ONLINE_S - 1)
    for head in NOT_THE_SNOW_BLOWER:
        assert not control.snow_blower(head, 5), f"head {head!r} must get the mowing rules (ADR 0009)"
    for age in (None, control.ONLINE_S, control.ONLINE_S + 1, -1, -0.5, "5", True):
        assert not control.snow_blower(1, age), f"a report aged {age!r} must get the mowing rules (ADR 0009)"


@pytest.mark.parametrize("cmd, args, actor", STARTS)
def test_lawn_dry_is_listed_but_not_checked_for_the_snow_blower(cmd, args, actor):
    (ok, reason), by_name = _checked(cmd, args, actor, head_type=1)
    assert ok, f"a wet lawn refused a snow blower's {cmd} from {actor}: {reason}"
    lawn = by_name["Lawn dry"]
    assert lawn["ok"] is None and not lawn["hard"] and lawn["detail"] == control.NOT_FOR_SNOW


@pytest.mark.parametrize("cmd, args, actor", [s for s in STARTS if s[2] != "user"])
@pytest.mark.parametrize("head", NOT_THE_SNOW_BLOWER)
def test_lawn_dry_still_refuses_every_other_head(cmd, args, actor, head):
    (ok, reason), _ = _checked(cmd, args, actor, head_type=head)
    assert not ok and "Lawn dry" in reason, f"head {head!r} skipped Lawn dry (ADR 0009: only the snow blower does)"


@pytest.mark.parametrize("cmd, args, actor", [s for s in STARTS if s[2] != "user"])
def test_lawn_dry_still_refuses_when_the_head_report_is_missing_or_stale(cmd, args, actor):
    (ok, reason), _ = _checked(cmd, args, actor)                                    # no head in the context at all
    assert not ok and "Lawn dry" in reason
    # The robot is online, but no message has named the head lately (or ever): the snapshot's age is not the head's.
    for age in (control.ONLINE_S, None, -1):
        (ok, reason), _ = _checked(cmd, args, actor, head_type=1, head_age_s=age)
        assert not ok and "Lawn dry" in reason and "Robot online" not in reason, f"head age {age!r}"


@pytest.mark.parametrize("cmd, args, actor", [s for s in STARTS if s[2] in control.STRICT_ACTORS])
@pytest.mark.parametrize("over, name", [({"yard": {"ok": False, "detail": "person", "events": []}}, "Yard clear"),
                                        ({"yard": {"ok": None, "detail": "no Frigate", "events": []}}, "Yard clear"),
                                        ({"phone_app_active": True}, "Phone app"),
                                        ({"phone_app_active": None}, "Phone app"),
                                        ({"presence": False}, "Someone home"),
                                        ({"presence": None}, "Someone home"),
                                        ({"error_code": 7}, "No active error"),
                                        ({"writing": True}, "write to the robot")])
def test_the_snow_blower_keeps_every_other_rule(cmd, args, actor, over, name):
    (ok, reason), _ = _checked(cmd, args, actor, head_type=1, **over)
    assert not ok and name in reason, f"the snow blower skipped '{name}' (ADR 0009 relaxes Lawn dry and nothing else)"


def test_the_snow_blower_keeps_the_battery_rule():
    (ok, reason), _ = _checked("start_plan", {"plan_id": 1}, "scheduler", head_type=1, battery=5)
    assert not ok and "Battery" in reason
