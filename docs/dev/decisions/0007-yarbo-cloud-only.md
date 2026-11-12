# 0007. Yarbo cloud only: no local route

- **Status:** Accepted
- **Date:** 2026-11-11
- **Links:** #91, #111, #113

## Context
The panel first read the robot on its own MQTT broker on the home network, and later sent run commands there too
(faster than the cloud on a weak link). Yarbo firmware V3.14.4x closes that broker: connections to 1883 are refused.
The broker was anonymous, so closing it is likely deliberate and unlikely to be undone. Keeping two routes meant
route modes, fallbacks and a repeat of protective commands by the other route, all for a route that no longer exists.

## Decision
The panel talks to the robot only through the Yarbo cloud, as the official app does. `app.link.RobotLink` is the one
doorway to `CloudTransport.publish`; there are no route modes and no fallback. The robot's serial comes from the
account's robot list and is never typed in. Signing in to the Yarbo cloud is required.

## Consequences
- The panel needs internet and a working Yarbo account; when the cloud is down, the panel only records what it has.
- Commands can take up to about a minute to act on a weak link.
- Every command is sent once; nothing repeats it by another route.
- Robots on older firmware with the local broker open are not used locally either.
- Revisit only if Yarbo offers an authenticated local API.

## Enforced by
`app/link.py`, `app/cloud.py`, `app/commands.py`; `tests/test_guards.py`
`test_only_the_link_reaches_the_cloud_publish` and `test_only_known_modules_publish_to_the_robot`;
`tests/test_cloud_only.py`.
