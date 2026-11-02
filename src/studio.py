"""Shared map, activity and Appium session for Copilot and the dashboard."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from mcp_bridge import MCPBridge
from ios_runtime import IOSRuntime
from recorder import Recorder
from workflow import (
    APPLICATION_GRAPH_PATH, MAX_CONTEXT_BYTES, graph_summary, load_application_graph,
    mcp_environment, repository_location, search_application_graph, simulator_build_url,
)

STUDIO_TOOL_NAMES = {
    "get_application_graph_summary", "search_application_graph",
    "get_application_node", "get_studio_state",
    "inspect_ios_environment", "show_ios_simulator", "check_ios_connection",
}
BLOCKED_TOOLS = {"appium_ai", "appium_prepare_ios_real_device", "manage_ios_tunnel"}


def json_result(value: Any, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        isError=error,
    )


def studio_tools() -> list[Tool]:
    specs = [
        ("get_application_graph_summary", "Read the canonical application_graph.json summary before navigating the app.", {}, []),
        ("search_application_graph", "Search screens, controls, accessibility IDs and navigation in the canonical map; returns nearby nodes and directed edges.", {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
        }, ["query"]),
        ("get_application_node", "Read an exact map node with incoming and outgoing relations. Static evidence must be verified against the running app XML.", {
            "node_id": {"type": "string", "minLength": 1},
        }, ["node_id"]),
        ("get_studio_state", "Read activity, build/repository paths or URLs, extra context, selected simulator, Appium session ID and recorded steps.", {}, []),
        ("inspect_ios_environment", "Discover available iPhone simulators and Xcode on this Mac. Only these local simulators are supported; no USB tunnels.", {}, []),
        ("show_ios_simulator", "Open the native Simulator window for a discovered iPhone UDID after preparing it with prepare_ios_simulator.", {
            "udid": {"type": "string", "minLength": 1},
        }, ["udid"]),
        ("check_ios_connection", "Probe local WDA /status to verify readiness before using an endpoint in capabilities. Does not create a session.", {
            "local_port": {"type": "integer", "minimum": 1, "maximum": 65535},
        }, ["local_port"]),
    ]
    return [Tool(
        name=name, description=description,
        inputSchema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        annotations=ToolAnnotations(readOnlyHint=name != "show_ios_simulator", destructiveHint=False, openWorldHint=False),
    ) for name, description, properties, required in specs]


class Studio:
    def __init__(self, bridge: MCPBridge | None = None) -> None:
        self.bridge = bridge or MCPBridge()
        self.ios = IOSRuntime()
        self.recorder: Recorder | None = None
        self._state_lock = threading.RLock()
        self._call_lock = threading.RLock()
        self._graph_lock = threading.Lock()
        self._graph: dict[str, Any] | None = None
        self._graph_version: tuple[int, int] | None = None
        self._tools: list[Tool] = []
        self.running: str | None = None
        self.upstream_error: str | None = None
        self.session_id: str | None = None
        self.device: dict[str, Any] | None = None
        self.capabilities: dict[str, Any] | None = None
        self.preparation: dict[str, Any] | None = None

    def graph(self) -> dict[str, Any]:
        with self._graph_lock:
            info = APPLICATION_GRAPH_PATH.stat()
            version = (info.st_mtime_ns, info.st_size)
            if self._graph is None or self._graph_version != version:
                self._graph = load_application_graph()
                self._graph_version = version
            return self._graph

    def tools(self) -> list[Tool]:
        with self._call_lock:
            try:
                if not self.bridge.connected:
                    self.bridge.connect("npx", ["-y", "appium-mcp@latest"], env=mcp_environment(), timeout=180)
                if not self._tools:
                    self._tools = [tool.model_copy(deep=True) for tool in self.bridge.list_tools() if tool.name not in BLOCKED_TOOLS]
                    for tool in self._tools:
                        if tool.name == "select_device":
                            properties = tool.input_schema.setdefault("properties", {})
                            properties["platform"] = {"type": "string", "enum": ["ios"]}
                            properties["iosDeviceType"] = {"type": "string", "enum": ["simulator"]}
                self.upstream_error = None
            except Exception as exc:
                self.upstream_error = str(exc)
                return studio_tools()
            return studio_tools() + self._tools

    def set_activity(
        self, activity: str, context: str = "", repo_url: str = "",
        build_url: str = "", extra_context: str = "", extra_context_filename: str = "",
    ) -> dict[str, Any]:
        for value in (activity, context, repo_url, build_url, extra_context, extra_context_filename):
            if not isinstance(value, str):
                raise ValueError("Los campos de actividad deben ser texto.")
        if not activity.strip():
            raise ValueError("La actividad necesita un nombre.")
        if len(extra_context.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("El contexto extra supera el limite de 2 MiB.")
        if build_url:
            build_url = simulator_build_url(build_url)
        repo_url = repository_location(repo_url)
        graph = self.graph()
        with self._call_lock, self._state_lock:
            self.recorder = Recorder(
                activity=activity.strip(), context=context, repo_url=repo_url,
                app_map=graph, device=self.device, build_url=build_url,
                extra_context=extra_context, extra_context_filename=extra_context_filename,
            )
        return self.state()

    def state(self) -> dict[str, Any]:
        with self._state_lock:
            recording = self.recorder.recording if self.recorder else None
            state = {
                "connected": self.bridge.connected,
                "upstream_error": self.upstream_error,
                "running_tool": self.running,
                "session_id": self.session_id,
                "device": self.device,
                "capabilities": self.capabilities,
                "preparation": self.preparation,
                "target": "local_iphone_simulator",
                "activity": None if recording is None else {
                    "name": recording.activity, "context": recording.context,
                    "repo_url": recording.repo_url, "build_url": recording.build_url,
                    "extra_context": recording.extra_context,
                    "extra_context_filename": recording.extra_context_filename,
                },
                "step_count": len(recording.steps) if recording else 0,
                "steps": recording.steps[-300:] if recording else [],
            }
            state = copy.deepcopy(state)
        try:
            state["graph"] = graph_summary(self.graph())
        except (OSError, ValueError) as exc:
            state["graph_error"] = str(exc)
        return state

    def graph_node(self, node_id: str) -> dict[str, Any]:
        graph = self.graph()
        node = next((node for node in graph["nodes"] if node["id"] == node_id), None)
        if node is None:
            raise ValueError("Nodo no encontrado: " + node_id)
        incoming = [edge for edge in graph["edges"] if edge["target"] == node_id]
        outgoing = [edge for edge in graph["edges"] if edge["source"] == node_id]
        return {
            "node": node, "incoming": incoming[:100], "outgoing": outgoing[:100],
            "incoming_count": len(incoming), "outgoing_count": len(outgoing),
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if not isinstance(arguments, dict):
            return json_result({"error": "Arguments must be a JSON object."}, error=True)
        with self._call_lock:
            with self._state_lock:
                self.running = name
            started = time.perf_counter()
            try:
                if name in BLOCKED_TOOLS:
                    raise ValueError("Este servicio solo utiliza simuladores de iPhone de esta Mac.")
                if name == "get_application_graph_summary":
                    result = json_result(graph_summary(self.graph()))
                elif name == "search_application_graph":
                    result = json_result(search_application_graph(self.graph(), **arguments))
                elif name == "get_application_node":
                    result = json_result(self.graph_node(**arguments))
                elif name == "get_studio_state":
                    result = json_result(self.state())
                elif name == "inspect_ios_environment":
                    result = json_result(self.ios.inspect())
                elif name == "show_ios_simulator":
                    result = json_result(self.ios.show_simulator(**arguments))
                elif name == "check_ios_connection":
                    result = json_result(self.ios.check_connection(**arguments))
                else:
                    available = {tool.name: tool for tool in self.tools()}
                    if name not in available:
                        raise ValueError("Herramienta no disponible: " + name)
                    forwarded = dict(arguments)
                    self._validate_simulator_target(name, forwarded)
                    schema = available[name].input_schema.get("properties", {})
                    if self.session_id and "sessionId" in schema and "sessionId" not in forwarded:
                        forwarded["sessionId"] = self.session_id
                    arguments = forwarded
                    timeout = 300 if name in {"prepare_ios_simulator", "appium_session_management"} else 120
                    outcome = self.bridge.call_tool(name, forwarded, timeout=timeout)
                    result = outcome.raw or json_result({"error": outcome.error}, error=True)
                    if outcome.ok:
                        self._update_session(name, forwarded, outcome.text)
            except Exception as exc:
                result = json_result({"error": str(exc)}, error=True)
            finally:
                with self._state_lock:
                    self.running = None
            with self._state_lock:
                if self.recorder and name != "get_studio_state":
                    summary = "\n".join(block.text for block in result.content if block.type == "text")
                    self.recorder.log_step(name, arguments, "error" if result.is_error else "ok", summary, (time.perf_counter() - started) * 1000)
            return result

    def _validate_simulator_target(self, name: str, arguments: dict[str, Any]) -> None:
        if name == "select_device":
            if arguments.get("platform", "ios") != "ios" or arguments.get("iosDeviceType", "simulator") != "simulator":
                raise ValueError("Solo se permite seleccionar simuladores locales de iPhone.")
            arguments.update({"platform": "ios", "iosDeviceType": "simulator"})
            if arguments.get("deviceUdid"):
                self.ios.simulator(arguments["deviceUdid"])
        if name == "prepare_ios_simulator":
            if arguments.get("platform", "ios") != "ios":
                raise ValueError("Solo se permiten simuladores iOS de iPhone.")
            self.ios.simulator(arguments.get("udid"))
        if name == "appium_session_management":
            if arguments.get("remoteServerUrl") or arguments.get("action") == "attach":
                raise ValueError("Las sesiones remotas no estan permitidas en este servicio.")
            if arguments.get("action") == "create":
                if arguments.get("platform") != "ios":
                    raise ValueError("La sesion debe ser iOS en un simulador local.")
                caps = arguments.get("capabilities", "{}")
                caps = json.loads(caps) if isinstance(caps, str) else caps
                if not isinstance(caps, dict):
                    raise ValueError("Las capabilities deben representar un objeto JSON.")
                options = caps.get("appium:options", {})
                if not isinstance(options, dict):
                    raise ValueError("appium:options debe ser un objeto JSON.")
                udids = [value for value in (options.get("udid"), caps.get("appium:udid"), caps.get("udid")) if value]
                if not udids:
                    raise ValueError("Incluye appium:udid del simulador local en las capabilities.")
                for udid in udids:
                    self.ios.simulator(udid)
                if len(set(udids)) != 1:
                    raise ValueError("Las capabilities contienen UDID contradictorios.")
                if caps.get("platformName", "iOS").lower() != "ios":
                    raise ValueError("platformName debe ser iOS.")

    def _update_session(self, name: str, arguments: dict[str, Any], text: str) -> None:
        with self._state_lock:
            if name == "prepare_ios_simulator":
                try:
                    self.preparation = json.loads(text)
                except ValueError:
                    self.preparation = {"result": text}
            if name == "select_device":
                udid = arguments.get("deviceUdid")
                if not udid:
                    try:
                        udid = json.loads(text).get("capabilities", {}).get("appium:udid")
                    except (ValueError, AttributeError):
                        pass
                if udid:
                    if not self.device or self.device.get("udid") != udid:
                        self.preparation = None
                    self.device = {"udid": udid, "platform": arguments.get("platform"), "iosDeviceType": arguments.get("iosDeviceType")}
            if name == "appium_session_management":
                action = arguments.get("action")
                if action == "create":
                    capabilities = arguments.get("capabilities")
                    self.capabilities = json.loads(capabilities) if isinstance(capabilities, str) else capabilities
                    marker = "session created successfully with ID: "
                    first_line = text.splitlines()[0] if text else ""
                    if marker in first_line:
                        self.session_id = first_line.split(marker, 1)[1].strip()
                elif action in {"select", "attach"}:
                    if self.session_id != arguments.get("sessionId"):
                        self.capabilities = None
                    self.session_id = arguments.get("sessionId")
                elif action in {"delete", "detach"} and arguments.get("sessionId", self.session_id) == self.session_id:
                    self.session_id = None
                    self.capabilities = None
            if self.recorder:
                self.recorder.recording.device = self.device

    def save_recording(self) -> dict[str, str]:
        with self._call_lock, self._state_lock:
            if self.recorder is None:
                raise ValueError("No hay actividad para guardar.")
            name = "".join(character if character.isalnum() or character in "-_" else "-" for character in self.recorder.recording.activity) or "activity"
            directory = Path(__file__).resolve().parent.parent / "recordings"
            path = self.recorder.save(directory / f"{name}.json")
            script = directory / f"{name}_replay.py"
            script.write_text(self.recorder.to_replay_script(path.name), encoding="utf-8")
            return {"recording": str(path), "replay": str(script)}

    def clear_steps(self) -> None:
        with self._call_lock, self._state_lock:
            if self.recorder:
                self.recorder.clear()

    def shutdown(self) -> None:
        with self._call_lock:
            self.bridge.shutdown()