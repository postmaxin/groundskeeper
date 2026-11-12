# Settings
<!-- order: 80 -->

One section at a time. The dot next to each section says whether it is set up. Changes apply at once and need no
restart; a field left blank goes back to the environment's value, shown as the placeholder.

On a phone the list of sections folds into one row, **Sections**, naming the one you are in: tap it to pick another.
It is open when you go to Settings and closes once you choose a section.

| Section | What it holds |
|---|---|
| [Set up](#settings/setup) | The first-run guide: every step below in order, with what each is for ([more](#help/setup)) |
| [Robot](#settings/robot) | The Yarbo cloud sign-in and the robot's serial; deck width; the Wi-Fi survey |
| [Safety](#settings/safety) | The control and map writing switches ([more](#help/safety)) |
| [Run rules](#settings/rules) | What must be true to start, and what each guard does during a run |
| [Lawn conditions](#settings/lawn) | Weather from the Tempest and/or Home Assistant, soil moisture, units, the dry / damp / wet rules and the last 24 h |
| [Cameras](#settings/cameras) | Frigate: which cameras see the yard, and the ground each covers |
| [Map imagery](#settings/imagery) | The aerial photo under the map ([more](#help/imagery)) |
| [Home Assistant](#settings/ha) | Address and token, presence, the entities the panel publishes |
| [Notifications](#settings/alerts) | The HA notify service and/or a webhook |
| [AI assistant](#settings/assistant) | Optional: a token and connect snippets so an assistant can read the panel and help by conversation ([more](#help/mcp)) |
| [Access](#settings/access) | The admin password, signing out, and API tokens for programs such as Home Assistant |
| [This panel](#settings/panel) | Polling, retention, version, data directory, API, theme |
| [Raw data](#settings/data) | Every field the robot and the panel report |

## A suggested order

Robot first (nothing works without it). Then Map imagery, Cameras, Lawn conditions, Run rules, Notifications, and
Safety last, when you are ready to let the panel act on the robot. An [AI assistant](#settings/assistant) is
optional at any point: once connected it can read every setting and the help, and is good at finding the most
detailed aerial map of your yard.

## Signing in

The panel asks for a password before it shows anything. The first time you open it, it asks for a **setup code**
together with the password you choose. The code is printed in the panel's log (`docker logs groundskeeper`), so
someone else on your network cannot claim the panel before you do. Sessions last 30 days.

- **Change password** (Settings → Access) signs out every other browser.
- **API tokens** are for programs, such as a Home Assistant integration or a script. They send
  `Authorization: Bearer <token>`. A token can read, and can ask for robot commands while control is on, under the
  strictest checks: anything unknown blocks a start or resume, and it cannot override. It cannot change settings,
  safety switches, the map, the password or other tokens; those need you, signed in. A token is shown once. AI
  assistants use their own token, set in
  [AI assistant](#settings/assistant).
  - **A full token reaches** every read under `/api/` (the map, run tracks, positions, settings without their
    secrets and Frigate snapshots), the aerial tiles under `/tiles/` and `/imagery/`,
    and robot commands while Control is on (strict checks, never camera-on). Treat it like a key to the yard.
  - **A metrics-only token** (tick **Metrics only** when you create it) reaches `GET /api/metrics` and nothing else:
    every other locked path (`/api/`, `/tiles/`, `/imagery/`, including the live stream and the command routes)
    answers 403. Open paths such as `/health` and the sign-in page answer as they do for anyone. Use
    this kind for Prometheus: the token sits in a config file or a secret, and a leak shows no more than the
    metrics. Until the metrics endpoint is added to the panel, such a token can read nothing at all. Tokens made
    before this option stay full.
- **Forgot the password?** Run `docker exec groundskeeper python -m app.auth reset` and restart the container.
  This also removes every session and API token. The log then shows a new setup code.
- **Behind a reverse proxy** (for https or a hostname): set `PANEL_URL` to the address people open, so changes
  from the pages are accepted. Also set `FORWARDED_ALLOW_IPS` to the proxy's address, so the limit on wrong
  passwords counts each visitor rather than the proxy as a whole.
- **Other web apps on the same host:** browsers send the panel's sign-in cookie to every port of that host. If you
  run apps there that you don't trust, give the panel its own hostname.
- `/health` stays open for Docker and uptime checks. It reports the version, whether the robot is reachable and
  whether it is busy mowing.
- **Version** ([This panel](#settings/panel) → Running): a pre-release image says which it is, for example
  `0.19.7 · nightly · 1a756d0` (the version, the channel, the commit). A release shows only the version and commit.
- **Proxy that signs people in itself?** `PANEL_AUTH=off` turns the panel's own sign-in off. Never
  do this when anyone else on the network can reach the panel: it would let them start the robot.
  If the proxy is there for single sign-on, use the panel's own OpenID Connect sign-in instead (below).

### Single sign-on (OpenID Connect)

The panel can let people sign in through an OpenID Connect provider you already run, such as Authentik, Keycloak
or Authelia, or a hosted one like Google. It works like Grafana's generic OAuth sign-in, and the settings are named
after Grafana's. Set them in the panel's environment (`docker run -e` or compose; keep the
secret out of any file you share). They are read from the environment only, on purpose: they are not on the
Settings page, so nobody who is signed in can widen who may sign in:

| Setting | What it is |
|---|---|
| `OIDC_ISSUER` | The provider's issuer address (https). The panel reads its `/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The client you registered at the provider. |
| `OIDC_ALLOWED_GROUPS` | Comma-separated groups that may use the panel, read from the `groups` claim (`OIDC_GROUPS_CLAIM` to rename). |
| `OIDC_ALLOWED_DOMAINS` | Comma-separated email domains that may use the panel. The provider must say the email is verified. |
| `OIDC_NAME` | The button label, "Sign in with …". |
| `OIDC_SCOPES` | Default `openid profile email`. Add the scope your provider needs for groups. |
| `OIDC_AUTO_LOGIN` | `on` sends the sign-in page straight to the provider. |
| `PANEL_PASSWORD` | `off` turns the password off once single sign-on works: no password form, no setup code. |
| `PANEL_URL` | Required: the address people open, with no path (the panel serves itself from the root). The provider sends them back to `PANEL_URL/api/auth/oidc/callback`, so register that as the client's redirect URI. |

- **Who gets in:** only people in an allowed group or domain, and everyone who gets in has full use of the panel,
  as with the password. Unlike Grafana, the panel refuses to start with single sign-on on and neither
  `OIDC_ALLOWED_GROUPS` nor `OIDC_ALLOWED_DOMAINS` set: it would let every account at the provider drive the robot.
- **Groups are the safer choice.** An allowed domain trusts the provider's word that the email is verified. A
  provider that lets people type their own email, or brokers other providers, can make that word cheap; with
  Google, an allowed domain also takes any Google account that has an address there.
- **Open it at `PANEL_URL`.** Signing in from another address (an IP, another name) first moves you to `PANEL_URL`,
  because that is where the provider sends you back.
- **Sessions** are the same 30-day sessions the password gives. Signing out ends the panel's session, not the
  provider's. The event log names who signed in, and anyone turned away.
- **Locked out because the provider is down or wrong?** This can only happen once you turned the password off.
  Remove `PANEL_PASSWORD=off` from the panel's environment (or set it to anything else) and restart the panel: the
  password form is back, and your old password works again. If you never set one, the panel's log
  (`docker logs groundskeeper`) shows a one-time setup code, as on a first install. If the panel won't start at all,
  its log says which `OIDC_*` setting it refused: fix it, or remove `OIDC_ISSUER` as well to turn single sign-on
  off. Leave `PANEL_PASSWORD` unset until single sign-on works, then turn it off.

## Wi-Fi survey

**When to use it.** It is off unless you turn it on, and most yards never need it. Turn it on when the robot loses
its link in parts of the yard: "No data from Yarbo" during runs, gaps in the track, or weak-positioning spots that
[Problem areas](#help/live) blame on the link (HaLow at −77 dBm or weaker, corrections late). It maps which of your
Wi-Fi networks the robot can hear, and how well, across the whole yard. That shows where the signal fades, where an
access point or a mesh node would help, and whether a network the robot has joined reaches the weak spots at all.
Run it once with **Next run only**, look at Signal → Wi-Fi, and switch it off again unless you are tracking a change
(say, a new access point). It only reads: the robot is never asked to join or leave a network.

**How it works.** Settings → Robot → **Wi-Fi survey**. While a run is driving, the panel asks the robot every 30 s which Wi-Fi
networks it hears, and stores each one's signal where the robot was; the Signal map's **Wi-Fi** shows it. It does not
ask while the robot is parked mid-plan, and with the survey off nothing is kept. Each ask makes the robot scan for
about 4 seconds, so **Next run only** comes first: it surveys one run, writes a report to [Events](#events) (answers,
how long they took, and pauses in the robot's messages during a scan and otherwise), then switches itself off. An
answer that takes more than 12 seconds is counted but not put on the map: the robot has moved on by then.
**Every run** keeps it on. A run with under 10 minutes of driving does not count as the test. Survey data older than
90 days is deleted.

## Yarbo cloud sign-in

You type your Yarbo password in Settings → Robot. It goes straight to Yarbo's own SDK and is never stored or
logged; only the session is kept on the server. The panel needs it: it reads and commands the robot only through
the Yarbo cloud, as the official app does. (Firmware V3.14.4x closed the robot's own broker on your network.)

The cloud needs the robot's serial. You never type it: the panel asks your Yarbo account which robots it has and
uses the only one (several: the card lists them, press **Use this** on yours). The serial found is kept, so a restart
still knows the robot. While none is known the card says **waiting for the robot's serial**: sign in to the Yarbo
account the robot is on.

### Robot video

Settings → Robot → **Robot video** is **off** on a new install. When you switch it on (the panel asks you
to confirm), a signed-in person can ask the panel for the join details of one of the robot's cameras. **Your browser
then connects to Agora's real-time network to play the stream**; the video never passes through the panel. It uses the
Yarbo cloud sign-in above, so that must be connected. Only a person signed in to the panel can switch it on or ask
for a camera: API tokens and AI assistants cannot, and the stream's key is never written to the logs or Events. The
address of the cloud's video service is built in, so there is nothing to set up. It can be replaced only by the
`YARBO_ROBOT_SERVICE_URL` environment variable (it must start with `https://`), never in Settings, because the panel
sends your Yarbo session to it; Settings only shows "configured" or "not configured". With `PANEL_AUTH=off`
anyone who can reach the panel gets the video too. The video plays in the **Cameras** tile on [Live](#help/live).
While that tile is open it also sends the robot a keep-awake every 4 seconds (as the Yarbo app does while open,
since the robot streams only then): this needs **Control** on, lasts at most 15 minutes per press of **Open**, and
stops when the tile is closed (a beat already on its way can still arrive just after). The stream's secret is sent
unwrapped, in the join message and the access-point request, inside TLS to Agora (whose servers decrypt the stream
for the browser anyway), because it is longer than Agora's Web SDK can wrap; a stopgap pending an Agora fix
(AgoraIO/agora-rtc-web#15). Allow `*.agora.io` and `*.sd-rtn.com` in ad blockers, or the tile cannot connect.
The panel asks the SDK not to send Agora usage statistics or upload logs, and switches off its remote configuration;
a network check while video played saw only Agora's gateway, edge and media servers. Agora still sees your
browser's address, the panel's address (the `Origin` and `Referer` of the SDK's requests) and the camera's
channel name, which carries the robot's serial number.
