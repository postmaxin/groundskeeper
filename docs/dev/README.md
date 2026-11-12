# Developer documentation

These pages explain how Groundskeeper works and what is known about the Yarbo robot it talks to. Start with
[`architecture.md`](architecture.md): the modules, what each does and the API.

[`design.md`](design.md) is the design document: architecture, transports, recorder, Map Studio, scheduling,
control and its interlocks, and the safety rationale; its section numbers (§7.4, §10.3, …) are cited from the code.
[`protocol.md`](protocol.md) is the reference for the robot's MQTT protocol through the Yarbo cloud (and what the
local broker showed before firmware V3.14.4x closed it), access rules and read commands.
[`map-writes.md`](map-writes.md) gives the map, area, plan, schedule and backup write commands as captured from the
official app. [`robot-states.md`](robot-states.md) lists the robot's state, pause and error codes and the thresholds
behind the Problem areas layer and the stuck alert. [`ui-design.md`](ui-design.md) holds the navigation,
colour-token and chart rules, [`mobile-review.md`](mobile-review.md) the phone review (#52) and its open findings,
and [`imagery-catalog.md`](imagery-catalog.md) describes the aerial imagery catalog, its privacy rules and how to add
a source. User-facing help lives in [`../user/`](../user/) and is served in the panel's Help.
