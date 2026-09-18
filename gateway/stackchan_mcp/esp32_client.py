"""ESP32 connection manager: WebSocket server the ESP32 connects to, MCP client to it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import json
import logging
import os
import time
import uuid
from typing import Any

import websockets
import websockets.exceptions
from websockets.asyncio.server import ServerConnection

from .audio_input_hook import push_audio_capture
from .audio_stream import (
    handle_audio_frame,
    is_recording,
    is_recording_session,
    start_recording,
    stop_recording,
)
from .notify_config import (
    DEFAULT_MESSAGE_TEMPLATES,
    NotifyConfig,
    load_notify_config,
    render_template,
)
from .protocol import HelloResponse, make_mcp_message, parse_jsonrpc_response

logger = logging.getLogger(__name__)

#: Max seconds to wait for an ESP32 WS response. Overridable for noisy WiFi links.
RESPONSE_TIMEOUT = float(os.getenv("STACKCHAN_DEVICE_TIMEOUT", "10.0"))

ToolCall = tuple[str, dict[str, Any]]
ToolCallResult = tuple[Any, dict[str, Any] | None]

# Tool-name prefix → hardware lane for per-peripheral dispatch ordering.
_TOOL_LANES = {
    "self.robot.": "servo",
    "self.led.": "led",
    "self.display.": "avatar",
    "self.screen.": "display",
    "self.audio_speaker.": "audio",
    "self.camera.": "camera",
    "self.touch.": "touch",
    "self.get_device_status": "status",
}


def _hardware_lane(tool_name: str) -> str:
    """Return the hardware lane used for per-peripheral dispatch ordering."""
    for prefix, lane in _TOOL_LANES.items():
        if tool_name.startswith(prefix):
            return lane
    return "default"


class ESP32Connection:
    """Manages a single ESP32 device connection."""

    def __init__(self, ws: ServerConnection, session_id: str):
        self._ws = ws
        self.session_id = session_id
        self.device_id: str = "unknown"
        self.tools: list[dict[str, Any]] = []
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._connected = True
        self._initialized = False
        # Pending avatar_set_fetch calls keyed by expected checksum so
        # overlapping fetches of different sets can be discriminated.
        self._avatar_set_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Device-declared WebSocket protocol version (default 1 = raw Opus).
        self.protocol_version: int = 1

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("ESP32 not connected")

    async def send_mcp_request(self, method: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
        """Send an MCP request to ESP32 and wait for the response (result, error)."""
        if not self._connected:
            return None, {"code": -32000, "message": "ESP32 not connected"}

        req_id = self._next_id()
        message = make_mcp_message(self.session_id, method, params, req_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws_send(json.dumps(message))
            response = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
            return parse_jsonrpc_response(response)
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return None, {"code": -32000, "message": f"Timeout waiting for ESP32 response (method={method})"}
        except Exception as exc:
            self._pending.pop(req_id, None)
            # Mark a concurrently-set future exception observed so the loop
            # does not log "exception was never retrieved".
            if future.done() and not future.cancelled():
                future.exception()
            return None, {"code": -32000, "message": f"ESP32 communication error: {exc}"}

    async def initialize(self, vision_url: str = "", vision_token: str = "") -> bool:
        """Send MCP initialize to ESP32."""
        capabilities: dict[str, Any] = {}
        if vision_url:
            vision: dict[str, Any] = {"url": vision_url}
            if vision_token:
                vision["token"] = vision_token
            capabilities["vision"] = vision
        result, error = await self.send_mcp_request("initialize", {"capabilities": capabilities})
        if error:
            logger.error("ESP32 initialize failed: %s", error)
            return False
        logger.info(
            "ESP32 initialized: protocol=%s server=%s",
            result.get("protocolVersion", "?"),
            result.get("serverInfo", {}),
        )
        self._initialized = True
        return True

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover tools available on ESP32 (follows nextCursor pagination)."""
        all_tools: list[dict[str, Any]] = []
        cursor = ""
        while True:
            result, error = await self.send_mcp_request("tools/list", {"cursor": cursor})
            if error:
                logger.error("tools/list failed: %s", error)
                break
            all_tools.extend(result.get("tools", []))
            next_cursor = result.get("nextCursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        self.tools = all_tools
        logger.info("Discovered %d tools on ESP32", len(all_tools))
        return all_tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
        """Call a tool on ESP32."""
        return await self.send_mcp_request("tools/call", {"name": name, "arguments": arguments})

    async def send_avatar_set_fetch(
        self, url: str, token: str, mode: str, checksum: str, expected_size: int, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Send avatar_set_fetch and wait for the avatar_set_loaded reply.

        Returns the device's reply dict ({ok, checksum, error}), or a
        synthesized {ok: False, error: ...} dict on timeout/send failure.
        """
        if not self._connected:
            return {"ok": False, "checksum": checksum, "error": "not_connected"}

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        # Last-writer-wins on duplicate checksum: cancel a prior waiter so a
        # re-pushed set doesn't strand callers.
        previous = self._avatar_set_waiters.pop(checksum, None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._avatar_set_waiters[checksum] = future

        msg = {
            "type": "avatar_set_fetch",
            "url": url,
            "token": token,
            "mode": mode,
            "checksum": checksum,
            "expected_size": expected_size,
        }
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._avatar_set_waiters.pop(checksum, None)
            return {"ok": False, "checksum": checksum, "error": "device_timeout"}
        except asyncio.CancelledError:
            return {"ok": False, "checksum": checksum, "error": "superseded"}
        except Exception as exc:
            self._avatar_set_waiters.pop(checksum, None)
            return {"ok": False, "checksum": checksum, "error": f"send_failed: {exc}"}

    def handle_avatar_set_loaded(self, payload: dict[str, Any]) -> None:
        """Resolve a pending send_avatar_set_fetch by checksum."""
        checksum = payload.get("checksum", "")
        future = self._avatar_set_waiters.pop(checksum, None)
        if future is not None and not future.done():
            future.set_result(payload)
        else:
            logger.warning("avatar_set_loaded for unknown checksum=%s (no pending waiter)", checksum)

    def handle_response(self, payload: dict[str, Any]) -> None:
        """Handle an incoming MCP response from ESP32."""
        req_id = payload.get("id")
        if req_id is not None and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(payload)
        else:
            # Notification (no id) — log and discard for now
            logger.info("ESP32 notification: %s", payload.get("method", ""))

    async def _ws_send(self, payload: bytes | str) -> None:
        """Send, translating websockets errors to ConnectionError.

        The ``websockets`` exceptions are *not* ConnectionError subclasses;
        without translation a mid-stream disconnect would leak as raw
        tracebacks past the orchestrator's ``except ConnectionError`` filter.
        """
        try:
            await self._ws.send(payload)
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            self.disconnect()  # fail fast on subsequent calls
            raise ConnectionError(f"WebSocket send failed: {exc}") from exc

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Send a single Opus frame as a WebSocket binary frame (TTS egress)."""
        self._require_connected()
        await self._ws_send(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification (``start`` / ``stop`` / ...).

        Brackets the audio frames so the device enters and exits
        ``kDeviceStateSpeaking``; without it the frames are dropped.
        """
        self._require_connected()
        await self._ws_send(json.dumps({"session_id": self.session_id, "type": "tts", "state": state}))

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        """Send a listen state notification (``start`` / ``stop``).

        ``mode`` is carried only for ``state=\"start\"``; the firmware
        accepts but ignores it in Phase 1 (manual stop boundaries).
        """
        self._require_connected()
        message: dict[str, Any] = {"session_id": self.session_id, "type": "listen", "state": state}
        if state == "start":
            message["mode"] = mode
        await self._ws_send(json.dumps(message))

    def disconnect(self) -> None:
        """Mark connection as disconnected and fail all pending futures."""
        self._connected = False
        self._initialized = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("ESP32 disconnected"))
        self._pending.clear()


class ESP32Manager:
    """Manages ESP32 device connections (currently a single device)."""

    def __init__(self, notify_config: NotifyConfig | None = None):
        self._connection: ESP32Connection | None = None
        self._server: Any = None
        self._lock = asyncio.Lock()
        self._notify_config = notify_config or load_notify_config()
        # Optional callbacks wired by the owning Gateway (optional so the
        # manager stays constructible standalone in tests).
        self.on_human_interaction: Callable[[], None] | None = None
        self.on_device_ready: Callable[[], Awaitable[None]] | None = None
        self.on_listen_started: Callable[[], Awaitable[None]] | None = None
        self._init_tasks: list[asyncio.Task] = []
        self._vision_url: str = ""
        self._vision_token: str = ""
        # Serialises the whole TTS start → frames → stop block so concurrent
        # say() calls cannot interleave Opus frames or yank the device out of
        # kDeviceStateSpeaking mid-utterance. STT capture shares this lock:
        # the firmware aborts in-flight TTS when listen.start arrives
        # mid-speaking, so the device audio path is one serialised resource.
        self._tts_lock = asyncio.Lock()
        self._listen_lock = self._tts_lock
        # Device-driven listen capture (wake word / button / LCD touch): on
        # inbound listen.start open the shared recording slot and on stop
        # forward the buffered Opus frames to the configured audio hook.
        self._audio_hook_url: str = ""
        self._audio_hook_token: str = ""
        # session_id that owns the device-driven recording slot (or None);
        # a stale disconnect must not clobber a fresh session's buffer.
        self._device_driven_session_id: str | None = None
        self._tool_lane_locks = {
            lane: asyncio.Lock()
            for lane in ("servo", "led", "avatar", "display", "audio", "camera", "touch", "status", "default")
        }

    def set_notify_config(self, notify_config: NotifyConfig) -> None:
        """Replace the startup notification config used for future events."""
        self._notify_config = notify_config

    @property
    def device_connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    @property
    def connection(self) -> ESP32Connection | None:
        return self._connection

    @property
    def tts_lock(self) -> asyncio.Lock:
        """Per-device lock guarding the TTS send sequence (see _tts_lock)."""
        return self._tts_lock

    @property
    def listen_lock(self) -> asyncio.Lock:
        """Per-device lock guarding the STT capture sequence (see _tts_lock)."""
        return self._listen_lock

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        vision_url: str = "",
        vision_token: str = "",
        audio_hook_url: str = "",
        audio_hook_token: str = "",
    ) -> None:
        """Start the WebSocket server for ESP32 connections."""
        self._vision_url = vision_url
        self._vision_token = vision_token
        self._audio_hook_url = audio_hook_url
        self._audio_hook_token = audio_hook_token
        if audio_hook_url:
            logger.info("Device-driven listen capture enabled (audio hook %s)", audio_hook_url)
        logger.info("ESP32 WebSocket server starting on ws://%s:%d", host, port)
        self._server = await websockets.serve(self._handler, host, port, process_request=self._check_auth)

    async def stop(self) -> None:
        """Stop the WebSocket server and cancel pending init tasks."""
        for task in self._init_tasks:
            task.cancel()
        self._init_tasks.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _check_auth(
        self, connection: ServerConnection, request: websockets.http11.Request
    ) -> None | websockets.http11.Response:
        """Validate the Bearer token (websockets 16+ process_request)."""
        expected = os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN")
        if not expected:
            logger.warning("STACKCHAN_TOKEN not set — accepting all connections")
            return None
        if request.headers.get("Authorization", "") == f"Bearer {expected}":
            return None
        logger.warning("ESP32 auth rejected")
        return websockets.http11.Response(401, "Unauthorized", websockets.datastructures.Headers())

    async def _handler(self, ws: ServerConnection) -> None:
        """Handle an incoming ESP32 WebSocket connection.

        The read loop runs continuously, dispatching MCP responses to
        pending futures; device initialization runs as a separate task so
        it never blocks this loop.
        """
        session_id = str(uuid.uuid4())
        device_id = ws.request.headers.get("Device-Id", "unknown") if ws.request else "unknown"
        logger.info("ESP32 connecting: device=%s", device_id)

        connection = ESP32Connection(ws, session_id)
        connection.device_id = device_id

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    # Binary = Opus audio frame: buffer for STT capture when a
                    # slot is open (v1 only; the orchestrator gates listen()).
                    await handle_audio_frame(message, session_id)
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from ESP32: %s", str(message)[:100])
                    continue

                msg_type = data.get("type", "")

                if msg_type == "hello":
                    if not data.get("features", {}).get("mcp"):
                        logger.warning("ESP32 does not support MCP, rejecting")
                        await ws.close()
                        return

                    # Capture the WS protocol version so callers can decide
                    # wire-format compatibility (raw Opus = v1 only).
                    raw_version = data.get("version", 1)
                    try:
                        connection.protocol_version = int(raw_version)
                    except (TypeError, ValueError):
                        connection.protocol_version = 1
                    if connection.protocol_version != 1:
                        logger.warning(
                            "ESP32 negotiated WebSocket protocol version=%s; the gateway emits raw "
                            "Opus binary frames matching v1 only. TTS calls (say) will be blocked "
                            "at the orchestrator until v2/v3 BinaryProtocol header wrapping is "
                            "implemented",
                            connection.protocol_version,
                        )

                    await ws.send(HelloResponse(session_id=session_id).model_dump_json())

                    async with self._lock:
                        if self._connection and self._connection.connected:
                            logger.warning("Replacing existing ESP32 connection")
                            self._connection.disconnect()
                        self._connection = connection

                    # Init runs detached so the read loop keeps pumping its responses.
                    task = asyncio.create_task(self._init_device(connection, device_id))
                    self._init_tasks.append(task)
                    task.add_done_callback(lambda t: self._init_tasks.remove(t) if t in self._init_tasks else None)

                elif msg_type == "mcp":
                    connection.handle_response(data.get("payload", {}))

                elif msg_type == "avatar_set_loaded":
                    # Device reports the result of a load_avatar_set fetch.
                    connection.handle_avatar_set_loaded(data)

                elif msg_type == "stackchan-event":
                    await self._emit_stackchan_event(data)

                elif msg_type == "listen":
                    # Device-driven listen start/stop (wake word, button, LCD
                    # touch). MCP-driven listen() opens its own recording slot,
                    # so we only act when the device initiated AND a hook URL
                    # is configured to receive the result.
                    await self._handle_device_listen(data, session_id)

                else:
                    logger.debug("ESP32 message type=%s (ignored)", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.info("ESP32 disconnected: device=%s", device_id)
        finally:
            # Drop a partial device-driven capture on disconnect, guarded by
            # session_id so a stale disconnect cannot tear down an unrelated
            # session's recording slot.
            if self._device_driven_session_id == session_id and is_recording_session(session_id):
                self._device_driven_session_id = None
                discarded = stop_recording()
                if discarded:
                    logger.warning(
                        "device-driven listen aborted mid-capture: session=%s discarded %d frames",
                        session_id, len(discarded),
                    )
            elif self._device_driven_session_id == session_id:
                # Our flag thinks we own the slot but audio_stream disagrees —
                # clear the flag without tearing down the slot.
                self._device_driven_session_id = None
            connection.disconnect()
            async with self._lock:
                if self._connection is connection:
                    self._connection = None

    async def _handle_device_listen(self, data: dict[str, Any], session_id: str) -> None:
        """Handle a device-driven listen start/stop notification."""
        state = data.get("state", "")
        if state == "start":
            if not self._audio_hook_url:
                logger.debug(
                    "device-driven listen.start session=%s ignored (STACKCHAN_AUDIO_HOOK_URL not configured)",
                    session_id,
                )
            elif is_recording():
                # MCP-driven listen() owns the slot; let it complete.
                logger.debug(
                    "device-driven listen.start session=%s ignored (MCP-driven recording active)",
                    session_id,
                )
            else:
                start_recording(session_id)
                self._device_driven_session_id = session_id
                logger.info(
                    "device-driven listen started: session=%s mode=%s",
                    session_id, data.get("mode", ""),
                )
                # Flash "I'm listening..." at record start, fire-and-forget so
                # the read loop never blocks on the display round-trip.
                if self.on_listen_started is not None:
                    asyncio.create_task(self._run_listen_started_hook())
        elif state == "stop":
            if self._device_driven_session_id == session_id:
                self._device_driven_session_id = None
                frames = stop_recording()
                logger.info(
                    "device-driven listen stopped: session=%s frames=%d",
                    session_id, len(frames),
                )
                # Push asynchronously; failures are logged inside push_audio_capture.
                asyncio.create_task(
                    push_audio_capture(
                        self._audio_hook_url, self._audio_hook_token, frames, session_id=session_id
                    )
                )
        else:
            logger.debug(
                "listen message with unknown state=%r session=%s",
                state, session_id,
            )

    async def _init_device(self, connection: ESP32Connection, device_id: str) -> None:
        """Initialize MCP session with a newly connected device."""
        if await connection.initialize(vision_url=self._vision_url, vision_token=self._vision_token):
            await connection.discover_tools()
            logger.info("ESP32 ready: device=%s tools=%d", device_id, len(connection.tools))
            # Let the owning Gateway re-apply persisted state (volume);
            # fire-and-forget so a slow hook never stalls the init task.
            if self.on_device_ready is not None:
                asyncio.create_task(self._run_device_ready_hook())
        else:
            logger.error("ESP32 MCP initialization failed")

    async def _run_hook(self, callback: Callable[[], Awaitable[None]] | None) -> None:
        """Invoke an optional async hook, never propagating failures."""
        if callback is None:
            return
        try:
            await callback()
        except Exception:
            logger.exception("gateway hook callback failed")

    async def _run_device_ready_hook(self) -> None:
        """Invoke the on_device_ready callback, never propagating."""
        await self._run_hook(self.on_device_ready)

    async def _run_listen_started_hook(self) -> None:
        """Invoke the on_listen_started callback, never propagating."""
        await self._run_hook(self.on_listen_started)

    async def _emit_stackchan_event(self, payload: dict[str, Any]) -> None:
        """Forward a firmware-originated stackchan event to the MCP client."""
        event_type = payload.get("event_type")
        subtype = payload.get("subtype")
        duration_ms = payload.get("duration_ms")
        ts = payload.get("ts")
        session_id = payload.get("session_id")
        if event_type != "touch":
            logger.warning("Malformed stackchan-event frame: event_type=%r", event_type)
            return
        if subtype not in {"tap", "stroke"}:
            logger.warning("Malformed stackchan-event frame: subtype=%r", subtype)
            return
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            logger.warning("Malformed stackchan-event frame: duration_ms=%r", duration_ms)
            return
        if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0:
            logger.warning("Malformed stackchan-event frame: ts=%r", ts)
            return
        if not isinstance(session_id, str) or not session_id:
            logger.warning("Malformed stackchan-event frame: session_id=%r", session_id)
            return

        if self.on_human_interaction is not None:
            try:
                self.on_human_interaction()
            except Exception:
                logger.exception("on_human_interaction callback failed")

        config = self._notify_config
        message = config.messages.get((event_type, subtype), DEFAULT_MESSAGE_TEMPLATES[(event_type, subtype)])
        ts_unix = time.time()
        event_payload = {
            "event_type": event_type,
            "subtype": subtype,
            "duration_ms": duration_ms,
            "action": message.action,
            "ts": ts,
            "ts_unix": ts_unix,
            "session_id": session_id,
        }
        legacy_params = {k: v for k, v in event_payload.items() if k != "ts_unix"}
        logger.info(
            "stackchan-event: %s/%s action=%s duration=%sms ts=%s session=%s",
            event_type, subtype, message.action, duration_ms, ts, session_id,
        )

        if not (config.legacy_event_enabled or config.channels_enabled or config.jsonl_enabled):
            logger.info("stackchan-event received and dropped: notification paths disabled")
            return

        from .stdio_server import notify_stackchan_event

        if config.legacy_event_enabled:
            await notify_stackchan_event("stackchan/event", legacy_params)

        if config.channels_enabled:
            content = render_template(message.template, event_payload)
            # Channel meta must be all-string per the CC binary's Zod schema.
            channel_meta = {
                k: (str(v) if k in ("duration_ms", "ts", "ts_unix") else v)
                for k, v in event_payload.items()
            }
            await notify_stackchan_event(
                "notifications/claude/channel", {"content": content, "meta": channel_meta}
            )

        if config.jsonl_enabled:
            # log_event swallows OS/permission errors; the broad except is a
            # second-tier guard so a helper bug cannot break the paths above.
            from .event_log import log_event

            try:
                log_event(
                    event_type=event_type,
                    subtype=subtype,
                    duration_ms=duration_ms,
                    ts=ts,
                    session_id=session_id,
                    action=message.action,
                    path=config.jsonl_path,
                    ts_unix=ts_unix,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("stackchan-event log persistence raised unexpectedly: %s", exc)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Call a tool on the connected ESP32 device."""
        result = await self.call_tools([(name, arguments)])
        return result[0]

    async def call_tools(self, calls: Sequence[ToolCall]) -> list[ToolCallResult]:
        """Call multiple tools, serialising same-lane calls while overlapping
        hardware-independent peripherals (servo + LEDs + avatar, etc.)."""
        if not calls:
            return []
        if not self._connection or not self._connection.connected:
            return [(None, {"code": -32000, "message": "No ESP32 device connected"}) for _ in calls]
        if not self._connection.initialized:
            return [(None, {"code": -32000, "message": "ESP32 not initialized"}) for _ in calls]

        connection = self._connection
        return list(
            await asyncio.gather(
                *(self._call_tool_on_connection(connection, name, arguments) for name, arguments in calls)
            )
        )

    async def _call_tool_on_connection(
        self, connection: ESP32Connection, name: str, arguments: dict[str, Any]
    ) -> ToolCallResult:
        lane = _hardware_lane(name)
        async with self._tool_lane_locks[lane]:
            if connection is not self._connection or not connection.connected:
                return None, {"code": -32000, "message": "ESP32 not connected"}
            return await connection.call_tool(name, arguments)

    async def send_avatar_set_fetch(
        self, url: str, token: str, mode: str, checksum: str, expected_size: int, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Forward an avatar_set_fetch to the device and await the reply.

        Returns a synthetic {ok: False, error: "no_device"} when no device
        is connected so the MCP tool surfaces a clean error JSON.
        """
        if not self._connection or not self._connection.connected:
            return {"ok": False, "checksum": checksum, "error": "no_device"}
        return await self._connection.send_avatar_set_fetch(url, token, mode, checksum, expected_size, timeout)

    def _connection_or_raise(self) -> ESP32Connection:
        """Return the connected device, raising if none is attached."""
        conn = self._connection
        if conn is None or not conn.connected:
            raise ConnectionError("No ESP32 device connected")
        return conn

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Push a single Opus frame to the connected device (TTS pipeline)."""
        await self._connection_or_raise().send_audio_frame(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification; see ESP32Connection.send_tts_state."""
        await self._connection_or_raise().send_tts_state(state)

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        """Send a listen state notification; see ESP32Connection.send_listen_state."""
        await self._connection_or_raise().send_listen_state(state, mode=mode)

    def get_status(self) -> dict[str, Any]:
        """Get current connection status."""
        if not self._connection or not self._connection.connected:
            return {"connected": False, "device_id": None, "tools_count": 0}
        return {
            "connected": True,
            "device_id": self._connection.device_id,
            "initialized": self._connection.initialized,
            "tools_count": len(self._connection.tools),
            "tools": [t.get("name", "") for t in self._connection.tools],
        }
