# 0010. Robot video through Yarbo's Agora channel, as a host that publishes nothing

- **Status:** Accepted
- **Date:** 2026-11-12
- **Links:** #23, #112, #124, #138; `docs/dev/protocol.md` (Robot video)

## Context
The robot's cameras are not served on the LAN. The robot publishes each camera to Agora, on a channel per camera, and
the official app watches with a token from Yarbo's cloud robot service. That is the only route to the video. In
Agora's live mode an audience member is invisible to the other users, and the robot publishes only while a
broadcaster is in its channel: a tile that joined as audience got video only while the app's camera page was open.
The robot also stops a moment after the last broadcaster leaves, and `{"state":0}` cuts the view for everyone
watching, the app included. The panel is often served over plain http on a home network, where the browser has no
Web Crypto, and the Agora Web SDK uses it to wrap the stream secret on every join.

## Decision
- The panel gets video only the way the app does: a token from the robot service (`video.DEFAULT_SERVICE_URL`,
  overridable by the environment, never by Settings) and the Agora channel for one camera at a time.
- The Cameras tile joins as a **host and never publishes**: no microphone, camera or track of its own.
- It only ever switches the cameras **on** (`{"state":1}`), from a person at the panel, never off. Each camera change,
  token renewal, or Open soon after a Close switches them on again, at least 3 s after the last leave.
- Over plain http the tile lends the SDK a stand-in for the one Web Crypto call its join needs, only while its
  client lives. The SDK's wrapped value is never used (the real secret goes into the join frame), and every other
  Web Crypto call is refused. Over https the browser's own is used and never replaced.

## Consequences
- Video works with the app closed, from any browser on the home network, with no change to the robot.
- The panel is a broadcaster in Yarbo's channel. Agora bills the minutes to Yarbo's App ID, which is why the tile
  watches one camera at a time and leaves when nobody is looking (hidden tab, close, leaving Live).
- Over plain http the token and stream key cross the network unencrypted; the user docs say to use https on networks
  you do not trust.
- The route depends on Yarbo's robot service, its address and Agora's live-mode behaviour; an app update can break it.
  The 3 s gap is observed, not measured. Revisit if Yarbo offers a local or documented video route, if the robot
  starts publishing to audience members, or if Yarbo objects to third-party viewers.

## Enforced by
`web/cameras.js` (`setClientRole("host")`, `CAM_REJOIN_GAP_MS`, `camerasStandInCrypto`), `app/video.py`,
`app/link.py` (`ON_ONLY`); `tests/test_cameras_js.py` `test_the_tile_joins_as_host_but_never_publishes_anything`,
`test_changing_camera_leaves_the_channel_and_switches_the_cameras_on_again`,
`test_opening_again_soon_after_a_close_waits_out_the_gap`,
`test_over_plain_http_the_sdks_wrap_gets_a_stand_in_only_while_the_client_lives`,
`test_over_https_the_browsers_own_web_crypto_is_used_and_never_replaced`; `tests/test_link.py`
`test_the_link_refuses_any_on_only_payload_but_on`.
