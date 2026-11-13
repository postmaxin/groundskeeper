# 0012. Bluetooth (BLE) local control as an optional, off-by-default transport

- **Status:** Proposed
- **Date:** 2026-11-13
- **Links:** 0007, 0011, 0002, 0004, #150 (upstream), fork #73

> **Fork draft, not agreed upstream.** This is the fork's proposal, written to answer 0011. 0011 asks for the ADR to
> be agreed in upstream #150 before any code, so this stays in the fork until it has been tested on a real robot and
> taken to that issue. Until then 0007 stays Accepted: it would be marked `Superseded by 0012` only in the PR that
> upstream agrees. No code follows from this page yet.

## Context
0007 made the panel cloud only after firmware closed the robot's local MQTT broker. The robot still has a BLE
module, and a read of the official Android app (static, nothing sent to a robot) shows what it is: not a separate
API but a tunnel for the same topics and payloads as the cloud path. Each message is the same JSON (or protobuf)
body, optionally zlib-compressed, split into MTU-sized chunks, wrapped in a small protobuf `Packet` and written to
one GATT characteristic. Telemetry and replies come back the same way, in the cloud path's message types. When the
link is up the app prefers it for nearly every command. The framing is in `protocol.md` (Bluetooth section).

So BLE would be a second local path that needs no internet, which 0007 removed the last of. It also shows three
problems:

- The tunnel carries `cmd_vel` and the other motor commands in the official app, exactly as the cloud does.
- The app does no authentication at the application layer (no key, token, or bonding call). Whether the robot
  enforces anything itself is unknown.
- Reach is about 5 m in the vendor's own text, and a panel in a container may not see a Bluetooth adapter at all.

## Decision
Add an optional BLE transport, **off by default**, that sits behind `app.link.RobotLink` like the cloud one, so every
guard, interlock and command check applies unchanged. There is no second doorway.

- It sends only what the cloud path may send: the official run commands, the documented reads, and captured writes.
  Each `RobotLink` entry point keeps its own allowlist, unchanged: `command()` checks `CATALOGUE` and
  `HARD_BLOCKED`, `write_command()` checks `WRITE_COMMANDS`, and `read()` checks `READ_ONLY_COMMANDS`. They run
  before a BLE write exactly as before a cloud one, so `CATALOGUE` is not applied to reads or map writes.
- A command is sent by one route only. The panel never repeats by BLE a command the cloud already sent, or the other
  way round (0011).
- Using the route needs an explicit setting and a named adapter; with it off the panel behaves as 0007 describes.
- No real robot data, keys or pairing secrets in code, docs or tests.
- No code is written, and none of the framing is trusted, until the framing captures below are done. Map writes
  need the later write captures too.

### How it honours 0002
The BLE tunnel carries `cmd_vel`, `cmd_roller` and the other motor commands in the official app, so the transport is
not safe just because it is "only a transport". It must refuse them exactly as the cloud path does: the check runs in
`RobotLink` before the transport is chosen, never inside the BLE code, and a test sends each blocked command to the
BLE route and expects the same refusal. The route never opens a raw write door and never offers the app's manual
drive, joystick or Bluetooth mapping-drive flows. Nothing from those flows is captured for replay.

### How it honours 0004
Writes come only from captures read back as before. The app encodes five topics as protobuf over BLE, where the
cloud path sends JSON. Four are persistent map writes (`save_clean_area`, `save_pathway`, `save_deadend`,
`save_sidewalk`): their BLE encodings are different writes, and each needs its own capture, its own entry in
`docs/dev/map-writes.md`, and a replay against a harmless test object before use. The fifth,
`preview_snowbot_area_path`, is a compute-only request that `RobotLink.read()` allows, with no state to read back; it
stays on the cloud (or is refused on BLE) until its BLE encoding is captured too. Until then BLE carries reads and
the run commands only, and map writes stay on the cloud. Every BLE write is read back and put back on a mismatch.

## Consequences
- Local control without the internet becomes possible, with lower latency than a weak cloud link.
- The route-mode complexity that 0007 removed partly returns. It stays contained to one choice made inside
  `RobotLink` (cloud, BLE or neither per command), with no fallback that repeats a command.
- Open risks, none yet settled:
  - **No application-layer auth seen.** If the robot does not enforce link-layer security, anyone in range who knows
    the name pattern could connect. This is why the route is off by default, and it is not safe to try against a live
    robot until a capture shows what the robot enforces.
  - **Range.** About 5 m line of sight, 3 m for the Mini's Bluetooth mapping. A server in the house will not reach a
    robot in the yard; the adapter has to be near the robot's working area (or a relay close to it).
  - **Container access.** A container needs the host's adapter (D-Bus and BlueZ, or a passed-through device), and
    the Windows and macOS hosts differ.
  - **Single central.** The robot may accept one connection at a time, so the panel could lock out the phone app.
- Revisit or reject this if the captures show the robot accepts commands with no pairing and no pairing-mode window,
  and the panel cannot add a protection of its own.

### To confirm by an HCI capture before any code is trusted
**Framing captures** (needed before any BLE code). Taken with Android's Bluetooth HCI snoop log of the official app
on the owner's own robot, read-only flows only. The raw logs stay on the owner's machine: they hold the serial, home
coordinates, map geometry, Wi-Fi names and passwords, and device identifiers such as a SIM ICCID, some inside
compressed payloads. Anything shared (in an issue, a doc or a test) is decoded first and has all of those removed,
not only serials and passwords:

- the MTU the app requests and the one agreed, and the real chunk size (the static read has `mtu - 120`, or `mtu`
  when it is 100 or less, but its literals may be tagged integers)
- the model code in the serial that selects the "Yarbo Rover" or "Yarbo Mini" name prefix, and which code is which
- whether pairing or bonding is required (any SMP PDUs), and whether a bound robot accepts a connection without the
  pairing-mode power-button hold
- the characteristic's GATT properties (write without response, notify)
- the module version at which zlib starts (the static read has 1.1.43), and what `ble_version` reports
- the idle stream (topics and rate) and a multi-chunk reply such as `get_map`, to confirm reassembly
- real range and the link-loss behaviour

**Write captures** (needed only before BLE map writes, and only as 0004 requires: authorised, one harmless test
object, read back): JSON versus protobuf for the five topics above, and whether the robot also accepts JSON for them.
Read-only flows cannot show this.

## Enforced by
Nothing yet, because this ADR is a proposal. When it is accepted, `app/link.py` and `app/control.py` (`HARD_BLOCKED`,
`CATALOGUE`) must hold the guards for the BLE route, and `tests/test_guards.py` and `tests/test_cloud_only.py` change
in the same PR, each change explained against 0007, 0002 and 0004.
