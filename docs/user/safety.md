# Safety
<!-- order: 2 -->

The panel watches by default. It can act on the robot only through its switches, all **off** on a new install, all
in [Settings → Safety](#settings/safety).

## The switches

- **Control** lets the panel start, pause, resume, stop and send the robot home. Switching it on means typing
  ENABLE, and it is logged in Events.
- **Map writing** lets Map Studio send no-go zones, pathways, area and no-vision zone edits and pathway mow
  settings to the robot. Also typed ENABLE, also logged.
- **Assistant commands** lets a connected AI assistant ([MCP](#help/mcp)) send the same commands as Control, when
  you ask it. Control must be on too, and an assistant can never override a check.

With control and map writing off, nothing the panel does can move the robot or change its map. Guards (below)
still notify.

## What every command passes

Only five run commands exist: start a plan, pause, resume, stop, return to dock; the sixth only switches the robot's
cameras on, and only for a person at the panel. **Manual driving is blocked for good.**
Each one is checked, in order:

1. It is one of those commands.
2. Control is on.
3. The robot is online (data under 2 minutes old) and the panel can reach it: on your network, or through the
   Yarbo cloud as a fallback.
4. For a start, also: the plan exists, no active error, enough battery, not already running, the yard is clear on
   the cameras, the phone app is not driving it, the lawn is dry, and someone is home.
5. For a start or a resume: no map, settings or Wi-Fi change is being written to the robot (a Wi-Fi join can take
   a minute). Pause, stop and return to dock never wait.

Hard checks can never be overridden. A wet lawn is a soft check you can override by hand; the scheduler cannot.
Every attempt, sent or refused, is in [Events](#events) with the checks it passed or failed.

## Guards during a run

Every 5 seconds while it mows: a person or pet on a yard camera, rain starting, or everyone leaving home can pause
it, dock it, or just notify ([Settings → Run rules](#settings/rules)). With control off they only notify.

**Two people rules:** *Yard must be clear to start* (Before a run starts) only stops a start. The *person guard*
(During a run) acts on a mow that is already running. Switching one off leaves the other as it is; Run rules says
so when only one is on, and a guard's alert and notice name the rule that acted.

**Your resume wins:** when you resume (from the panel or the app) while a guard is holding the robot for a person,
that guard leaves it alone until the detection ends; it applies again the next time someone appears.

**After dark:** a plan that stopped to charge heads back out by itself as soon as the battery is full, even at
night. When that happens between sunset and sunrise at your yard (worked out from the robot's map), the panel sends
it home again (Return to dock, the default) or just notifies you. Not when you sent it out yourself, and not when
it left before sunset. On a real mow, Return to dock ended the plan; the next schedule window then starts it again
in the morning, once the run rules pass. If a plan stays open instead, every try to go out before sunrise is sent
home too.
Resuming after a guard is off unless you switch it on, because resuming spins the blades.

## Map writes

The robot must be docked and idle. Every write is read back and compared; on any mismatch everything sent is undone.

A start and a write from this panel never overlap. A start or resume waits for a write in progress to finish. In the
other direction, after a start or resume goes out, writes wait until the robot shows the plan running (it can take a
few seconds), or for 3 minutes if it never does. Just before a write sends its first command, the panel also asks
again that no plan is running, since reading the map first can take a while. A write refused this way says so and
sends nothing. Once a write has started it is never cut short, and putting things back after a failed write is never
held back. Starts from the Yarbo app aren't covered: the panel only knows about them once the robot reports the plan.
