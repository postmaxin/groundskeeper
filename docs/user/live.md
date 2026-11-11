# Live
<!-- order: 10 -->

The robot now: where it is, what it is doing, and the last 24 hours of its track.

**Map / Camera** (beside the title) switches the layout; this browser remembers the choice. **Camera** puts the
follow-cam where the map was, with the camera chips top left, the yard check top right and the robot's state along
the foot; the map becomes a small card in the panel. The panel then starts with the emergency buttons:
a big red **Pause** (it turns into **Resume** while the plan is paused), with **Stop** and **Return to dock** under
it, and the small map right under them. Each needs two taps: the first arms it for 4 seconds (it turns amber and says "Tap again"), the second sends. They go through the same checks as the
Control card and need control on. Drawing camera coverage and aligning the imagery switch back to the big map while
they last. When no camera's coverage holds the robot (it has driven out of every camera's view, or no position yet),
the big slot shows the map again and the camera comes back when the robot drives into view; both changes wait 10 s so
one stray position does not flip the picture. Pinning a camera with its chip keeps the camera up regardless.

On a phone the **Map** layout's panel sits under the map, so while a run is going it starts with the same emergency
buttons; the Control card's own buttons are a screen further down. Docked or idle, the panel starts with the status
as before.

The bar at the top of every page: problems first, tinted in their colour (a robot error, a guard, a run waiting
for you). Then the switches, **Control** (and **Map writing** in Map Studio): click to switch one off; switching it
on asks you to type ENABLE. Then the lawn. At the right, the connections in one pill (Cloud, Broker, RTK; on a
narrower screen just the word and its dot, with the detail when you hover) and, at the far right, the robot: its
state and battery. Each part opens where you can act on it (battery and RTK → Health, lawn → Lawn conditions, cloud
and broker → Settings → Robot). On a phone or a tablet the connections have a row of their own; while every one is fine they
shrink to a single **Links ok** chip beside the theme button (it opens Settings → Robot), and as soon as one is not,
they are all spelled out again, and the ones that aren't fine say why. A grey RTK on the dock counts as fine: no fix there is normal.

In the **Camera** layout the small map always shows the whole yard: it refits whenever its card changes size and
cannot be panned or zoomed. **Map layout** gives the full map.

Map colours: mowing areas white, no-go zones red hatched, no-vision zones blue dashed, the electric fence yellow,
pathways grey roads. The track: green RTK fixed, amber float, red-orange single point. Gaps and off-plan stretches
are pink, edge passes violet.

- **Map:** the robot (sky blue and pulsing while it works, amber when held mid-plan, with an arrowhead pointing
  where it is heading), its areas, no-go zones, pathways and today's track. **Colour** switches the track between RTK fix quality and edge passes; the legend at
  the foot of the map explains the colours.
- **Map tools** (top right): layers (imagery, zones, labels, track, cameras), Problem areas, Signal, 3D and
  Fit yard. On a phone the row keeps Imagery, Zones and Track; the rest are under **More** (tap outside it or press
  Esc to close it). **Align**, next to the imagery credit at the foot of the map, lines the aerial photo up with the
  robot's map (also in [Settings → Map imagery](#settings/imagery)).
- **3D:** tilts the map and raises the ground: the robot's own heights over the lawns (they sharpen with every mow),
  public lidar elevation (USGS 3DEP) around them. The **1× to 3×** menu next to it draws it taller so small slopes show. Right-drag, or two fingers on
  a phone, turns and tilts; the compass resets north. The same view works in Runs, for replay. In 3D the robot is a
  small 3D model of a Yarbo with the Lawn Mower Pro: it faces where it is heading, leans with its own tilt, stands on
  a soft shadow and glides between updates instead of jumping. The mower in front follows the drawn ground under it
  (the panel works that out from the ground; the robot does not report it), and the beacon on top shows the status
  colour. A single stray position is ignored; a real jump (the
  robot carried, or back after a long gap) is taken once the next update agrees. In a replay it drives the run. **Follow** (in 3D) puts the camera behind the robot and a little to one
  side and keeps it there as the robot turns; right-drag moves the camera round it, and a drag or Fit yard lets go.
- **Signal:** HaLow strength, RTK fixed share or satellites across the yard (30 days of runs, 2 m cells), or
  **Wi-Fi (survey)**: one network's signal in 4 m cells, picked from the networks the robot heard. Wi-Fi needs the
  survey on in [Settings → Robot](#settings/robot).
- **Problem areas:** a weak-positioning spot also says when HaLow was weak there (−77 dBm or lower) or corrections
  came late. A good link there points at the sky view instead: trees, walls, the house.
- **Cameras:** the robot's own cameras (front, left, right, back), not the yard cameras of the follow-cam. The tile
  is there only when [Settings → Robot → Robot video](#help/settings) is on and the video service is set. **Open**
  switches the robot's cameras on (needs Control on; with Control off it says so and plays only if the cameras are
  already on, for example from the Yarbo app) and shows one camera at a time: pick Front, Left, Right or Back. While
  the tile is open your browser also asks the panel for a **keep-awake** every 4 seconds, the same
  `set_working_state` the Yarbo app sends while it is open: the robot publishes video only while someone does. Each
  request makes the panel send exactly one, only for a signed-in person, only with Robot video and Control on, so
  closing the tile (or the tab) stops it (a beat already on its way can still arrive just after Close); the Events list notes when it starts and when it stops, not each one. The
  Yarbo app, in turn, switches the cameras off when it goes to the background, so opening the app and then leaving it
  stops the panel's video until you **Close** and **Open** the tile again. The video library needs `*.agora.io` and
  `*.sd-rtn.com` allowed in ad blockers and privacy extensions; if one blocks them, the tile shows an error. The
  state beside the title is *Connecting*, *Waiting for the robot* (cameras off or the robot asleep), *Live* or an
  error with **Try again**. An error says which step failed and a short reason, such as "Joining failed: OperationError". The video comes from Agora's network to your browser and plays only while the tile is
  open: **Close**, another page, a hidden tab or a closed page stop it. The panel never switches the cameras off,
  because that would cut the Yarbo app's view too; the robot does that itself. The panel asks Agora's SDK not to send
  usage statistics or upload logs, and switches off its remote configuration (which could turn those back on); a
  network capture of the tile in use (SDK 4.24.9) found no statistics or log traffic (issue #35). Agora's servers see the panel's address (the `Origin` and
  `Referer` of the SDK's requests) and your browser's address. The stream's secret is longer than Agora's Web SDK can
  wrap, so the panel puts it into the SDK's join message and access-point request itself, unwrapped inside the TLS connections to Agora, whose
  servers decrypt the stream for your browser anyway; this is a stopgap until Agora fixes their SDK (issue #46). The SDK also keeps a few items of its own in this
  browser's storage; the panel clears them when the SDK has loaded and when the tile leaves the channel. The demo shows
  a placeholder card ("The demo robot has no cameras"; it loads nothing from Agora). It has played the back camera during a run while the Yarbo app was also open. **Known limitations**,
  from one robot so far:
  - Waking a docked, sleeping robot is not shown to work: with the Yarbo app closed, no video arrived within about
    35 seconds. Opening the Yarbo app once, or a running plan, may be needed first.
  - Only the back camera has been seen. The front camera may depend on the head (mower, snow blower, leaf blower);
    left and right are untried.
  - The Yarbo app going to the background switches the cameras off; **Close** and **Open** the tile again.
  - Ad blockers must allow `*.agora.io` and `*.sd-rtn.com`.
  - The secret swap is a stopgap: Agora Web SDK 4.24.9 can wrap at most 62 bytes and the robot's secret is 64
    (issue #46, [AgoraIO/agora-rtc-web#15](https://github.com/AgoraIO/agora-rtc-web/issues/15)).
  - Not yet looked at on a phone.
  - Tried with one robot and one viewer at a time.
- **Control card:** pick a plan and see its areas. Only the buttons that make sense in the robot's state are on.
  Needs control switched on ([Safety](#help/safety)). Hover a button that is off to see why; on a phone or a narrow
  window a line under the buttons says it instead.
- **Ready to start:** runs every check a start would face and sends nothing. When a start would be refused it says
  why and what fixes it ("Can't start: the lawn is not dry"), with every check one click further.
- **Why it stopped:** when the robot paused itself (bumper, emergency stop, rain, a fault) or the last run ended
  before its plan finished, a card says why and what to do.
- **Pending start:** when a schedule window is ready, confirm or dismiss it here.
- **Follow-cam:** live video (5 frames a second) from the Frigate camera whose coverage contains the robot. It
  plays only while Live is on screen; if the video fails it shows a still every 3 seconds and tries again. "Camera
  layout" under it switches to the camera-first layout.
- **Lawn:** Dry, Damp or Wet, and when it should be dry.
