# Setting up
<!-- order: 1 -->

[Settings → Set up](#settings/setup) walks through these steps in order. It opens by itself on a fresh install
(not signed in to the Yarbo cloud or no map yet) and when the saved Yarbo session has expired. Each step is the
same controls as its own Settings section, so you can also skip the guide and work through the list below. A tick
means the panel already has what the step needs; nothing is stored for the guide itself. The page updates by itself
while something is on its way (the robot connecting, the cloud signing in, the imagery loading), so there is no need
to reload.

Before any of this, the first visit sets the admin password. It needs the setup code from the panel's log
(`docker logs groundskeeper`); see [Signing in](#help/settings).

1. **Yarbo cloud** ([Robot](#settings/robot)): sign in to your Yarbo account. The panel reaches the robot only
   through the cloud, as the official app does. You type your own password; it goes to Yarbo's SDK and is never
   stored. Signing in usually takes under a minute. The robot's serial comes from your account; you never type it:
   one robot is used, several are listed with **Use this**. Then the map is read on its own.
2. **Map imagery** ([Map imagery](#settings/imagery)), in two parts. *Pick the photo:* your yard's location comes
   from the robot's map, so only the sources that cover it are asked, sharpest first. *Line it up:* **Align on the
   map** opens the map to match a spot on the robot's map with the same spot in the photo; saving brings you back.
   See [Map imagery](#help/imagery).
3. **Cameras** (optional, [Cameras](#settings/cameras)): with Frigate, switch on the cameras that see the yard and
   draw what each covers.
4. **Home Assistant** (optional, [Home Assistant](#settings/ha)): the address and a long-lived token, then the
   presence entity, picked from a list. If the panel runs in Docker, use HA's IP address: `.local` names often do
   not resolve there.
5. **Weather** ([Lawn conditions](#settings/lawn)): a Tempest on your network is heard with no setup; Home
   Assistant sensors are picked from lists matched to each input.
6. **Safety** ([Safety](#settings/safety), [Run rules](#settings/rules)): the four switches, changeable right there:
   control and map writing (each switched on by typing ENABLE), yard must be clear, and resume after a guard pause.
   The recommended values are control off, map writing off, yard must be clear on and no resume; "Use the
   recommended" sets all four and sends nothing to the robot. See [Safety](#help/safety).
7. **Notifications** ([Notifications](#settings/alerts)): an HA notify service or a webhook, then send a test.
8. **AI assistant** (optional, [AI assistant](#settings/assistant)): a token so an assistant can read the panel and
   help by conversation. See [AI assistants](#help/mcp).
