"""Why the robot cannot run, in one set of words: what is wrong and what fixes it.

Every place that explains a start that would be refused, a schedule window that was skipped, a run that stopped
early or a robot that paused itself turns its raw cause into Reasons here, so Live, Schedule, a run's page, Events
and notifications all say the same thing. A Reason is a plain dict (it goes straight into JSON):
  {code, title, fix, detail, link, severity: "block" | "warn" | "info", source}
"""
from . import telemetry


def reason(code: str, title: str, fix: str, detail: str = "", link: str | None = None, severity: str = "block",
           source: str = "") -> dict:
    return {"code": code, "title": title, "fix": fix, "detail": detail, "link": link, "severity": severity,
            "source": source}


# The start checks (control.check), by name: (code, title when it fails, fix, link).
CHECKS = {
    "Robot online": ("offline", "The robot is not reporting",
                     "Check that it is powered on and on the network (HaLow or Wi-Fi)", "#settings/robot"),
    "Route to the robot": ("no_route", "The panel cannot reach the robot",
                           "Check the Yarbo cloud in Settings → Robot: sign in, or wait while it reconnects",
                           "#settings/robot"),
    "Plan exists": ("plan_missing", "That plan is not on the robot",
                    "Pick another plan, or make it again in the Yarbo app", "#help/not-yet"),
    "No active error": ("robot_error", "The robot has an active error",
                        "Clear the error on the robot or in the Yarbo app", "#health"),
    "Battery": ("battery", "The battery is too low", "Let it charge on the dock", "#settings/rules"),
    "Not already running": ("running", "A run is already going", "Wait for it to finish, or stop it", "#live"),
    "No write to the robot in progress": ("writing", "A change is being written to the robot",
                                          "Wait a minute for the map, settings or Wi-Fi change to finish", "#events"),
    "Yard clear (Frigate)": ("yard", "Someone (or an animal) is in the yard, or the cameras cannot tell",
                             "Wait until the yard is clear; check Frigate if the cameras are down", "#settings/cameras"),
    "Phone app not driving it": ("phone_app", "The Yarbo app is driving it, or the panel cannot tell",
                                 "Close the robot's screen in the Yarbo app. If the panel cannot tell, sign in to the "
                                 "Yarbo cloud (it shows the app's keep-awake) or start from the panel yourself",
                                 "#settings/robot"),
    "Lawn dry": ("lawn", "The lawn is not dry", "Wait for it to dry; the expected time is on Live", "#settings/lawn"),
    "Someone home (Alarmo)": ("presence", "Nobody is home, or Home Assistant cannot tell",
                              "Starts need someone home; check Home Assistant if it is down", "#settings/ha"),
}

# The robot's own pause reasons (telemetry.PAUSE_REASONS), when it paused itself mid-plan.
PAUSES = {
    2: ("charging_break", "Charging mid-plan", "Nothing: it goes back out by itself when charged", "info"),
    3: ("power_restart", "The robot restarted", "Check it, then resume from Live or the app", "block"),
    4: ("estop", "The emergency stop is pressed", "Release the stop button on the robot, then resume", "block"),
    5: ("bumper", "The bumper was hit", "Clear what it hit, then resume", "block"),
    6: ("stuck", "The robot is stuck", "Free it (see the map and cameras), then resume", "block"),
    7: ("fault", "The robot reports a fault", "See Health for the error, fix it, then resume", "block"),
    9: ("rain", "The robot's rain sensor stopped it", "Wait for the rain to pass and the lawn to dry", "block"),
}


def from_check(c: dict) -> dict:
    code, title, fix, link = CHECKS.get(c["name"], ("check", c["name"], "See the start rules", "#settings/rules"))
    if c["ok"] is None or c.get("detail") == "unknown":
        title = f"Cannot tell: {c['name'].lower()}"
        if code == "battery":
            fix = "Wait for the robot to report its charge"
        elif code == "lawn":
            fix = "Check the weather station or the Home Assistant weather sensors"
    return reason(code, title, fix, str(c.get("detail") or ""), link,
                  "block" if c.get("hard") else "warn", "check")


def from_checks(checks: list[dict], enabled: bool = True, plans: list | None = None, override: bool = False) -> list[dict]:
    """The reasons a start would be refused, most important first (none when it would go out)."""
    out = []
    if not enabled:
        out.append(control_off())
    if plans is not None and not plans:
        out.append(no_plans())
    for c in checks:
        failed = (c["hard"] and c["ok"] is not True) or (not c["hard"] and c["ok"] is False and not override)
        if not failed or (plans is not None and not plans and c["name"] == "Plan exists"):
            continue
        out.append(from_check(c))
    return out


def control_off() -> dict:
    return reason("control_off", "Control is switched off", "Switch it on in Settings → Safety", "", "#settings/safety",
                  source="switch")


def no_plans() -> dict:
    return reason("no_plans", "There are no plans on the robot",
                  "Make a plan in the Yarbo app; the panel cannot create plans yet", "", "#help/not-yet",
                  source="plans")


def no_plan_chosen() -> dict:
    return reason("no_plan_chosen", "This window has no plan", "Edit the window and pick a plan", "", "#schedule",
                  source="schedule")


def wrong_head(wanted: str) -> dict:
    return reason("wrong_head", f"The attached head is not a {wanted} head",
                  f"Fit the {wanted} head, or set the window's head to any", "", "#schedule", source="schedule")


def from_window(checks: list[dict], text: str) -> list[dict]:
    """A schedule window's last check (scheduler.last_eval) as reasons."""
    if checks:
        return from_checks(checks)
    if text.startswith("no plan chosen"):
        return [no_plan_chosen()]
    if text.startswith("attached head is not a "):
        return [wrong_head(text.removeprefix("attached head is not a ").removesuffix(" head"))]
    return [reason("other", text, "", source="schedule")] if text and text != "ok" else []


def from_pause(code) -> dict | None:
    """Why the robot paused itself (planning_paused), or None for a person's pause (1) or no pause."""
    if isinstance(code, bool) or not isinstance(code, (int, float)) or code != int(code):
        return None
    code = int(code)
    if code in (0, 1):
        return None
    code_, title, fix, sev = PAUSES.get(code, ("paused", f"Paused by the robot (reason {code})",
                                               "See Health, then resume", "block"))
    return reason(code_, title, fix, telemetry.PAUSE_REASONS.get(code, ""), "#live", sev, "robot")


def from_error(label: str | None) -> dict | None:
    """A robot plan / recharge error label ("WP017 Exceeded boundary") with the status dictionary's fix."""
    if not label:
        return None
    wp = label.split()[0]
    known = telemetry.STATUS_DICTIONARY.get(wp)
    return reason("robot_error", known[0] if known else label, known[1] if known else "See Health and the Yarbo app",
                  label, "#health", source="robot")


def from_end(end: str | None) -> dict | None:
    """Why a run ended, for anything but a finished plan or a run still going."""
    if not end or end == "plan finished":
        return None
    if end.startswith("error"):
        return reason("run_error", f"The run stopped on {end}", "See Health for the error and fix it before the next run",
                      end, "#health", source="run")
    if end == "recorder restarted":
        return reason("panel_restart", "The panel restarted during the run", "Nothing to fix: the robot carried on",
                      end, "#events", "info", "run")
    if end == "paused, not resumed":
        return reason("not_resumed", "The run was paused and never resumed",
                      "Resume sooner next time: a long pause ends the run", end, "#live", "warn", "run")
    if end == "returned to dock":
        # Also how a normal finish looks when the plan's own progress was not read: information, not a warning.
        return reason("docked_early", "Back on the dock; the panel did not see the plan finish",
                      "If the yard is not done, see Events for that time (a guard, rain, low battery or a person)",
                      end, "#events", "info", "run")
    return reason("stopped", "The run was stopped before the plan finished",
                  "Someone pressed stop (panel or app), or the robot stopped; see Events", end, "#events", "warn", "run")


def line(r: dict) -> str:
    """One line for a notification or a log: "Title. Fix." """
    return f"{r['title']}. {r['fix']}." if r.get("fix") else f"{r['title']}."


def lines(reasons: list[dict]) -> str:
    return " ".join(line(r) for r in reasons)
