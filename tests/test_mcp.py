"""The MCP server for AI assistants: which tools exist, that writes wait for confirm=true, that the safety switches,
guard auto-resume and auto-start can never be set by an assistant, that commands need their own switch and go
through the Controller as actor "mcp" with no override, that coordinates never reach an assistant, and that /mcp is
off without a token and refuses a wrong one. The REST layer is faked; no robot, no network."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette

from app import mcp_server
from app.store import Store


class FakeController:
    def __init__(self):
        self.sent = []
        self.previewed = []

    async def preview(self, cmd, args, actor="user"):
        self.previewed.append((cmd, args, actor))
        return {"cmd": cmd, "would_send": True, "reason": "ok", "checks": []}

    async def send(self, cmd, args=None, actor="user", override=False, wait=True):
        self.sent.append((cmd, args, actor, override, wait))
        return {"sent": True, "cmd": cmd, "checks": [], "lat": 43.0}


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path))
    calls = []
    replies = {}

    async def api(method, path, json=None, params=None, raw=False):
        calls.append((method, path, json))
        return replies.get((method, path), {"ok": True})

    state = SimpleNamespace(rec=SimpleNamespace(store=store), ops=SimpleNamespace(controller=FakeController()))
    server = mcp_server.PanelMCP(api=api, state=lambda: state)
    return SimpleNamespace(server=server, store=store, calls=calls, replies=replies, state=state)


def call(env, name, **args):
    """A tool call as an MCP client makes it; returns the structured result."""
    out = asyncio.run(env.server.mcp.call_tool(name, args))        # a tool's ToolError is raised from here
    data = out.structured_content
    if data is not None:
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(out.content[0].text)


def names(env):
    return {t.name for t in asyncio.run(env.server.mcp.list_tools())}


def test_tools_cover_reading_setup_and_control_but_nothing_forbidden(env):
    have = names(env)
    for want in ("status", "readiness", "runs", "lawn_conditions", "schedule", "settings", "help_page", "imagery_where",
                 "imagery_discover", "imagery_use", "imagery_align", "set_integration", "set_setting",
                 "set_schedule_windows", "robot_command", "setup_status"):
        assert want in have
    for banned in ("cmd_vel", "map_apply", "draft_apply", "restore_backup", "cloud_login", "set_control",
                   "imagery_try", "imagery_save_source"):              # sending the yard somewhere new is the user's
        assert not any(banned in n for n in have)


def test_an_assistants_start_counts_every_unknown_as_a_fail():
    from app import control
    ctx = {"snapshot_age_s": 5, "route_state": "connected", "error_code": None, "plans": [{"id": 1}], "battery": 90,
           "running": False, "yard": {"ok": None, "detail": "Frigate unreachable"}, "phone_app_active": None,
           "conditions": None, "presence": None, "writing": False}
    ok_user, _ = control.verdict(control.check("start_plan", {"plan_id": 1}, ctx, control.GATE_DEFAULTS, "user"), False)
    ok_mcp, why = control.verdict(control.check("start_plan", {"plan_id": 1}, ctx, control.GATE_DEFAULTS, "mcp"), False)
    assert ok_user and not ok_mcp
    known = {**ctx, "yard": {"ok": True, "detail": "clear"}, "phone_app_active": False, "conditions": "Dry",
             "presence": True, "writing": False}
    assert control.verdict(control.check("start_plan", {"plan_id": 1}, known, control.GATE_DEFAULTS, "mcp"), False)[0]


def test_text_and_number_lists_lose_places_too():
    out = mcp_server.scrub({
        "error": "GET https://img.example/ImageServer/exportImage?bbox=-8515000.1,4718000.2 failed",
        "tile": "https://t.example/tile/20/123456/234567.jpg timed out",
        "note": "at 38.978912, -76.491123", "where2": [-76.491123, 38.978912],
        "box": [[-76.49112, 38.9784], [-76.4901, 38.9795]],
        "probe": {"ok": True, "native_m": 0.0762, "px": [512, 512]}, "sn": "YB0000DEMO0003", "ref": [1.5, 2.5],
        "with_alt": [-76.491123, 38.978912, 0], "tile2": "https://t.example/20-123456-234567.png failed",
        "event": "robot YB0000DEMO0003 at 192.0.2.41:1883; cloud user user@example.com",
        "entry": {"url": "https://ok.example/ImageServer?bandIds=0,1,2"}})
    text = json.dumps(out)
    assert "8515000" not in text and "123456" not in text and "38.97" not in text and "76.49" not in text
    assert "YB0000DEMO" not in text and "ref" not in out and "with_alt" not in out and "user@" not in text
    assert out["probe"] == {"ok": True, "native_m": 0.0762, "px": [512, 512]}
    assert out["entry"]["url"].endswith("bandIds=0,1,2")                     # a pasted draft goes back intact


def test_read_results_carry_no_coordinates(env):
    env.replies[("GET", "/api/status")] = {"state": "docked", "lat": 39.0, "robot": {"lon": -76.5, "battery": 80,
                                           "position": [1, 2]}, "runs": [{"bbox": [0, 0, 1, 1], "id": 3}]}
    out = call(env, "status")
    assert out == {"state": "docked", "robot": {"battery": 80}, "runs": [{"id": 3}]}


def test_writes_wait_for_confirm(env):
    out = call(env, "imagery_use", source_id="usgs_naip")
    assert out["needs_confirm"] and not [c for c in env.calls if c[0] != "GET"]
    call(env, "imagery_use", source_id="usgs_naip", confirm=True)
    assert ("PUT", "/api/imagery/source", {"provider": "usgs_naip"}) in env.calls


@pytest.mark.parametrize("url", ["http://127.0.0.1:8425/x", "http://10.0.0.4/arcgis", "http://localhost/x",
                                 "file:///etc/passwd", "http://[::1]/x"])
def test_discovery_never_reaches_into_the_lan(env, url):
    with pytest.raises(ToolError):
        call(env, "imagery_discover", url=url)
    assert not env.calls


@pytest.mark.parametrize("key", ["control_enabled", "map_write_enabled", "mcp_control_enabled", "imagery_alignment",
                                 "conditions", "webhook_url", "GATES"])
def test_switches_are_not_settable(env, key):
    with pytest.raises(ToolError):
        call(env, "set_setting", key=key, value=True, confirm=True)
    assert not env.calls


GATES = {"min_battery_pct": 30, "require_dry": True, "allow_damp": False, "presence_required": True,
         "yard_clear_minutes": 10, "yard_labels": ["person", "dog"], "phone_app_quiet_s": 60, "yard_clear_required": True,
         "guard_person": "pause", "guard_rain": "dock", "guard_everyone_left": "notify", "guard_resume": False,
         "guard_resume_after_min": 5}


@pytest.mark.parametrize("change", [{"guard_resume": True}, {"min_battery_pct": 20}, {"require_dry": False},
                                    {"yard_clear_required": False}, {"allow_damp": True}, {"guard_person": "ignore"},
                                    {"guard_rain": "notify"}, {"yard_labels": ["person"]}, {"yard_clear_minutes": 1},
                                    {"made_up": 1}])
def test_run_rules_cannot_be_loosened(env, change):
    env.replies[("GET", "/api/settings")] = {"gates": GATES}
    with pytest.raises(ToolError):
        call(env, "set_setting", key="gates", value=change, confirm=True)
    assert all(c[0] == "GET" for c in env.calls)


def test_run_rules_can_be_tightened(env):
    env.replies[("GET", "/api/settings")] = {"gates": GATES}
    change = {"min_battery_pct": 40, "guard_everyone_left": "dock", "yard_labels": ["person", "dog", "cat"],
              "guard_person": "dock", "require_dry": True}
    call(env, "set_setting", key="gates", value=change, confirm=True)
    assert env.calls[-1] == ("PUT", "/api/settings/gates", change)


def test_yard_cameras_only_grow_and_only_with_cameras_frigate_has(env):
    env.replies[("GET", "/api/settings")] = {"yard_cameras": ["front"]}
    env.replies[("GET", "/api/frigate/cameras")] = {"cameras": ["front", "back", "side"]}
    for value in ([], ["back"], ["front", "made_up"], ["front"] + [f"c{i}" for i in range(40)]):
        with pytest.raises(ToolError):
            call(env, "set_setting", key="yard_cameras", value=value, confirm=True)
    call(env, "set_setting", key="yard_cameras", value=["front", "back"], confirm=True)
    assert [c[1] for c in env.calls if c[0] == "PUT"] == ["/api/settings/yard_cameras"]


@pytest.mark.parametrize("key,value", [("presence_entity", "sun.sun"), ("camera_polygons", {"x": [[0, 0]]}),
                                       ("ha_notify_service", "script.open_garage"),
                                       ("ha_notify_service", "shell_command.x"), ("deck_m", float("nan"))])
def test_settings_that_could_blind_a_check_or_call_anything_are_refused(env, key, value):
    with pytest.raises(ToolError):
        call(env, "set_setting", key=key, value=value, confirm=True)
    assert all(c[0] == "GET" for c in env.calls)


@pytest.mark.parametrize("change", [{"phone_app_quiet_s": float("nan")}, {"yard_clear_minutes": float("inf")},
                                    {"yard_labels": ["person", "dog", "toaster"]}, {"guard_rain": "pause"}])
def test_run_rules_reject_non_numbers_unknown_labels_and_softer_guards(env, change):
    env.replies[("GET", "/api/settings")] = {"gates": GATES}
    with pytest.raises(ToolError):
        call(env, "set_setting", key="gates", value=change, confirm=True)


def test_polling_can_be_set(env):
    assert call(env, "set_integration", group="panel", values={"poll_idle_s": 120})["needs_confirm"]
    call(env, "set_integration", group="panel", values={"poll_idle_s": 120}, confirm=True)
    assert env.calls == [("PUT", "/api/integrations/panel", {"poll_idle_s": 120})]


@pytest.mark.parametrize("group,values", [("robot", {"host": "10.0.0.9"}), ("frigate", {"url": "http://evil"}),
                                          ("panel", {"url": "http://evil"}), ("home_assistant", {"token": False}),
                                          ("home_assistant", {"url": "http://evil", "token": "x"}),
                                          ("weather", {"tempest_port": 0}), ("panel", {"poll_idle_s": None}),
                                          ("panel", {"poll_idle_s": False}), ("../settings/gates", {"guard_resume": True})])
def test_integrations_that_could_mislead_a_check_or_leak_a_token_are_refused(env, group, values):
    with pytest.raises(ToolError):
        call(env, "set_integration", group=group, values=values, confirm=True)
    assert not env.calls


def test_auto_start_windows_are_the_users(env):
    a = {"id": "a", "auto_start": True, "days": [1], "start": "09:00"}
    env.replies[("GET", "/api/schedule")] = {"windows": [a, {"id": "b", "auto_start": False}]}
    for bad in ([{"id": "b", "auto_start": True}], [{**a, "start": "03:00"}], [{**a, "auto_start": False}],
                [a, a], [{"id": "new", "auto_start": True}]):
        with pytest.raises(ToolError):
            call(env, "set_schedule_windows", windows=bad, confirm=True)
    call(env, "set_schedule_windows", windows=[a, {"id": "c"}], confirm=True)
    assert env.calls[-1][1] == "/api/schedule/windows"
    call(env, "set_schedule_windows", windows=[{"id": "c"}], confirm=True)          # leaving it out removes it


def test_commands_need_their_own_switch_and_are_logged_when_refused(env):
    with pytest.raises(ToolError, match="Settings → Safety"):
        call(env, "robot_command", command="pause", confirm=True)
    assert env.state.ops.controller.sent == []
    log = env.store.control_log(5)
    assert log and log[0]["actor"] == "mcp"


def test_a_command_goes_through_the_controller_as_mcp_without_override(env):
    env.store.set_setting(mcp_server.SWITCH, True)
    assert call(env, "robot_command", command="start_plan", plan_id=1)["needs_confirm"]
    out = call(env, "robot_command", command="start_plan", plan_id=1, confirm=True)
    assert env.state.ops.controller.sent == [("start_plan", {"plan_id": 1}, "mcp", False, "background")]
    assert "lat" not in out
    with pytest.raises(ToolError):
        call(env, "robot_command", command="cmd_vel", confirm=True)


def test_an_assistant_cannot_switch_the_cameras_on(env):
    env.store.set_setting(mcp_server.SWITCH, True)
    with pytest.raises(ToolError, match="not a command"):
        call(env, "robot_command", command="smart_vision_control", confirm=True)
    assert env.state.ops.controller.sent == []


@pytest.mark.parametrize("tool,args", [
    ("maintenance_done", {"item_id": "../schedule/pending/confirm?", "confirm": True}),
    ("maintenance_done", {"item_id": "x/../../schedule/pending/confirm", "confirm": True}),
    ("help_page", {"page_id": "../track/points"}),
    ("help_page", {"page_id": "..%2Fsnapshot"}),
    ("imagery_sample", {"source_id": "../../map/raw?"}),
    ("test_integration", {"group": "../mcp/token?"}),
    ("imagery_use", {"source_id": "../x", "confirm": True})])
def test_no_argument_can_steer_a_call_to_another_endpoint(env, tool, args):
    with pytest.raises(ToolError):
        call(env, tool, **args)
    assert not env.calls


def test_only_listed_routes_are_open():
    for method, path in [("POST", "/api/schedule/pending/confirm"), ("POST", "/api/mcp/token"),
                         ("PUT", "/api/settings/control_enabled"), ("PUT", "/api/settings/gates/../control_enabled"),
                         ("GET", "/api/track/points"), ("GET", "/api/map/raw"), ("POST", "/api/studio/draft/apply"),
                         ("PUT", "/api/integrations/robot"), ("POST", "/api/cloud/logout")]:
        assert not mcp_server.allowed(method, path), path
    assert mcp_server.allowed("GET", "/api/runs/12") and mcp_server.allowed("PUT", "/api/settings/gates")


def test_readiness_previews_as_the_assistant_and_says_when_its_switch_is_off(env):
    env.replies[("GET", "/api/plans")] = {"plans": [{"id": 7}]}
    out = call(env, "readiness")
    assert env.state.ops.controller.previewed == [("start_plan", {"plan_id": 7}, "mcp")]
    assert out["would_send"] is False and "Safety" in out["reason"]
    env.store.set_setting(mcp_server.SWITCH, True)
    assert call(env, "readiness")["would_send"] is True


def test_readiness_with_no_plans_says_so(env):
    env.replies[("GET", "/api/plans")] = {"plans": []}
    assert call(env, "readiness")["would_send"] is False


def test_imagery_where_names_regions_not_places(env):
    env.replies[("GET", "/api/imagery/sources")] = {"sources": [
        {"id": "md", "region": "Maryland", "country": "US", "covers": True, "needs_key": None},
        {"id": "esri", "region": "Worldwide", "country": "XX", "covers": True, "needs_key": "a key"},
        {"id": "nl", "region": "Netherlands", "country": "NL", "covers": False}]}
    out = call(env, "imagery_where")
    assert out["regions"] == ["Maryland"] and out["countries"] == ["US"] and out["free_sources_here"] == ["md"]


def test_tokens_are_stored_as_a_hash(env):
    assert mcp_server.authorised(env.store, "Bearer x") is None              # off until a token exists
    token = mcp_server.new_token(env.store)
    assert token not in json.dumps(env.store.setting(mcp_server.TOKEN_SETTING))
    assert mcp_server.authorised(env.store, f"Bearer {token}") is True
    assert mcp_server.authorised(env.store, "Bearer nope") is False
    assert mcp_server.authorised(env.store, None) is False
    assert mcp_server.token_status(env.store)["enabled"]


def test_the_endpoint_is_off_without_a_token_and_needs_the_right_one(env):
    app = Starlette(routes=env.server.routes())

    async def go():
        async with env.server.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as c:
                async def post(headers):
                    return await c.post("/mcp", headers={"Accept": "application/json, text/event-stream", **headers},
                                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                off = await post({})
                token = mcp_server.new_token(env.store)
                wrong = await post({"Authorization": "Bearer wrong"})
                right = await post({"Authorization": f"Bearer {token}"})
                get = await c.get("/mcp", headers={"Accept": "text/event-stream", "Authorization": f"Bearer {token}"})
                return off, wrong, right, get

    off, wrong, right, get = asyncio.run(go())
    assert off.status_code == 404 and wrong.status_code == 401
    assert get.status_code == 405, "a GET would open a standing stream that a revoked token would not close"
    assert right.status_code == 200, right.text
    tools = {t["name"] for t in right.json()["result"]["tools"]}
    assert "status" in tools and "robot_command" in tools


def test_an_assistants_resume_faces_the_full_checks():
    from app import control
    ctx = {"snapshot_age_s": 5, "route_state": "connected", "error_code": None,
           "yard": {"ok": None, "detail": "unknown"}, "phone_app_active": None, "conditions": "Dry", "presence": True, "writing": False}
    names = lambda actor: {c["name"] for c in control.check("resume", {}, ctx, control.GATE_DEFAULTS, actor)}  # noqa: E731
    assert "Yard clear (Frigate)" in names("mcp") and "Yard clear (Frigate)" not in names("user")
    ok, _ = control.verdict(control.check("resume", {}, ctx, control.GATE_DEFAULTS, "mcp"), override=False)
    assert not ok                                              # an unknown yard blocks an assistant's resume


def test_an_assistant_cannot_make_the_after_dark_guard_a_pause():
    cur = {"guard_after_dark": "notify"}
    assert mcp_server.tighter_gates(cur, {"guard_after_dark": "pause"})
    assert mcp_server.tighter_gates(cur, {"guard_after_dark": "dock"}) == []
    assert mcp_server.tighter_gates({"guard_after_dark": "dock"}, {"guard_after_dark": "notify"})
