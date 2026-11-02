"""Dashboard for the shared MCP service controlled by Copilot CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow import parse_extra_context, validate_url

st.set_page_config(page_title="Appium MCP Studio", page_icon=":material/phone_iphone:", layout="wide")
st.title("Appium MCP Studio")
service_url = st.sidebar.text_input("Servicio MCP", value="http://127.0.0.1:8765")


def api(path: str, method: str = "GET", data: dict | None = None) -> dict:
    base = validate_url(service_url).rstrip("/")
    parsed = urlsplit(base)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path or parsed.query:
        raise ValueError("Usa la URL base del servicio local de Studio.")
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.request(method, base + path, json=data)
        if response.is_error:
            raise ValueError(response.json().get("error", response.reason_phrase))
        return response.json()


@st.fragment(run_every="2s")
def live_dashboard() -> None:
    try:
        state = api("/api/state")
    except (httpx.HTTPError, ValueError) as exc:
        st.warning(f"Servicio MCP no disponible: {exc}")
        return
    status_tab, steps_tab = st.tabs(["Estado", "Pasos"])
    with status_tab:
        activity = state.get("activity") or {}
        columns = st.columns(3)
        columns[0].metric("Appium", "Conectado" if state["connected"] else "Sin conexión")
        columns[1].metric("Pasos", state["step_count"])
        columns[2].metric("Herramienta activa", state.get("running_tool") or "Ninguna")
        st.write("**Actividad:**", activity.get("name") or "Sin actividad")
        st.write("**Sesión:**", state.get("session_id") or "Sin sesión")
        st.write("**Build:**", activity.get("build_url") or "Sin build")
        if state.get("device"):
            st.json(state["device"])
        if state.get("preparation"):
            with st.expander("Preparación iOS"):
                st.json(state["preparation"])
        if state.get("capabilities"):
            with st.expander("Capabilities de la sesión"):
                st.json(state["capabilities"])
        if state.get("upstream_error"):
            st.error(state["upstream_error"])
        if state.get("graph_error"):
            st.error(state["graph_error"])
        if state.get("graph"):
            with st.expander("application_graph.json"):
                st.json(state["graph"])
        if activity:
            with st.expander("Contexto de la actividad"):
                st.write(activity.get("context", ""))
                st.text(activity.get("extra_context", ""))
    with steps_tab:
        steps = state["steps"]
        if steps:
            st.dataframe([{
                "#": step["index"], "tool": step["tool"], "status": step["status"],
                "duration_ms": step["duration_ms"], "timestamp": step["timestamp"],
            } for step in steps], width="stretch")
            with st.expander("Argumentos y resultados"):
                st.json(steps)
        else:
            st.info("Sin pasos registrados.")
        save_column, clear_column = st.columns(2)
        if save_column.button("Guardar grabación", icon=":material/save:", disabled=not state.get("activity")):
            try:
                result = api("/api/recording/save", "POST")
                st.success(f"Guardado: {result['recording']}")
            except (httpx.HTTPError, ValueError) as exc:
                st.error(str(exc))
        if clear_column.button("Limpiar pasos", icon=":material/delete:", disabled=not steps):
            try:
                api("/api/recording/clear", "POST")
                st.rerun()
            except (httpx.HTTPError, ValueError) as exc:
                st.error(str(exc))


live_dashboard()
st.divider()
st.subheader("Actividad")
with st.form("activity"):
    activity_name = st.text_input("Nombre de la actividad")
    build_url = st.text_input("Ruta o URL de la build de simulador (.app / .zip)", placeholder="/Users/usuario/Downloads/MiApp.app")
    repo_url = st.text_input("Ruta o URL del repositorio", placeholder="/Users/usuario/Documents/MiApp")
    context = st.text_area("Contexto / instrucciones", height=120)
    extra_file = st.file_uploader("Archivo de contexto extra", type=["txt", "md", "json"])
    submitted = st.form_submit_button("Guardar actividad", type="primary", icon=":material/save:")
if submitted:
    try:
        extra_context = parse_extra_context(extra_file.getvalue(), extra_file.name) if extra_file else ""
        api("/api/activity", "POST", {
            "activity": activity_name, "build_url": build_url, "repo_url": repo_url,
            "context": context, "extra_context": extra_context,
            "extra_context_filename": extra_file.name if extra_file else "",
        })
        st.success("Actividad guardada.")
        st.rerun()
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        st.error(str(exc))