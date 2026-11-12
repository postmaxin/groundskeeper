"""What the panel may publish to the robot, and the checks every send goes through.

`READ_ONLY_COMMANDS` are the read requests (named get_/read_, or listed in COMPUTE_ONLY with a reason): the recorder,
backups and Map Studio send only these, through app.link.RobotLink.read. `check_command` lets through only the run
commands in control.CATALOGUE and the captured writes in mapwrite.WRITE_COMMANDS, never cmd_vel. Everything goes
through the Yarbo cloud (app/cloud.py); the robot's own broker on the home network is closed by current firmware.
"""
READ_ONLY_COMMANDS = frozenset({
    "get_device_msg",
    "get_map",
    "read_all_plan",
    "get_plan_feedback",   # the running plan's progress, or "no plan working"
    "read_all_clean_area", # every area's full record (what an in-place area save starts from)
    "read_mower_area_params",  # one area's mow settings, by id
    "read_gps_ref",
    "read_low_cfd_config",
    "read_tow_params",
    "get_all_map_backup",        # the robot's list of its own map backups (id, name, timestamp)
    "get_map_buckup_from_id",    # one backup's map, by id (sic: the robot's spelling)
    "read_mower_pathway_params", # one pathway's mowing settings, by id
    "read_deadend_params",       # one dead end's settings, by id (id 0 in the reply = the robot's defaults)
    "get_wifi_list",             # the Wi-Fi networks the robot hears (a ~4 s scan): the survey and Health → Network
    "get_connect_wifi_name",     # the Wi-Fi network it is on (no answer when none): Health → Network
    "read_schedules",            # the robot's own schedules (made in the app): the Schedule page warns about them
}) | {
    "preview_snowbot_area_path", # COMPUTE_ONLY: the planned mowing route for an area outline (the app's preview)
}
# Requests that are not named get_/read_ but only make the robot compute and answer (nothing on it changes). Each one is
# listed here by name, so the read-only test still catches anything else.
COMPUTE_ONLY = frozenset({"preview_snowbot_area_path"})


class ReadOnlyViolation(Exception):
    """Raised for any attempt to publish something other than a read request."""


def check_read_only(cmd: str) -> None:
    if cmd not in READ_ONLY_COMMANDS:
        raise ReadOnlyViolation(f"refusing to publish {cmd!r}: only read requests are allowed")


def check_command(cmd: str) -> None:
    """A command the panel may send: a run command or a captured write. cmd_vel never."""
    from . import control, mapwrite          # imported here: both modules sit above this one
    if cmd in control.HARD_BLOCKED or cmd not in (set(control.CATALOGUE) | set(mapwrite.WRITE_COMMANDS)):
        raise ReadOnlyViolation(f"refusing to publish {cmd!r}: not a run command or a captured write")
