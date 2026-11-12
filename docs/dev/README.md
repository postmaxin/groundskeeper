# Developer documentation

These pages explain how Groundskeeper works and what is known about the Yarbo robot it talks to. Start with
[`architecture.md`](architecture.md): the modules, what each does and the API.

## Where each kind of writing goes

| Kind | Where | Changes when |
|---|---|---|
| How to use the panel | [`../user/`](../user/), served as the panel's Help | A feature changes, in the same PR |
| How it works now | This folder: the pages below | The code changes, in the same PR |
| Why it is this way | [`decisions/`](decisions/) (ADRs) | Not rewritten; a new ADR supersedes an old one |
| What is planned or unknown | GitHub issues (`research`, `capture needed`, the module labels) | All the time |
| Rules that must hold | Tests, `tests/test_guards.py` first | With the rule, and its ADR |

Detail that belongs to one module lives in that module's docstring; these pages link to it rather than copy it.

## Pages

- [`architecture.md`](architecture.md): modules, features, configuration and the API.
- [`design.md`](design.md): each subsystem's behaviour and rules (transport, recorder, Map Studio, scheduling,
  conditions, control and its interlocks, cameras, integrations, security). Its section numbers (§7.4, §10.3, …) are
  cited from the code.
- [`decisions/`](decisions/): why the panel is the way it is, one decision per record, and when a change needs one.
- [`protocol.md`](protocol.md): the robot's MQTT protocol through the Yarbo cloud (and what the local broker showed
  before firmware V3.14.4x closed it), access rules and read commands.
- [`map-writes.md`](map-writes.md): the map, area, plan, schedule and backup write commands, as captured from the
  official app.
- [`robot-states.md`](robot-states.md): the robot's state, pause and error codes, and the thresholds behind the
  Problem areas layer and the stuck alert.
- [`ui-design.md`](ui-design.md): navigation, colour tokens and chart rules.
- [`mobile-review.md`](mobile-review.md): the phone review (#52) and its open findings.
- [`imagery-catalog.md`](imagery-catalog.md): the aerial imagery catalog, its privacy rules and how to add a source.
