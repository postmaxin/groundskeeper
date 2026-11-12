# Health
<!-- order: 60 -->

Under **Robot → Health**: the robot now and over the last 24 hours, 7 days or 30 days. Hover (or touch) a chart
to read the value at that time.

- **Positioning:** fix now, share of time fixed, satellites, DOP, correction age.
  - **Correction gaps:** times the RTK corrections from the base station stopped arriving for 20 s or more, with
    what the fix fell to and the HaLow signal then. Without corrections the fix drops to DGPS or single point however
    many satellites the robot sees. With HaLow strong, look at the base station (its power, its view of the sky);
    with HaLow weak, the link is the likely cause. Each gap is also an event (*rtk_corrections_gap*) when it ends
    (one that ends while the panel is restarting is listed here but has no event).
  - The **RTK** pill in the top bar reads *no corrections* in amber while they are more than 10 s old, docked too.
    None at all (-1) is normal on the dock and doesn't count.
- **Link:** the connection through the Yarbo cloud (messages and read requests) and HaLow over time.
- **Network:** each link's strength now, and the robot's Wi-Fi.
  - **HaLow** in dBm: amber below −77, red at −82 (where the robot switches to 4G).
  - **4G:** the values the robot reports, as it reports them; "not reported" when they are all zero.
  - **Wi-Fi:** the network it is on and its signal (0–100 %), from the latest scan by the panel or the phone app.
  - **Scan** asks the robot for nearby networks (a few seconds; up to a minute on a weak link). Not during a plan.
  - **Join** a network from the list with its password, or **Forget** a saved one. Open networks can't be joined
    from here yet. Both need
    map writing on (Settings → Safety) and the robot on its charger with no plan. The panel then asks the robot
    which network it is on, or scans again, and says whether it worked. A forgotten network needs its password to
    join again. Changing Wi-Fi can drop the robot's connection for a moment.

The top bar's connections pill shows the same as **signal bars**: HaLow (4 bars from −60 dBm, none at the 4G
level), Wi-Fi when the robot is on it (from the latest scan), and GPS (RTK fixed 4, or 3 with a high heading DOP;
float 2; single 1; no fix 0).
- **Power:** charge, voltage, cell temperatures.
- **Deck and CPU.**
- **Firmware history** and **error codes**, with a dictionary of the robot's status codes.
