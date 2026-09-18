"""stdio MCP server for MCP client.

Exposes the StackChan tool surface (see ``toolkit.py``) over the MCP
Python SDK's stdio transport.  Each tool call is dispatched through the
shared registry: gateway-local handlers run in-process, device tools are
relayed to the connected ESP32.  This module only wires the registry to
the transport — all tool definitions and dispatch live in ``toolkit.py``.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import AsyncExitStack
from typing import Any, Literal, cast

import anyio
from mcp.server import InitializationOptions, NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import Notification

from . import __version__
from . import control as control  # re-exported for callers/tests that patch it
from . import web_search as web_search  # re-exported for callers/tests that patch it
from .gateway import get_gateway
from .notify_config import NotifyConfig, load_notify_config
from .toolkit import (
    PRESET_DPS,
    SPEED_DESCRIPTION,
    SPEED_DPS_MAX,
    _resolve_speed_dps,
    call_tool,
    list_tools,
)

# Back-compat alias: the HTTP command queue dispatcher (http_server.py)
# still routes queue items through this name.
_dispatch_mcp_tool = call_tool

logger = logging.getLogger(__name__)

STACKCHAN_EVENT_METHOD = "stackchan/event"
CHANNEL_NOTIFICATION_METHOD = "notifications/claude/channel"
CHANNEL_CAPABILITY = "claude/channel"
_SUPPORTED_EVENT_METHODS = {STACKCHAN_EVENT_METHOD, CHANNEL_NOTIFICATION_METHOD}
STACKCHAN_EVENT_INSTRUCTIONS = (
    "Stack-chan physical events arrive as server-initiated "
    "notifications with method='stackchan/event'. Params include "
    "event_type ('touch'), subtype ('tap' or 'stroke'), "
    "duration_ms, ts, session_id. When such a notification "
    "arrives, react naturally using existing tools "
    "(set_avatar, say, set_mouth, set_leds, move_head). There is "
    "no dedicated reply tool — the existing tool palette is the "
    "reaction surface."
)
STACKCHAN_CHANNEL_INSTRUCTIONS = (
    'Stack-chan physical events arrive as Channels notifications under '
    '<channel source="plugin:stackchanmcp:stackchanmcp" action="..." '
    'subtype="..." duration_ms="...">. React naturally using existing '
    'tools (set_avatar, say, set_mouth, set_leds, move_head).'
)
STACKCHAN_JSONL_INSTRUCTIONS = (
    "Stack-chan physical events are persisted to the JSONL log; host "
    "integration consumes them externally."
)

_active_session: Any | None = None
_active_sessions: dict[int, Any] = {}


class StackChanEventNotification(
    Notification[dict[str, Any], Literal["stackchan/event"]]
):
    method: Literal["stackchan/event"] = "stackchan/event"
    params: dict[str, Any]


class StackChanChannelNotification(
    Notification[dict[str, Any], Literal["notifications/claude/channel"]]
):
    method: Literal["notifications/claude/channel"] = "notifications/claude/channel"
    params: dict[str, Any]


class StackChanServer(Server):
    """MCP Server that records the active session for event notifications."""

    def __init__(self, name: str, *, notify_config: NotifyConfig) -> None:
        super().__init__(name)
        self._notify_config = notify_config

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        if notification_options is None and experimental_capabilities is None:
            return _create_initialization_options(self, self._notify_config)
        return super().create_initialization_options(
            notification_options=notification_options,
            experimental_capabilities=experimental_capabilities,
        )

    async def run(
        self,
        read_stream: Any,
        write_stream: Any,
        initialization_options: InitializationOptions,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ) -> None:
        global _active_session
        session: Any | None = None
        try:
            async with AsyncExitStack() as stack:
                lifespan_context = await stack.enter_async_context(self.lifespan(self))
                session = await stack.enter_async_context(
                    ServerSession(
                        read_stream,
                        write_stream,
                        initialization_options,
                        stateless=stateless,
                    )
                )
                _active_session = session
                _active_sessions[id(session)] = session

                task_support = (
                    self._experimental_handlers.task_support
                    if self._experimental_handlers
                    else None
                )
                if task_support is not None:
                    task_support.configure_session(session)
                    await stack.enter_async_context(task_support.run())

                async with anyio.create_task_group() as tg:
                    try:
                        async for message in session.incoming_messages:
                            logger.debug("Received message: %s", message)
                            tg.start_soon(
                                self._handle_message,
                                message,
                                session,
                                lifespan_context,
                                raise_exceptions,
                            )
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            if session is not None:
                _active_sessions.pop(id(session), None)
            _active_session = _latest_active_session()

    async def _handle_message(
        self,
        message: Any,
        session: Any,
        lifespan_context: Any,
        raise_exceptions: bool = False,
    ) -> None:
        global _active_session
        _active_session = session
        _active_sessions[id(session)] = session
        await super()._handle_message(
            message,
            session,
            lifespan_context,
            raise_exceptions,
        )


def _latest_active_session() -> Any | None:
    if not _active_sessions:
        return None
    return next(reversed(_active_sessions.values()))


async def notify_stackchan_event(method: str, params: dict[str, Any]) -> None:
    """Forward a stackchan event to the connected MCP client."""
    if method not in _SUPPORTED_EVENT_METHODS:
        logger.warning("Unsupported stackchan event notification method: %s", method)
        return

    sessions = list(_active_sessions.values())
    if not sessions and _active_session is not None:
        sessions = [_active_session]
    if not sessions:
        logger.warning("Cannot emit %s notification: no active MCP session", method)
        return

    notification = _build_stackchan_notification(method, params)
    for session in sessions:
        try:
            await session.send_notification(cast(Any, notification))
        except Exception as exc:  # pragma: no cover - depends on client transport failure
            logger.warning("Failed to emit %s notification: %s", method, exc)


def _build_stackchan_notification(
    method: str,
    params: dict[str, Any],
) -> StackChanEventNotification | StackChanChannelNotification:
    if method == STACKCHAN_EVENT_METHOD:
        return StackChanEventNotification(params=params)
    return StackChanChannelNotification(params=params)


def _build_experimental_capabilities(
    notify_config: NotifyConfig,
) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    if notify_config.legacy_event_enabled:
        capabilities[STACKCHAN_EVENT_METHOD] = {}
    if notify_config.channels_enabled:
        capabilities[CHANNEL_CAPABILITY] = {}
    return capabilities


def _build_stackchan_event_instructions(notify_config: NotifyConfig) -> str | None:
    fragments = []
    if notify_config.channels_enabled:
        fragments.append(STACKCHAN_CHANNEL_INSTRUCTIONS)
    if notify_config.legacy_event_enabled:
        fragments.append(STACKCHAN_EVENT_INSTRUCTIONS)
    if (
        notify_config.jsonl_enabled
        and not notify_config.channels_enabled
        and not notify_config.legacy_event_enabled
    ):
        fragments.append(STACKCHAN_JSONL_INSTRUCTIONS)
    if not fragments:
        return None
    return "\n\n".join(fragments)


def _create_initialization_options(
    server: Server,
    notify_config: NotifyConfig,
) -> InitializationOptions:
    return InitializationOptions(
        server_name="stackchanmcp",
        server_version=__version__,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities=_build_experimental_capabilities(notify_config),
        ),
        instructions=_build_stackchan_event_instructions(notify_config),
    )


def _verify_mcp_sdk_compatibility() -> None:
    """Fail fast if the installed MCP SDK no longer exposes the private
    attributes that ``StackChanServer`` depends on.

    ``StackChanServer`` mirrors a slimmed-down copy of ``Server.run()`` so it
    can capture the active ``ServerSession`` for server-initiated
    ``stackchan/event`` notifications. The public MCP SDK currently does not
    offer a stable hook for this, so the subclass touches
    ``Server._experimental_handlers`` and ``Server._handle_message`` directly.

    These private members are pinned by the ``mcp>=1.27,<2.0`` range declared
    in ``pyproject.toml``. This guard adds an extra safety net so the gateway
    fails with a clear ``RuntimeError`` at startup rather than silently
    dropping notifications or crashing mid-message if a future installation
    somehow resolves a wholly incompatible SDK shape.
    """

    probe = Server("compat-check")

    if not hasattr(probe, "_experimental_handlers"):
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._experimental_handlers` "
            "to exist on instances. The installed MCP SDK appears to have removed or "
            "renamed this attribute; pin `mcp` to a verified 1.x release."
        )

    handle = getattr(probe, "_handle_message", None)
    if not callable(handle) or not inspect.iscoroutinefunction(handle):
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._handle_message` to be "
            "an async callable. The installed MCP SDK does not expose it in the "
            "expected shape; pin `mcp` to a verified 1.x release."
        )

    try:
        sig = inspect.signature(handle)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "stackchan-mcp gateway could not introspect "
            "`mcp.server.Server._handle_message` signature on the installed MCP SDK; "
            "pin `mcp` to a verified 1.x release."
        ) from exc

    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
        )
    ]
    if len(positional) < 4:
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._handle_message` to "
            "accept at least 4 positional arguments "
            "(message, session, lifespan_context, raise_exceptions); the installed "
            f"MCP SDK exposes {sig}. Pin `mcp` to a verified 1.x release."
        )


def create_server(notify_config: NotifyConfig | None = None) -> StackChanServer:
    """Create and configure the MCP server with tool handlers."""
    _verify_mcp_sdk_compatibility()
    if notify_config is None:
        notify_config = load_notify_config()
    server = StackChanServer("stackchanmcp", notify_config=notify_config)

    @server.list_tools()
    async def list_tools_handler() -> list[Any]:
        """List available stackchan tools.

        Tools prefixed with ESP32 names (self.*) are relayed to the device.
        Gateway-local tools (say, listen, notes, switchbot, web_search,
        get_status, ...) are handled here; see toolkit.py for the full
        declaration of each tool's schema and dispatch.
        """
        return list_tools()

    @server.call_tool()
    async def call_tool_handler(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        """Handle a tool call by dispatching through the shared registry."""
        return await call_tool(name, arguments or {}, get_gateway())

    return server


async def run_stdio_server(notify_config: NotifyConfig | None = None) -> None:
    """Run the MCP server on stdio."""
    if notify_config is None:
        notify_config = load_notify_config()
    server = create_server(notify_config=notify_config)
    async with stdio_server() as (read_stream, write_stream):
        logger.info("stdio MCP server starting")
        await server.run(
            read_stream,
            write_stream,
            _create_initialization_options(server, notify_config),
        )
