"""Install defaults from the environment. Nothing install-specific is hard-coded, and nothing here is required:
every value can also be set, and overrides this, in Settings (see runtime.py)."""
import os
from dataclasses import dataclass


def _num(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class Config:
    yarbo_host: str | None
    yarbo_port: int
    yarbo_sn: str | None
    data_dir: str
    port: int
    poll_idle_s: float
    poll_active_s: float
    map_refresh_s: float
    raw_every_s: float
    raw_keep_days: int
    run_idle_end_s: float
    tempest_port: int
    ha_url: str | None
    ha_token: str | None
    demo: bool
    frigate_url: str | None
    panel_url: str | None
    demo_speed: float = 1.0
    robot_service_url: str | None = None   # YARBO_ROBOT_SERVICE_URL: the robot service (§13); no default


def load() -> Config:
    demo = os.environ.get("DEMO", "").strip() == "1" or os.environ.get("DEMO_SEED", "").strip() == "1"
    return Config(
        yarbo_host=None if demo else os.environ.get("YARBO_HOST", "").strip() or None,
        yarbo_port=int(_num("YARBO_PORT", 1883)),
        yarbo_sn=None if demo else os.environ.get("YARBO_SN", "").strip() or None,
        data_dir=os.environ.get("DATA_DIR", "./data"),
        port=int(_num("PORT", 8425)),
        poll_idle_s=_num("POLL_IDLE_S", 60),
        poll_active_s=_num("POLL_ACTIVE_S", 2),
        map_refresh_s=_num("MAP_REFRESH_S", 6 * 3600),
        raw_every_s=_num("RAW_EVERY_S", 60),
        raw_keep_days=int(_num("RAW_KEEP_DAYS", 90)),
        run_idle_end_s=_num("RUN_IDLE_END_S", 300),
        tempest_port=0 if demo else int(_num("TEMPEST_PORT", 50222)),
        ha_url=None if demo else os.environ.get("HA_URL", "").strip() or None,
        ha_token=None if demo else os.environ.get("HA_TOKEN", "").strip() or None,
        # DEMO=1: a made-up robot in a made-up yard (app/demo_robot.py); never the real robot, cloud or Home Assistant.
        # DEMO_SEED=1 is the older name and means the same.
        demo=demo,
        frigate_url=None if demo else os.environ.get("FRIGATE_URL", "").strip() or None,
        panel_url=os.environ.get("PANEL_URL", "").strip() or None,
        # Not in this repository: the address of the cloud's robot service is set on the deployment (design.md §13).
        robot_service_url=None if demo else os.environ.get("YARBO_ROBOT_SERVICE_URL", "").strip() or None,
        demo_speed=max(0.1, min(50.0, _num("DEMO_SPEED", 1.0))),
    )
