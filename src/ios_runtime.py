"""Host operations for agent-controlled iPhone simulators on this Mac."""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Any

import httpx

from workflow import list_simulators, open_simulator


def validate_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("El puerto debe ser un entero entre 1 y 65535.")
    return value


class IOSRuntime:
    def inspect(self) -> dict[str, Any]:
        result = {
            "platform": platform.system(), "architecture": platform.machine(),
            "tools": {name: shutil.which(name) for name in ("xcrun", "xcode-select")},
            "simulator_tunnel_required": False,
        }
        try:
            result["simulators"] = list_simulators()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            result["simulator_error"] = str(exc)
        try:
            selected = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=10, check=True)
            result["xcode_path"] = selected.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            result["xcode_error"] = str(exc)
        return result

    def show_simulator(self, udid: str) -> dict[str, str]:
        self.simulator(udid)
        open_simulator(udid)
        return {"udid": udid, "window": "opened"}

    def simulator(self, udid: str) -> dict[str, Any]:
        device = next((device for device in list_simulators() if device["udid"] == udid), None)
        if not isinstance(udid, str) or device is None:
            raise ValueError("Selecciona un UDID de simulador descubierto, no un iPhone fisico.")
        return device

    def check_connection(self, local_port: int) -> dict[str, Any]:
        local_port = validate_port(local_port)
        url = f"http://127.0.0.1:{local_port}/status"
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                response = client.get(url)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("El endpoint no devuelve un estado WDA valido.")
            value = data.get("value", {})
            ready = isinstance(value, dict) and value.get("ready") is True
            return {"url": url, "reachable": True, "ready": ready, "status": data}
        except (httpx.HTTPError, ValueError) as exc:
            return {"url": url, "reachable": False, "ready": False, "error": str(exc)}
