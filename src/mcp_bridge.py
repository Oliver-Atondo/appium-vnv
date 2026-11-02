"""Synchronous bridge to the appium-mcp MCP server.

The official `mcp` Python SDK is async-only and its stdio transport relies
on anyio cancel scopes that must be entered/exited from the *same*
asyncio Task. To expose a simple synchronous API to Streamlit callbacks
while keeping one long-lived stdio connection to `appium-mcp` (spawned via
`npx -y appium-mcp@latest`) alive across many tool calls, this module runs
a single dedicated background thread + event loop where one persistent
coroutine owns the `stdio_client`/`ClientSession` context managers for
their entire lifetime, and services requests pushed onto an internal
asyncio.Queue from other threads.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool

_Job = tuple[Callable[[ClientSession], Awaitable[Any]], "concurrent.futures.Future[Any]"]


@dataclass
class ToolCallOutcome:
    ok: bool
    text: str
    raw: CallToolResult | None
    error: str | None = None


class MCPBridge:
    """Thread-backed synchronous facade over a single MCP ClientSession."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue[_Job | None] | None = None
        self._ready = threading.Event()
        self._connect_error: str | None = None
        self.connected = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def connect(self, command: str, args: list[str], env: dict[str, str], timeout: float = 60.0) -> None:
        """Start the background thread/loop and open the MCP session."""
        if self.connected:
            return
        self._ready.clear()
        self._connect_error = None
        self._thread = threading.Thread(target=self._run_loop, args=(command, args, env), daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("Timed out starting appium-mcp (npx) connection")
        if self._connect_error:
            self.connected = False
            raise RuntimeError(self._connect_error)
        self.connected = True

    def _run_loop(self, command: str, args: list[str], env: dict[str, str]) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve(command, args, env))
        finally:
            self._loop.close()

    async def _serve(self, command: str, args: list[str], env: dict[str, str]) -> None:
        self._queue = asyncio.Queue()
        try:
            params = StdioServerParameters(command=command, args=args, env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._ready.set()
                    while True:
                        job = await self._queue.get()
                        if job is None:
                            break
                        coro_factory, fut = job
                        try:
                            result = await coro_factory(session)
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(exc)
                        else:
                            if not fut.done():
                                fut.set_result(result)
        except Exception as exc:  # noqa: BLE001 - startup failure (e.g. npx not found)
            self._connect_error = str(exc)
            self._ready.set()

    def disconnect(self, timeout: float = 10.0) -> None:
        if not self.connected or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.connected = False
        self._loop = None
        self._thread = None
        self._queue = None

    def shutdown(self) -> None:
        """Alias kept for symmetry/readability at call sites."""
        self.disconnect()

    # ------------------------------------------------------------------ #
    # MCP operations
    # ------------------------------------------------------------------ #
    def _submit(self, coro_factory: Callable[[ClientSession], Awaitable[Any]], timeout: float) -> Any:
        if not self.connected or self._loop is None or self._queue is None:
            raise RuntimeError("MCP bridge is not connected. Call connect() first.")
        fut: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (coro_factory, fut))
        return fut.result(timeout=timeout)

    def list_tools(self, timeout: float = 30.0) -> list[Tool]:
        result = self._submit(lambda session: session.list_tools(), timeout)
        return list(result.tools)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> ToolCallOutcome:
        try:
            result = self._submit(lambda session: session.call_tool(name, arguments), timeout)
        except Exception as exc:  # noqa: BLE001 - surface any transport/tool error to the UI
            return ToolCallOutcome(ok=False, text="", raw=None, error=str(exc))

        text_parts = []
        for block in result.content:
            text = getattr(block, "text", None)
            text_parts.append(text if text is not None else repr(block))
        text = "\n".join(text_parts)
        is_error = bool(result.is_error)
        return ToolCallOutcome(ok=not is_error, text=text, raw=result, error=None if not is_error else text)
