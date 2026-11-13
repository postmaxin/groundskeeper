# 0011. Bluetooth local control: open to contributors, not planned

- **Status:** Accepted
- **Date:** 2026-11-12
- **Links:** 0007, 0002, 0004, #150

## Context
0007 made the panel cloud only after firmware closed the robot's local MQTT broker. The robot still has a
Bluetooth (BLE) module, which reports its own firmware version (`ble_version`). It may offer a local route that
needs no internet. Nobody has looked at what it accepts, how it authenticates, or whether it reaches far enough
from where the panel runs.

## Decision
The maintainers will not work on a Bluetooth route. A contributor who works one out is welcome to propose it,
and it will be accepted if it:

- comes with a new ADR that supersedes 0007, agreed in #150 before the code;
- goes through `app.link.RobotLink` like every other command, so the guards and the command checks apply
  unchanged;
- keeps 0002 (no `cmd_vel`, no motor commands) and 0004 (writes only from captures, read back);
- is off by default and never repeats a command already sent by the cloud;
- has no real robot data, keys or pairing secrets in code, docs or tests.

## Consequences
- The panel stays cloud only until such a PR lands.
- Bluetooth range and Docker access to the host's adapter are likely the hard parts, not only the protocol.
- The route-mode complexity 0007 removed would partly come back; the superseding ADR must say how it is contained.

## Enforced by
Nothing yet: 0007's tests (`tests/test_cloud_only.py`, `tests/test_guards.py`) keep the panel cloud only until
a superseding ADR changes them.
