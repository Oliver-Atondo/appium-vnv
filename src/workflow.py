"""Validated workflow inputs and local iOS simulator setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_MAP_BYTES = 16 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APPLICATION_GRAPH_PATH = PROJECT_ROOT / "application_graph.json"


def mcp_environment(android_home: str = "") -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AI_VISION_")}
    env.update({"NO_UI": "true", "AI_VISION_ENABLED": "false"})
    if android_home:
        env["ANDROID_HOME"] = android_home
    return env


def validate_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La URL debe ser HTTP o HTTPS y tener un servidor.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("La URL no debe incluir credenciales ni fragmentos.")
    return value


def load_application_graph() -> dict[str, Any]:
    if APPLICATION_GRAPH_PATH.stat().st_size > MAX_MAP_BYTES:
        raise ValueError(f"El mapa supera el limite de {MAX_MAP_BYTES // (1024 * 1024)} MiB.")
    value = json.loads(APPLICATION_GRAPH_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema") != "application-cognitive-platform/graph":
        raise ValueError("application_graph.json no tiene el schema esperado.")
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not nodes or any(not isinstance(node, dict) or not isinstance(node.get("id"), str) for node in nodes):
        raise ValueError("application_graph.json debe contener nodos con IDs.")
    if not isinstance(edges, list) or any(not isinstance(edge, dict) or not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str) for edge in edges):
        raise ValueError("application_graph.json debe contener relaciones con source y target.")
    return value


def parse_extra_context(data: bytes, filename: str) -> str:
    if len(data) > MAX_CONTEXT_BYTES:
        raise ValueError("El contexto extra supera el limite de 2 MiB.")
    text = data.decode("utf-8-sig")
    if filename.lower().endswith(".json"):
        json.loads(text)
    return text


def graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes", [])
    return {
        "schema": graph.get("schema"),
        "name": graph.get("name"),
        "node_count": len(nodes),
        "edge_count": len(graph.get("edges", [])),
        "node_types": dict(Counter(node.get("type", "unknown") for node in nodes)),
        "applications": [node.get("label") for node in nodes if node.get("type") == "Application"],
        "lookup_tool": "search_application_graph",
    }


def search_application_graph(graph: dict[str, Any], query: str, limit: int = 12) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("La consulta del grafo no puede estar vacia.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30:
        raise ValueError("El limite de resultados debe estar entre 1 y 30.")
    terms = query.casefold().split()
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    ranked = []
    for node in nodes:
        searchable = " ".join(str(node.get(key, "")) for key in (
            "id", "label", "qualified_name", "owner", "accessibility_id", "text", "english_value", "key",
        )).casefold()
        matched = sum(term in searchable for term in terms)
        if matched:
            priority = 1 if node.get("type") in {"Screen", "Button", "Input", "Navigation"} else 0
            ranked.append((matched, priority, node))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    matches = [item[2] for item in ranked[:limit]]
    identifiers = {node["id"] for node in matches}
    related_edges = [edge for edge in edges if edge.get("source") in identifiers or edge.get("target") in identifiers]
    related_edges = related_edges[:60]
    neighbor_ids = {edge.get(key) for edge in related_edges for key in ("source", "target")} - identifiers
    neighbors = [node for node in nodes if node.get("id") in neighbor_ids][:30]
    fields = {
        "id", "type", "label", "owner", "accessibility_id", "localization_key",
        "localization_ref", "english_value", "text", "binding", "source", "target",
        "kind", "trigger", "confidence", "source_files", "line", "key",
    }
    return {
        "matches": [{key: value for key, value in node.items() if key in fields} for node in matches],
        "neighbors": [{key: value for key, value in node.items() if key in fields} for node in neighbors],
        "edges": [{key: value for key, value in edge.items() if key in {"source", "target", "type", "key"}} for edge in related_edges],
        "total_matches": len(ranked),
        "truncated": len(ranked) > limit,
    }


def simulator_build_url(value: str) -> str:
    value = value.strip()
    remote = urlsplit(value).scheme in {"http", "https"}
    source_path = urlsplit(value).path if remote else value
    if source_path.lower().endswith(".ipa"):
        raise ValueError(
            "Una .ipa de iPhone fisico no se puede ejecutar en el simulador. "
            "Usa una .app compilada para iphonesimulator o su archivo .zip."
        )
    if remote:
        return validate_url(value)
    path = local_path(value)
    if path.suffix.lower() == ".app" and path.is_dir():
        return str(path)
    if path.suffix.lower() == ".zip" and path.is_file():
        return str(path)
    if not path.exists():
        raise ValueError(f"No existe la build local: {path}")
    raise ValueError("La build local debe ser una carpeta .app de simulador o un archivo .zip.")


def local_path(value: str) -> Path:
    if not value.strip() or "://" in value:
        raise ValueError("Indica una ruta local o una URL HTTP(S).")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def repository_location(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if urlsplit(value).scheme in {"http", "https"}:
        return validate_url(value)
    path = local_path(value)
    if not path.is_dir():
        raise ValueError(f"No existe la carpeta del repositorio: {path}")
    return str(path)


def list_simulators() -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        raise RuntimeError("El simulador de iOS requiere macOS y Xcode.")
    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "--json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    devices = json.loads(result.stdout)["devices"]
    return [
        {**device, "runtime": runtime}
        for runtime, entries in devices.items()
        for device in entries
        if "iOS" in runtime and device.get("isAvailable") and device["name"].startswith("iPhone")
    ]


def open_simulator(udid: str) -> None:
    subprocess.run(
        ["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", udid],
        capture_output=True, text=True, check=True, timeout=30,
    )

