from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from mcp.types import CallToolResult, TextContent, Tool
from starlette.testclient import TestClient
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recorder import Recorder
from ios_runtime import IOSRuntime
from studio import Studio, studio_tools
from studio_mcp import create_app
from workflow import (
    MAX_CONTEXT_BYTES, MAX_MAP_BYTES, list_simulators, load_application_graph,
    mcp_environment, parse_extra_context, repository_location, simulator_build_url,
)

GRAPH = {
    "schema": "application-cognitive-platform/graph", "name": "CanonicalGraph",
    "nodes": [
        {"id": "screen:Login", "type": "Screen", "label": "Login"},
        {"id": "button:login", "type": "Button", "label": "Login button", "accessibility_id": "login-button"},
    ],
    "edges": [{"source": "screen:Login", "target": "button:login", "type": "contains"}],
}


def outcome(text="ok", ok=True):
    raw = CallToolResult(content=[TextContent(type="text", text=text)], isError=not ok)
    return SimpleNamespace(ok=ok, text=text, error=None if ok else text, raw=raw)


def test_studio():
    bridge = Mock(connected=True)
    bridge.list_tools.return_value = [
        Tool(name="appium_get_page_source", inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}}}),
        Tool(name="appium_session_management", inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "sessionId": {"type": "string"}}}),
        Tool(name="select_device", inputSchema={"type": "object"}),
        Tool(name="appium_ai", inputSchema={"type": "object"}),
    ]
    bridge.call_tool.return_value = outcome("<Login/>")
    studio = Studio(bridge)
    studio.graph = Mock(return_value=GRAPH)
    studio.ios.simulator = Mock(return_value={"udid": "PHONE", "name": "iPhone Simulator"})
    return studio


class InputTests(unittest.TestCase):
    def test_mcp_never_inherits_vision_configuration(self):
        with patch.dict("os.environ", {"AI_VISION_ENABLED": "true", "AI_VISION_API_KEY": "old-key", "PATH": "/usr/bin"}, clear=True):
            env = mcp_environment("/android-sdk")
        self.assertEqual(env["AI_VISION_ENABLED"], "false")
        self.assertEqual(env["ANDROID_HOME"], "/android-sdk")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("AI_VISION_API_KEY", env)

    def test_fixed_graph_loader_and_validation(self):
        path = Mock()
        path.stat.return_value.st_size = 100
        path.read_text.return_value = json.dumps(GRAPH)
        with patch("workflow.APPLICATION_GRAPH_PATH", path):
            self.assertEqual(load_application_graph(), GRAPH)
            for value in [None, [], {}, {**GRAPH, "nodes": ["login"]}, {**GRAPH, "edges": [{}]}]:
                path.read_text.return_value = json.dumps(value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_application_graph()
            path.stat.return_value.st_size = MAX_MAP_BYTES + 1
            with self.assertRaises(ValueError):
                load_application_graph()

    def test_expanded_graph_size_is_accepted(self):
        path = Mock()
        path.stat.return_value.st_size = 11_120_125
        path.read_text.return_value = json.dumps(GRAPH)
        with patch("workflow.APPLICATION_GRAPH_PATH", path):
            self.assertEqual(load_application_graph(), GRAPH)

    def test_extra_context(self):
        self.assertEqual(parse_extra_context(b"\xef\xbb\xbfreglas", "context.md"), "reglas")
        with self.assertRaises(ValueError):
            parse_extra_context(b"bad json", "context.json")
        with self.assertRaises(ValueError):
            parse_extra_context(b" " * (MAX_CONTEXT_BYTES + 1), "context.txt")

    def test_simulator_build_urls(self):
        with self.assertRaisesRegex(ValueError, "iphonesimulator"):
            simulator_build_url("https://builds.example/app.IPA?token=test")
        self.assertEqual(simulator_build_url("https://builds.example/app.zip?token=test"), "https://builds.example/app.zip?token=test")

    def test_local_build_paths_and_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Mi App.app"
            app.mkdir()
            archive = root / "Mi App.zip"
            archive.touch()
            self.assertEqual(simulator_build_url(str(app)), str(app.resolve()))
            self.assertEqual(simulator_build_url(str(archive)), str(archive.resolve()))
            self.assertEqual(repository_location(directory), str(root.resolve()))
            with patch("workflow.PROJECT_ROOT", root):
                self.assertEqual(simulator_build_url("Mi App.zip"), str(archive.resolve()))
                self.assertEqual(repository_location("."), str(root.resolve()))
            with patch.dict("os.environ", {"HOME": directory}):
                self.assertEqual(simulator_build_url("~/Mi App.app"), str(app.resolve()))

    def test_local_ipa_has_simulator_error_not_url_error(self):
        with self.assertRaisesRegex(ValueError, "iphonesimulator"):
            simulator_build_url("/Users/usuario/Desktop/CWA5.ipa")

    def test_missing_local_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No existe la build local"):
                simulator_build_url(str(Path(directory) / "missing.zip"))


class SimulatorTests(unittest.TestCase):
    def test_discovery_filters_available_iphones(self):
        devices = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {"name": "iPhone 16", "udid": "PHONE", "isAvailable": True},
            {"name": "iPad Pro", "udid": "IPAD", "isAvailable": True},
            {"name": "iPhone 15", "udid": "OLD", "isAvailable": False},
        ]}}
        with patch("workflow.sys.platform", "darwin"), patch("workflow.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(devices))):
            self.assertEqual([device["udid"] for device in list_simulators()], ["PHONE"])

    def test_agent_can_open_simulator_window(self):
        with patch("ios_runtime.list_simulators", return_value=[{"udid": "SIM"}]), patch("ios_runtime.open_simulator") as show:
            self.assertEqual(IOSRuntime().show_simulator("SIM")["window"], "opened")
            show.assert_called_once_with("SIM")

    def test_unknown_simulator_is_rejected(self):
        with patch("ios_runtime.list_simulators", return_value=[]), patch("ios_runtime.open_simulator") as show:
            with self.assertRaises(ValueError):
                IOSRuntime().show_simulator("UNKNOWN")
            show.assert_not_called()


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.studio = test_studio()
        self.studio.set_activity("login", context="instrucciones", extra_context="reglas", build_url="https://example.com/app.zip")

    def test_map_search_and_exact_node(self):
        result = self.studio.call_tool("search_application_graph", {"query": "Login", "limit": 1})
        self.assertFalse(result.is_error)
        data = json.loads(result.content[0].text)
        self.assertEqual(data["total_matches"], 2)
        self.assertEqual(data["neighbors"][0]["accessibility_id"], "login-button")
        node = self.studio.call_tool("get_application_node", {"node_id": "button:login"})
        self.assertEqual(json.loads(node.content[0].text)["incoming_count"], 1)
        self.assertEqual(self.studio.state()["step_count"], 2)
        self.studio.bridge.call_tool.assert_not_called()

    def test_unknown_and_vision_tools_are_rejected(self):
        self.assertNotIn("appium_ai", [tool.name for tool in self.studio.tools()])
        for name in ["appium_ai", "delete_everything"]:
            self.assertTrue(self.studio.call_tool(name, {}).is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_copilot_actions_share_session_and_recording(self):
        self.studio.bridge.call_tool.return_value = outcome("IOS session created successfully with ID: SESSION-1\nPlatform: iOS")
        self.studio.call_tool("appium_session_management", {"action": "create", "platform": "ios", "capabilities": '{"appium:udid":"PHONE"}'})
        self.studio.bridge.call_tool.return_value = outcome("<Login/>")
        result = self.studio.call_tool("appium_get_page_source", {})
        self.assertFalse(result.is_error)
        self.assertEqual(self.studio.bridge.call_tool.call_args.args[1]["sessionId"], "SESSION-1")
        state = self.studio.state()
        self.assertEqual(state["session_id"], "SESSION-1")
        self.assertEqual(state["activity"]["extra_context"], "reglas")
        self.assertEqual(state["step_count"], 2)
        self.assertEqual(state["steps"][-1]["tool"], "appium_get_page_source")
        self.assertNotIn("nodes", state["graph"])

    def test_failed_appium_action_is_preserved_and_recorded(self):
        self.studio.bridge.call_tool.return_value = outcome("Element not found", ok=False)
        result = self.studio.call_tool("appium_get_page_source", {})
        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].text, "Element not found")
        self.assertEqual(self.studio.state()["steps"][-1]["status"], "error")

    def test_upstream_unavailable_does_not_hide_map(self):
        self.studio.bridge.connected = False
        self.studio.bridge.connect.side_effect = RuntimeError("npx unavailable")
        self.assertEqual([tool.name for tool in self.studio.tools()], [tool.name for tool in studio_tools()])
        self.assertEqual(self.studio.state()["upstream_error"], "npx unavailable")
        self.assertFalse(self.studio.call_tool("get_application_graph_summary", {}).is_error)

    def test_state_reads_are_not_recorded(self):
        self.studio.call_tool("get_studio_state", {})
        self.assertEqual(self.studio.state()["step_count"], 0)

    def test_automatic_device_selection_is_visible(self):
        self.studio.bridge.call_tool.return_value = outcome('{"capabilities":{"appium:udid":"PHONE"}}')
        self.studio.call_tool("select_device", {"platform": "ios", "iosDeviceType": "simulator"})
        self.assertEqual(self.studio.state()["device"]["udid"], "PHONE")

    def test_remote_session_is_rejected(self):
        result = self.studio.call_tool("appium_session_management", {"action": "attach", "sessionId": "REMOTE"})
        self.assertTrue(result.is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_agent_capabilities_are_forwarded_without_rewriting(self):
        capabilities = json.dumps({"appium:udid": "PHONE", "appium:noReset": False, "appium:wdaLocalPort": 18100})
        self.studio.bridge.call_tool.return_value = outcome("IOS session created successfully with ID: SESSION-1")
        result = self.studio.call_tool("appium_session_management", {"action": "create", "platform": "ios", "capabilities": capabilities})
        self.assertFalse(result.is_error)
        self.assertEqual(self.studio.bridge.call_tool.call_args.args[1]["capabilities"], capabilities)
        self.assertEqual(self.studio.state()["capabilities"]["appium:noReset"], False)

    def test_agent_host_operations_are_recorded(self):
        with patch.object(self.studio.ios, "show_simulator", return_value={"udid": "SIM", "window": "opened"}):
            result = self.studio.call_tool("show_ios_simulator", {"udid": "SIM"})
        self.assertFalse(result.is_error)
        self.assertEqual(self.studio.state()["steps"][-1]["tool"], "show_ios_simulator")

    def test_simulator_cannot_open_usb_tunnel(self):
        self.studio.device = {"udid": "SIM", "platform": "ios", "iosDeviceType": "simulator"}
        result = self.studio.call_tool("manage_ios_tunnel", {"action": "start", "udid": "SIM", "local_port": 18100})
        self.assertTrue(result.is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_real_device_and_android_are_rejected(self):
        for arguments in [{"platform": "ios", "iosDeviceType": "real"}, {"platform": "android"}]:
            self.assertTrue(self.studio.call_tool("select_device", arguments).is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_unknown_udid_is_rejected_before_session_creation(self):
        self.studio.ios.simulator.side_effect = ValueError("Unknown simulator")
        result = self.studio.call_tool("appium_session_management", {
            "action": "create", "platform": "ios", "capabilities": '{"appium:udid":"PHYSICAL"}',
        })
        self.assertTrue(result.is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_session_requires_explicit_local_simulator_udid(self):
        result = self.studio.call_tool("appium_session_management", {"action": "create", "platform": "ios", "capabilities": "{}"})
        self.assertTrue(result.is_error)
        self.studio.bridge.call_tool.assert_not_called()

    def test_activity_rejects_device_ipa(self):
        with self.assertRaises(ValueError):
            self.studio.set_activity("wrong-build", build_url="https://example.com/app.ipa")


class IOSRuntimeTests(unittest.TestCase):
    def test_connection_requires_wda_ready_evidence(self):
        for ready in (True, False):
            client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"value": {"ready": ready}})))
            with patch("ios_runtime.httpx.Client", return_value=client):
                result = IOSRuntime().check_connection(18100)
            self.assertTrue(result["reachable"])
            self.assertEqual(result["ready"], ready)

    def test_connection_errors_are_diagnostic(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        with patch("ios_runtime.httpx.Client", return_value=client):
            result = IOSRuntime().check_connection(18100)
        self.assertFalse(result["ready"])
        self.assertIn("error", result)


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.studio = test_studio()
        self.client = TestClient(create_app(self.studio), base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.headers = {"accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"}
        self.initialize = self.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "copilot-test", "version": "1"},
        })

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def rpc(self, method, params=None):
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertNotIn("error", data, data)
        return data["result"]

    def test_discovery_exposes_map_and_appium_without_model(self):
        self.assertIn("Copilot is the agent", self.initialize["instructions"])
        names = [tool["name"] for tool in self.rpc("tools/list")["tools"]]
        self.assertIn("search_application_graph", names)
        self.assertIn("appium_get_page_source", names)
        self.assertNotIn("appium_ai", names)
        self.assertIn("show_ios_simulator", names)
        self.assertNotIn("manage_ios_tunnel", names)
        self.assertNotIn("appium_prepare_ios_real_device", names)
        self.assertIn("check_ios_connection", names)
        self.assertIn("Compose capabilities YOURSELF", self.initialize["instructions"])

    def test_resources_expose_full_map_and_summary(self):
        resources = self.rpc("resources/list")["resources"]
        self.assertIn("application://graph/map", [resource["uri"] for resource in resources])
        full = self.rpc("resources/read", {"uri": "application://graph/map"})
        self.assertEqual(json.loads(full["contents"][0]["text"]), GRAPH)
        summary = self.rpc("resources/read", {"uri": "application://graph/summary"})
        self.assertEqual(json.loads(summary["contents"][0]["text"])["node_count"], 2)

    def test_dashboard_context_and_copilot_call_share_state(self):
        response = self.client.post("/api/activity", json={"activity": "login", "extra_context": "reglas", "build_url": "https://example.com/app.zip"})
        self.assertEqual(response.status_code, 200)
        result = self.rpc("tools/call", {"name": "appium_get_page_source", "arguments": {}})
        self.assertFalse(result.get("isError", False))
        state = self.client.get("/api/state").json()
        self.assertEqual(state["step_count"], 1)
        self.assertEqual(state["activity"]["extra_context"], "reglas")
        self.assertEqual(self.client.post("/api/recording/clear").status_code, 200)
        self.assertEqual(self.client.get("/api/state").json()["step_count"], 0)

    def test_untrusted_origin_and_host_are_blocked(self):
        self.assertEqual(self.client.post("/api/activity", json={"activity": "bad"}, headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"host": "evil.example"}).status_code, 400)

    def test_invalid_activity_is_rejected(self):
        self.assertEqual(self.client.post("/api/activity", json={"activity": ""}).status_code, 400)

    def test_copilot_modern_ping_is_compatible(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "copilot-cli", "version": "1"},
        }
        headers = {**self.headers, "mcp-protocol-version": "2026-07-28", "mcp-method": "ping"}
        response = self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 123, "method": "ping", "params": {"_meta": meta},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"jsonrpc": "2.0", "id": 123, "result": {}})
        self.assertEqual(self.studio.state()["step_count"], 0)
        self.studio.bridge.call_tool.assert_not_called()

    def test_modern_ping_does_not_bypass_envelope_validation(self):
        headers = {**self.headers, "mcp-protocol-version": "2026-07-28", "mcp-method": "ping"}
        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 123, "method": "ping", "params": {}})
        self.assertEqual(response.status_code, 400)

    def test_modern_unknown_methods_still_return_not_found(self):
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
        headers = {**self.headers, "mcp-protocol-version": "2026-07-28", "mcp-method": "unknown"}
        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 123, "method": "unknown", "params": {"_meta": meta}})
        self.assertEqual(response.status_code, 404, response.text)

    def test_activity_api_preserves_local_build_and_repository_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "My App.app"
            app.mkdir()
            response = self.client.post("/api/activity", json={
                "activity": "local-build", "build_url": str(app), "repo_url": str(root),
            })
            self.assertEqual(response.status_code, 200, response.text)
            state = self.client.get("/api/state").json()
            self.assertEqual(state["activity"]["build_url"], str(app.resolve()))
            self.assertEqual(state["activity"]["repo_url"], str(root.resolve()))


class DashboardTests(unittest.TestCase):
    def test_dashboard_has_no_chat_models_or_appium_connection(self):
        studio = test_studio()
        studio.set_activity("login")
        real_client = httpx.Client

        def respond(request):
            if request.url.path == "/api/activity":
                studio.set_activity(**json.loads(request.content))
            return httpx.Response(200, json=studio.state())

        transport = httpx.MockTransport(respond)
        with patch("httpx.Client", side_effect=lambda **kwargs: real_client(transport=transport)):
            app = AppTest.from_file("../src/app.py").run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.chat_input), 0)
            self.assertFalse(any("modelo" in widget.label.lower() for widget in app.text_input))
            next(widget for widget in app.text_input if widget.label == "Nombre de la actividad").set_value("next-activity")
            next(widget for widget in app.button if widget.label == "Guardar actividad").click().run(timeout=20)
            self.assertFalse(app.exception)
        self.assertEqual(studio.state()["activity"]["name"], "next-activity")
        studio.bridge.connect.assert_not_called()
        studio.bridge.call_tool.assert_not_called()


class ReplayTests(unittest.TestCase):
    def test_replay_skips_map_queries_and_remaps_session(self):
        recorder = Recorder("login", "context")
        recorder.log_step("search_application_graph", {"query": "login"}, "ok", "{}", 1)
        recorder.log_step("prepare_ios_simulator", {"udid": "UDID"}, "ok", '{"ready":true}', 1)
        recorder.log_step("appium_session_management", {
            "action": "create", "platform": "ios",
            "capabilities": json.dumps({"appium:udid": "UDID", "appium:webDriverAgentUrl": "http://localhost:OLD"}),
        }, "ok", "IOS session created successfully with ID: OLD-SESSION\nPlatform: iOS", 1)
        recorder.log_step("appium_get_page_source", {"sessionId": "OLD-SESSION"}, "ok", "<Login/>", 1)
        bridge = Mock()
        bridge.call_tool.side_effect = [
            outcome('{"ready":true,"capabilitiesHint":{"appium:webDriverAgentUrl":"http://localhost:8123"}}'),
            outcome("IOS session created successfully with ID: NEW-SESSION\nPlatform: iOS"), outcome("<Login/>"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            recorder.save(Path(directory) / "login.json")
            namespace = {"__name__": "replay_test", "__file__": str(Path(directory) / "login_replay.py")}
            with patch("mcp_bridge.MCPBridge", return_value=bridge), patch("builtins.print"):
                exec(recorder.to_replay_script("login.json"), namespace)
                namespace["main"]()
        self.assertEqual(bridge.call_tool.call_count, 3)
        self.assertEqual(json.loads(bridge.call_tool.call_args_list[1].args[1]["capabilities"])["appium:webDriverAgentUrl"], "http://localhost:8123")
        self.assertEqual(bridge.call_tool.call_args_list[2].args[1]["sessionId"], "NEW-SESSION")
        self.assertEqual(bridge.connect.call_args.kwargs["env"]["AI_VISION_ENABLED"], "false")
        bridge.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()