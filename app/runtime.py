"""The configuration in effect: what the user set in Settings, else the environment.

The environment (Config) is the install's defaults. Settings → integrations holds what the user changed in the UI:
one dict per group, only the fields they set. Runtime answers with Config's attribute names, so the rest of the app
keeps reading cfg.frigate_url and gets the live value whichever layer it came from. Nothing here talks to anything.
"""
from __future__ import annotations

import logging
import math
import os
import re

from .config import Config

# group -> {field in the API: attribute on Config}
GROUPS: dict[str, dict[str, str]] = {
    "robot": {"host": "yarbo_host", "port": "yarbo_port", "sn": "yarbo_sn"},
    "frigate": {"url": "frigate_url"},
    "home_assistant": {"url": "ha_url", "token": "ha_token"},
    "weather": {"tempest_port": "tempest_port"},
    "panel": {"url": "panel_url", "poll_idle_s": "poll_idle_s", "poll_active_s": "poll_active_s",
              "map_refresh_s": "map_refresh_s", "raw_every_s": "raw_every_s", "raw_keep_days": "raw_keep_days",
              "run_idle_end_s": "run_idle_end_s"},
}
ENV_NAMES = {"yarbo_host": "YARBO_HOST", "yarbo_port": "YARBO_PORT", "yarbo_sn": "YARBO_SN", "frigate_url": "FRIGATE_URL",
             "ha_url": "HA_URL", "ha_token": "HA_TOKEN", "tempest_port": "TEMPEST_PORT", "panel_url": "PANEL_URL",
             "poll_idle_s": "POLL_IDLE_S", "poll_active_s": "POLL_ACTIVE_S", "map_refresh_s": "MAP_REFRESH_S",
             "raw_every_s": "RAW_EVERY_S", "raw_keep_days": "RAW_KEEP_DAYS", "run_idle_end_s": "RUN_IDLE_END_S"}
SECRETS = {("home_assistant", "token")}
# DEMO=1 talks to no robot, Home Assistant, Frigate or Tempest: these groups keep the environment's (empty) values.
DEMO_LOCKED = ("robot", "frigate", "home_assistant", "weather")
# false stored here means "switched off", even when the environment has a value
OFFABLE = {("robot", "host"), ("robot", "sn"), ("frigate", "url"), ("home_assistant", "url"), ("home_assistant", "token"),
           ("panel", "url")}
log = logging.getLogger(__name__)
_ATTR = {attr: (group, field) for group, fields in GROUPS.items() for field, attr in fields.items()}
_HOST = re.compile(r"^[A-Za-z0-9.\-]+$")
_SN = re.compile(r"^[A-Za-z0-9\-]{4,40}$")


def _number(v, lo, hi, what: str, integer: bool = False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} must be a number")
    if integer and v != int(v):
        raise ValueError(f"{what} must be a whole number")
    if not lo <= v <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return int(v) if integer else float(v)


def _url(v, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{what} must be an http(s) address")
    v = v.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", v):
        raise ValueError(f"{what} must start with http:// or https://")
    return v


def _text(v, what: str, pattern: re.Pattern | None = None, example: str = "") -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{what} must not be empty")
    v = v.strip()
    if pattern and not pattern.match(v):
        raise ValueError(f"{what} looks wrong{f' (expected something like {example})' if example else ''}")
    return v


VALIDATE = {
    ("robot", "host"): lambda v: _text(v, "the robot address", _HOST, "192.0.2.50"),
    ("robot", "port"): lambda v: _number(v, 1, 65535, "the port", integer=True),
    ("robot", "sn"): lambda v: _text(v, "the serial", _SN, "YB0000DEMO0001"),
    ("frigate", "url"): lambda v: _url(v, "the Frigate address"),
    ("home_assistant", "url"): lambda v: _url(v, "the Home Assistant address"),
    ("home_assistant", "token"): lambda v: _text(v, "the token"),
    ("weather", "tempest_port"): lambda v: _number(v, 0, 65535, "the UDP port", integer=True),
    ("panel", "url"): lambda v: _url(v, "the panel address"),
    ("panel", "poll_idle_s"): lambda v: _number(v, 5, 3600, "idle polling"),
    ("panel", "poll_active_s"): lambda v: _number(v, 0.5, 60, "polling during a run"),
    ("panel", "map_refresh_s"): lambda v: _number(v, 300, 7 * 86400, "the map refresh"),
    ("panel", "raw_every_s"): lambda v: _number(v, 10, 3600, "the raw snapshot interval"),
    ("panel", "raw_keep_days"): lambda v: _number(v, 1, 3650, "raw snapshot retention", integer=True),
    ("panel", "run_idle_end_s"): lambda v: _number(v, 30, 3600, "the run idle timeout"),
}


def _check(group: str, field: str, v):
    """A stored or incoming value: false means off where that makes sense, anything else is validated."""
    if v is False:
        if (group, field) not in OFFABLE:
            raise ValueError(f"{field} cannot be switched off; leave it blank for the default")
        return False
    return VALIDATE[(group, field)](v)


class Runtime:
    """Reads like a Config; writes go to the settings store under 'integrations'. A stored false is "off"."""

    def __init__(self, cfg: Config, store):
        self.env = cfg
        self.store = store
        self._stored: dict = {}
        raw = store.setting("integrations") or {}
        for group, fields in (raw.items() if isinstance(raw, dict) else []):
            if group not in GROUPS or not isinstance(fields, dict):
                continue
            for field, v in fields.items():
                if field not in GROUPS[group]:
                    continue
                try:
                    clean = _check(group, field, v)
                except ValueError as exc:
                    log.warning("ignoring stored %s.%s: %s", group, field, exc)
                    continue
                if clean is not None:
                    self._stored.setdefault(group, {})[field] = clean

    def __getattr__(self, name: str):
        if name in ("env", "store", "_stored"):
            raise AttributeError(name)
        at = _ATTR.get(name)
        if at and not (getattr(self.env, "demo", False) and at[0] in DEMO_LOCKED):
            v = self._stored.get(at[0], {}).get(at[1])
            if v is not None:
                return v
        return getattr(self.env, name)

    def stored(self, group: str, field: str):
        return self._stored.get(group, {}).get(field)

    def snapshot(self, group: str) -> dict:
        return dict(self._stored.get(group, {}))

    def restore(self, group: str, values: dict) -> None:
        """Put a group back as snapshot() saw it (after an apply step failed)."""
        if values:
            self._stored[group] = dict(values)
        else:
            self._stored.pop(group, None)
        self.store.set_setting("integrations", self._stored)

    def describe(self) -> dict:
        """Per field: the value in effect, where it came from, and the environment's value, for the Settings UI.
        Secrets are never returned; only whether one is set."""
        out = {}
        for group, fields in GROUPS.items():
            g = {}
            for field, attr in fields.items():
                stored, env = self._stored.get(group, {}).get(field), getattr(self.env, attr)
                value = stored if stored is not None else env
                secret = (group, field) in SECRETS
                env_given = bool(os.environ.get(ENV_NAMES[attr], "").strip())
                source = "setting" if stored is not None else "env" if env_given else "default" if env not in (None, "") else "none"
                item = {"source": source, "env_name": ENV_NAMES[attr]}
                if secret:
                    item.update({"set": bool(value), "env_set": bool(env)})
                else:
                    item.update({"value": value, "env": env})
                g[field] = item
            out[group] = g
        return out

    def set(self, group: str, values: dict) -> list[str]:
        """Apply a partial update to one group. A field left out is unchanged; null clears the user's value (the
        environment applies again); anything else is validated and stored. Returns the fields that changed."""
        if group not in GROUPS:
            raise KeyError(group)
        if not isinstance(values, dict):
            raise ValueError("expected an object of fields")
        if getattr(self.env, "demo", False) and group in DEMO_LOCKED:
            raise ValueError("this is a demo instance (DEMO=1): it has no real robot or integrations to set up")
        unknown = [k for k in values if k not in GROUPS[group]]
        if unknown:
            raise ValueError(f"unknown field {unknown[0]!r} for {group}")
        current = dict(self._stored.get(group, {}))
        changed = []
        for field, v in values.items():
            if v is None:
                if field in current:
                    del current[field]
                    changed.append(field)
                continue
            clean = _check(group, field, v)
            if current.get(field) != clean:
                current[field] = clean
                changed.append(field)
        if changed:
            if current:
                self._stored[group] = current
            else:
                self._stored.pop(group, None)
            self.store.set_setting("integrations", self._stored)
        return changed
