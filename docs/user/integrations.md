# Integrations
<!-- order: 88 -->

All optional. Each is set in Settings and has a Test button.

- **Tempest weather station** ([Lawn conditions](#settings/lawn)): read from the hub's local broadcast on UDP
  50222. No account or cloud. Feeds Dry / Damp / Wet. When the hub hears several stations, pick yours. Nothing
  heard? The panel must be on the hub's own network (not a guest network or another VLAN), and in Docker the
  container needs host networking (`network_mode: host`): broadcasts don't reach Docker's bridge network.
- **Weather from Home Assistant** ([Lawn conditions](#settings/lawn)): rain (a rate, a running total, an on/off
  rain sensor, or a weather entity's rainy or not), temperature (°C, °F or K) and humidity sensors can stand in for
  the Tempest or back it up; pick the source per input (Either takes the Tempest while it reports). Each input is a
  list of the HA entities that fit it (matched by HA's device class, likely ones first, with what each reads now);
  "Show all sensors" adds the rest, and "Type an id…" takes anything else. **Soil moisture** sensors (Zigbee2MQTT ones arrive
  in HA as entities) hold the lawn at Damp or Wet above the levels you set, however long ago it rained.
- **Frigate** ([Cameras](#settings/cameras)): tick the cameras that see the yard and draw what each covers. They
  feed the yard-clear check, the person and pet guard, and the follow-cam.
- **Home Assistant** ([Home Assistant](#settings/ha)): with an address and a long-lived token the panel reads
  presence and publishes `sensor.yarbo_panel_*` entities every minute. In Docker, use HA's IP address: `.local`
  names often do not resolve inside a container, and Test says so. Presence is picked from a list: an alarm panel
  (armed away or on vacation = nobody home), `zone.home` (people in it), or a person, tracker or group of them.
- **Notifications** ([Notifications](#settings/alerts)): an HA notify service, picked from the ones your Home
  Assistant has (the companion app's phones first), and/or a webhook. Alerts: robot
  error, stuck, head changed, cloud needs sign-in, maintenance due, schedule confirmations.
- **Prometheus** ([Access](#settings/access)): switch **Metrics** on and make a [metrics-only token](#settings/access),
  then point Prometheus at `GET /api/metrics`. It is built from what the panel already holds, so a scrape asks the
  robot, Home Assistant and Frigate for nothing, and it is safe to scrape often. It has no position (no latitude,
  longitude, local x/y or RTK height), no serial number and no robot label; heading is the only pose value.

  ```yaml
  scrape_configs:
    - job_name: groundskeeper
      metrics_path: /api/metrics
      scrape_interval: 30s
      authorization:
        credentials: gk_your_metrics_only_token      # or credentials_file: /etc/prometheus/groundskeeper.token
      static_configs:
        - targets: ["192.0.2.10:8425"]               # the panel; add scheme: https behind a reverse proxy
  ```

  With `PANEL_AUTH=off` there are no tokens: scrape without one, and only the proxy keeps others out.

  Over plain HTTP the token crosses the network unencrypted. On a shared network, put the panel behind an HTTPS
  reverse proxy and set `scheme: https` in the scrape job.

  `yarbo_*` are the robot's own values: `yarbo_battery_ratio`, `_battery_voltage_volts`, `_battery_current_amperes`,
  `_battery_temperature_celsius`, `_cpu_temperature_celsius`, `_charging`, `_error_code`, `_working_state`,
  `_activity{state}`, `_rtk_fix{quality}`, `_rtk_satellites`, `_odometry_confidence_ratio`, `_heading_degrees`,
  `_wheel_speed_metres_per_second{side}`, `_wheel_current_amperes{side}`, `_wheel_distance_metres_total{side}`,
  `_cpu_usage_ratio`, `_memory_available_bytes`, `_memory_free_bytes`, `_memory_cached_bytes`,
  `_disk_available_bytes`, `_disk_used_bytes` (the robot's computer; its process list is never read),
  `_halow_signal_dbm`, `_plan_active`, `_plan_progress_ratio` and `_plan_time_left_seconds` (the plan's own progress),
  and `_heartbeat_age_seconds`. A robot that goes quiet would leave a flat battery line, so once its last snapshot is
  older than 5 minutes the panel **drops the `yarbo_*` values** and keeps `yarbo_snapshot_age_seconds` and
  `yarbo_data_fresh` (0) to alert on. `groundskeeper_*` is the panel's own state and is always there: `_up`,
  `_cloud_connected`, `_route_connected` (the Yarbo cloud is the only route to the robot), `_robot_heard`,
  `_last_message_age_seconds`, `_robot_messages_total`, `_run_active`, `_stalled`, `_lost_contact`,
  `_guard_active{guard}`, `_last_command_result{result}`, `_last_command_age_seconds`, `_uptime_seconds` and
  `_build_info`. States are gauges with a fixed set of labels, so the number of series never grows. Example alerts:
  `groundskeeper_cloud_connected == 0`, `yarbo_data_fresh == 0`, `yarbo_battery_ratio < 0.2`.
- **API:** every endpoint with its schema is at [/api/docs](/api/docs). A program such as a Home Assistant
  integration can poll `GET /api/ha/summary` with an [API token](#settings/access): state, battery, positioning,
  lawn, the last finished run, the next schedule window, whether the robot is stuck or out of touch, and whether a
  start would go out now, and why not (checked at most every 30 s). It carries no position and no serial number: the
  robot is named by a hash of it. The [Home Assistant integration](https://github.com/Groundskeeper-robot/groundskeeper-ha)
  reads it. Its map card follows the robot with `GET /api/ha/stream` (server-sent events): every 5 s the state,
  the robot's last known position, the current run, the camera that covers it and the map's version, and each new
  position sample in between. Unlike the summary it carries the robot's position, as `/api/stream` already does for
  any API token; the integration keeps it out of Home Assistant's history.
