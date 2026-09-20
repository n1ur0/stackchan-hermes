"""Tests for ESP32 client connection management."""

import asyncio
import gc
import json
from contextlib import asynccontextmanager, nullcontext
from typing import Any

import pytest
import pytest_asyncio
import websockets

from stackchan_mcp.esp32_client import ESP32Connection, ESP32Manager, _hardware_lane


async def _start_manager(**kwargs) -> ESP32Manager:
    """Create and start an ESP32Manager on a free port; record its port."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0, **kwargs)  # Port 0 = OS picks a free port
    mgr._test_port = mgr._server.sockets[0].getsockname()[1]
    return mgr


@asynccontextmanager
async def _connect(manager):
    """Open a websocket to the manager's test port."""
    async with websockets.connect(f"ws://127.0.0.1:{manager._test_port}") as ws:
        yield ws


@pytest_asyncio.fixture
async def manager():
    """Create and start an ESP32Manager on a free port."""
    mgr = await _start_manager()
    yield mgr
    await mgr.stop()


async def test_manager_starts_and_stops():
    """Manager can start and stop cleanly."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    assert mgr._server is not None
    await mgr.stop()
    assert mgr._server is None


async def test_no_device_connected():
    """call_tool returns error when no device is connected."""
    mgr = ESP32Manager()
    result, error = await mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 0})
    assert result is None
    assert error is not None
    assert "not connected" in error["message"].lower() or "No ESP32" in error["message"]


async def test_get_status_disconnected():
    """get_status returns disconnected state."""
    mgr = ESP32Manager()
    status = mgr.get_status()
    assert status["connected"] is False
    assert status["device_id"] is None


async def test_esp32_hello_handshake(manager):
    """ESP32 can connect and complete hello handshake."""
    async with _connect(manager) as ws:
        # Send hello and receive the hello response
        resp = await _send_hello(
            ws,
            audio_params={
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        )
        assert resp["type"] == "hello"
        assert resp["version"] == 1
        assert "session_id" in resp

        # Receive initialize request from gateway and respond
        init_msg = await _recv_mcp(ws)
        assert init_msg["type"] == "mcp"
        assert init_msg["payload"]["method"] == "initialize"
        await ws.send(json.dumps(_mcp_response(init_msg, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-device", "version": "1.0.0"},
        })))

        # Receive tools/list request and respond
        tools_msg = await _recv_mcp(ws)
        assert tools_msg["type"] == "mcp"
        assert tools_msg["payload"]["method"] == "tools/list"
        await ws.send(json.dumps(_mcp_response(tools_msg, {
            "tools": [
                {
                    "name": "self.robot.set_head_angles",
                    "description": "Set head angles",
                    "inputSchema": {"type": "object"},
                }
            ],
            "nextCursor": "",
        })))

        # Wait for manager to process
        await asyncio.sleep(0.2)

        # Verify connection is established
        assert manager.device_connected is True
        status = manager.get_status()
        assert status["connected"] is True
        assert status["tools_count"] == 1


async def test_esp32_tool_call_relay(manager):
    """Gateway relays tool calls to ESP32."""
    async with _connect(manager) as ws:
        # Complete handshake
        await _complete_handshake(ws, tools=[
            {"name": "self.robot.set_head_angles", "description": "Set head", "inputSchema": {}}
        ])

        await asyncio.sleep(0.2)

        # Now call tool via manager
        call_task = asyncio.create_task(
            manager.call_tool("self.robot.set_head_angles", {"yaw": 45, "pitch": 10})
        )

        # ESP32 receives the request
        req_msg = await _recv_mcp(ws)
        assert req_msg["type"] == "mcp"
        assert req_msg["payload"]["method"] == "tools/call"
        assert req_msg["payload"]["params"]["name"] == "self.robot.set_head_angles"
        assert req_msg["payload"]["params"]["arguments"] == {"yaw": 45, "pitch": 10}

        # ESP32 sends response
        await ws.send(json.dumps(_mcp_response(req_msg, {
            "content": [{"type": "text", "text": "true"}],
            "isError": False,
        })))

        # Verify result
        result, error = await asyncio.wait_for(call_task, timeout=5.0)
        assert error is None
        assert result["content"][0]["text"] == "true"


async def test_esp32_disconnect_handling(manager):
    """Manager handles ESP32 disconnection gracefully."""
    async with _connect(manager) as ws:
        await _complete_handshake(ws)
        await asyncio.sleep(0.2)
        assert manager.device_connected is True

    # Connection closed
    await asyncio.sleep(0.2)
    assert manager.device_connected is False


async def test_auth_rejection(manager):
    """Unauthorized connections are rejected."""
    import os

    # Set token to require auth
    os.environ["STACKCHAN_TOKEN"] = "test-secret-token"
    try:
        # Try connecting without auth — should fail
        with pytest.raises(Exception):
            async with websockets.connect(
                f"ws://127.0.0.1:{manager._test_port}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ) as ws:
                await ws.recv()
    finally:
        del os.environ["STACKCHAN_TOKEN"]


# ---------------------------------------------------------------------------
# Parallel hardware-lane dispatch (Issue #73)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "lane"),
    [
        ("self.robot.set_head_angles", "servo"),
        ("self.led.set_many", "led"),
        ("self.display.set_avatar", "avatar"),
        ("self.screen.set_brightness", "display"),
        ("self.audio_speaker.set_volume", "audio"),
        ("self.camera.take_photo", "camera"),
        ("self.touch.get_touch_state", "touch"),
        ("self.get_device_status", "status"),
        ("self.unknown.experimental", "default"),
    ],
)
def test_hardware_lane_covers_gateway_tool_routes(tool_name, lane):
    """Gateway-routed ESP32 tools map to explicit hardware lanes."""
    assert _hardware_lane(tool_name) == lane


async def test_connection_pipelines_concurrent_tool_calls_before_first_response():
    """Concurrent tools/call requests are sent before either response arrives."""
    ws, conn = _make_conn(session_id="session-parallel")

    servo_task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )
    led_task = asyncio.create_task(
        conn.call_tool("self.led.set_many", {"colors": "[[255, 0, 0]]"})
    )

    await asyncio.sleep(0)

    assert len(ws.sent) == 2
    sent_messages = [json.loads(message) for message in ws.sent]
    request_ids = [message["payload"]["id"] for message in sent_messages]
    assert [message["payload"]["method"] for message in sent_messages] == [
        "tools/call",
        "tools/call",
    ]
    assert [message["payload"]["params"]["name"] for message in sent_messages] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]

    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[1],
            "result": {"content": [{"type": "text", "text": "led"}]},
        }
    )
    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[0],
            "result": {"content": [{"type": "text", "text": "servo"}]},
        }
    )

    servo_result, led_result = await asyncio.gather(servo_task, led_task)
    assert servo_result[0]["content"][0]["text"] == "servo"
    assert servo_result[1] is None
    assert led_result[0]["content"][0]["text"] == "led"
    assert led_result[1] is None


async def test_connection_removes_pending_request_when_call_is_cancelled():
    """Cancelling a tool call does not leave a stale pending response slot."""
    ws, conn = _make_conn(session_id="session-cancel")

    task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )

    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    assert len(conn._pending) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn._pending == {}


class _GateableConnection:
    """Fake initialized connection with per-tool release gates."""

    connected = True
    initialized = True

    def __init__(self, releases: dict[str, asyncio.Event]) -> None:
        self.releases = releases
        self.started: list[str] = []
        self.finished: list[str] = []
        self.all_started = asyncio.Event()

    async def call_tool(self, name, arguments):  # noqa: ARG002 - test fake
        self.started.append(name)
        if len(self.started) >= len(self.releases):
            self.all_started.set()
        await self.releases[name].wait()
        self.finished.append(name)
        return {"content": [{"type": "text", "text": name}]}, None


def _gate_mgr(tool_names):
    """Manager whose connection gates each tool call on its own release event."""
    releases = {name: asyncio.Event() for name in tool_names}
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]
    return mgr, connection


async def test_manager_call_tools_dispatches_independent_lanes_in_parallel():
    """Servo, LED, and avatar calls start together instead of waiting in line."""
    mgr, connection = _gate_mgr([
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ])

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.led.set_many", {"colors": "[]"}),
                ("self.display.set_avatar", {"face": "happy"}),
            ]
        )
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert connection.finished == []

    for release in connection.releases.values():
        release.set()
    results = await asyncio.wait_for(task, timeout=1.0)

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert [error for _, error in results] == [None, None, None]


async def test_manager_call_tool_uses_lane_dispatch_for_existing_api():
    """Existing single-tool API can still overlap independent hardware lanes."""
    mgr, connection = _gate_mgr([
        "self.robot.set_head_angles",
        "self.led.set_many",
    ])

    servo_task = asyncio.create_task(
        mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 45})
    )
    led_task = asyncio.create_task(
        mgr.call_tool("self.led.set_many", {"colors": "[]"})
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert connection.finished == []

    for release in connection.releases.values():
        release.set()
    results = await asyncio.wait_for(
        asyncio.gather(servo_task, led_task),
        timeout=1.0,
    )

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert [error for _, error in results] == [None, None]


async def test_manager_call_tools_serializes_calls_on_same_hardware_lane():
    """Two servo calls keep their relative order on the servo lane."""
    mgr, connection = _gate_mgr([
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ])

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.robot.get_head_angles", {}),
            ]
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == ["self.robot.set_head_angles"]

    connection.releases["self.robot.set_head_angles"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]

    connection.releases["self.robot.get_head_angles"].set()
    await asyncio.wait_for(task, timeout=1.0)
    assert connection.finished == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]


# ---------------------------------------------------------------------------
# send_audio_frame (TTS pipeline egress, Issue #70 PR2)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for websockets.ServerConnection used in unit tests."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, data):
        self.sent.append(data)


@pytest.mark.parametrize(
    ("session_id", "method", "kwargs", "expected_sent", "disconnect"),
    [
        ("session-1", "send_audio_frame", {"opus_frame": b"opus_payload_bytes"}, [b"opus_payload_bytes"], False),
        (
            "session-tts",
            "send_tts_state",
            {"state": "start"},
            [{"session_id": "session-tts", "type": "tts", "state": "start"}],
            False,
        ),
        (
            "session-listen",
            "send_listen_state",
            {"state": "start", "mode": "manual"},
            [{"session_id": "session-listen", "type": "listen", "state": "start", "mode": "manual"}],
            False,
        ),
        (
            "session-listen",
            "send_listen_state",
            {"state": "stop"},
            [{"session_id": "session-listen", "type": "listen", "state": "stop"}],
            False,
        ),
        ("session-1", "send_audio_frame", {"opus_frame": b"opus_payload_bytes"}, [], True),
        ("session-tts", "send_tts_state", {"state": "stop"}, [], True),
        ("session-listen", "send_listen_state", {"state": "start", "mode": "manual"}, [], True),
    ],
    ids=[
        "audio_frame_sends_binary",
        "tts_state_sends_json",
        "listen_state_start_includes_mode",
        "listen_state_stop_omits_mode",
        "audio_frame_raises_after_disconnect",
        "tts_state_raises_after_disconnect",
        "listen_state_raises_after_disconnect",
    ],
)
async def test_connection_send_wire_format(session_id, method, kwargs, expected_sent, disconnect):
    """Outbound sends hit the wire in the expected shape; a disconnected
    connection refuses to send rather than silently dropping."""
    ws, conn = _make_conn(session_id=session_id)

    if disconnect:
        conn.disconnect()
    with pytest.raises(ConnectionError) if disconnect else nullcontext():
        await getattr(conn, method)(**kwargs)

    if not expected_sent:
        assert ws.sent == []
    elif isinstance(expected_sent[0], bytes):
        assert ws.sent == expected_sent
    else:
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0]) == expected_sent[0]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("send_audio_frame", {"opus_frame": b"opus_payload_bytes"}),
        ("send_tts_state", {"state": "start"}),
        ("send_listen_state", {"state": "start"}),
    ],
    ids=["audio_frame", "tts_state", "listen_state"],
)
async def test_manager_send_raises_without_device(method, kwargs):
    """ESP32Manager raises ConnectionError when no device is attached.

    The orchestrator turns this into a clean MCP error JSON; without
    this guard the call would AttributeError on a None connection.
    """
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await getattr(mgr, method)(**kwargs)


def test_manager_listen_lock_is_same_as_tts_lock():
    """listen() and say() share a single audio-path lock per device.

    Without sharing, the firmware's ``HandleStartListeningEvent`` could
    abort an in-flight ``say()`` mid-utterance, and TTS frames in flight
    would leak into a concurrent capture's buffer. Treating the audio
    path as a single serialised resource keeps the device's state
    machine observable from the gateway side.
    """
    mgr = ESP32Manager()
    assert mgr.tts_lock is mgr.listen_lock


class _FailingWebSocket:
    """WebSocket that raises a websockets-specific error on send()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.send_calls = 0

    async def send(self, data):
        self.send_calls += 1
        raise self._exc


async def test_send_audio_frame_translates_websockets_close_to_connection_error():
    """websockets.ConnectionClosed becomes ConnectionError + marks dead.

    Without translation the websockets-specific exception would
    bypass the orchestrator's ``except ConnectionError`` filter and
    leak as a stack trace through the MCP transport.
    """
    import websockets.exceptions

    closed = websockets.exceptions.ConnectionClosed(rcvd=None, sent=None)
    ws, conn = _make_conn(ws=_FailingWebSocket(closed))

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_audio_frame(b"opus")

    # After the translated failure, the connection is marked dead so
    # subsequent sends fail fast without re-touching the dead socket.
    assert not conn.connected
    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"more")
    assert ws.send_calls == 1


async def test_send_tts_state_translates_oserror_to_connection_error():
    """OSError on send (e.g. broken pipe) is translated to ConnectionError."""
    ws, conn = _make_conn(ws=_FailingWebSocket(OSError("broken pipe")))

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_tts_state("start")
    assert not conn.connected


async def test_send_mcp_request_translates_send_failure_and_marks_disconnected():
    """tools/call send failures use the same connection-state handling as TTS."""
    ws, conn = _make_conn(ws=_FailingWebSocket(OSError("broken pipe")))
    loop = asyncio.get_running_loop()
    loop_errors = []
    previous_handler = loop.get_exception_handler()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        result, error = await conn.call_tool("self.robot.set_head_angles", {})
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result is None
    assert error is not None
    assert "WebSocket send failed" in error["message"]
    assert not conn.connected
    assert conn._pending == {}
    assert ws.send_calls == 1
    assert loop_errors == []


def test_connection_default_protocol_version_is_one():
    """Fresh ESP32Connection defaults to WebSocket protocol v1.

    v1 is what the gateway's audio framing currently targets (raw
    Opus binary frames); v2/v3 wrap payloads in a BinaryProtocol
    header this gateway does not yet emit, so the hello handler logs
    a warning when a non-v1 device negotiates.
    """
    _, conn = _make_conn()

    assert conn.protocol_version == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(session_id="session-1", ws=None) -> tuple[Any, ESP32Connection]:
    """Return (ws, conn) with an ESP32Connection over a fake WebSocket."""
    if ws is None:
        ws = _FakeWebSocket()
    return ws, ESP32Connection(ws, session_id=session_id)  # type: ignore[arg-type]


async def _recv_mcp(ws):
    """Receive and parse the next gateway message."""
    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    return json.loads(raw)


def _mcp_response(request, result):
    """Build an MCP jsonrpc response echoing the request id and session."""
    return {
        "session_id": request["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": request["payload"]["id"],
            "result": result,
        },
    }


async def _send_hello(ws, *, version=1, features=None, audio_params=None):
    """Send a hello and return the parsed hello response."""
    hello = {
        "type": "hello",
        "version": version,
        "features": {"mcp": True} if features is None else features,
        "transport": "websocket",
    }
    if audio_params is not None:
        hello["audio_params"] = audio_params
    await ws.send(json.dumps(hello))
    return await _recv_mcp(ws)


async def _complete_handshake(ws, tools=None):
    """Complete the full ESP32 handshake sequence."""
    if tools is None:
        tools = []

    # Send hello and consume the hello response
    await _send_hello(ws)

    # Receive and respond to initialize
    init_msg = await _recv_mcp(ws)
    await ws.send(json.dumps(_mcp_response(init_msg, {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "test-device", "version": "1.0.0"},
    })))

    # Receive and respond to tools/list
    tools_msg = await _recv_mcp(ws)
    await ws.send(json.dumps(_mcp_response(tools_msg, {"tools": tools, "nextCursor": ""})))


def _listen_message(state, mode=None, *, session_id=None):
    """Wire payload for a device-driven listen state change."""
    msg = {"type": "listen", "state": state}
    if session_id is not None:
        msg["session_id"] = session_id
    if mode is not None:
        msg["mode"] = mode
    return json.dumps(msg)


async def _wait_until(predicate, *, timeout=1.0, interval=0.05):
    """Poll until predicate() turns truthy or the deadline passes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --- Device-driven listen capture --------------------------------------------


@pytest_asyncio.fixture
async def manager_with_hook(monkeypatch):
    """ESP32Manager started with a configured audio hook URL.

    ``push_audio_capture`` is patched to record invocations into a
    shared list so tests can assert the hook was triggered without
    starting a real HTTP server. The recorded payload is the actual
    ``frames`` list the gateway captured for that listen window.
    """
    calls: list[dict] = []

    async def _fake_push(hook_url, token, frames, *, session_id="", timeout_s=10.0):
        calls.append(
            {
                "hook_url": hook_url,
                "token": token,
                "frames": list(frames),
                "session_id": session_id,
            }
        )
        return True

    monkeypatch.setattr(
        "stackchan_mcp.esp32_client.push_audio_capture", _fake_push
    )

    mgr = await _start_manager(
        audio_hook_url="http://test/hook",
        audio_hook_token="test-token",
    )

    try:
        yield mgr, calls
    finally:
        await mgr.stop()


async def test_device_driven_listen_pushes_to_hook(manager_with_hook):
    """device → gateway listen.start/stop sequence forwards frames
    captured between the two messages to the audio hook."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook

    async with _connect(mgr) as ws:
        await _complete_handshake(ws)

        # Device-initiated listen.start
        await ws.send(_listen_message("start", mode="manual", session_id=""))

        # Wait for gateway to open the recording slot. We can't observe
        # the gateway's internals through the WS, so poll the module
        # state for a short bounded time.
        assert await _wait_until(is_recording), "gateway did not open the recording slot"

        # Stream a couple of binary "audio" frames
        await ws.send(b"\xaa\xbb\xcc")
        await ws.send(b"\xdd\xee\xff")

        # Give the gateway a moment to buffer the frames
        await asyncio.sleep(0.1)

        # Device-initiated listen.stop
        await ws.send(_listen_message("stop"))

        # Wait for the push task to fire (asyncio.create_task in the
        # handler dispatches it eagerly; one event-loop tick is enough,
        # but we give it a few to absorb scheduling jitter).
        await _wait_until(lambda: bool(calls))

    assert len(calls) == 1
    assert calls[0]["hook_url"] == "http://test/hook"
    assert calls[0]["token"] == "test-token"
    assert calls[0]["frames"] == [b"\xaa\xbb\xcc", b"\xdd\xee\xff"]


async def test_device_driven_listen_disabled_when_no_hook(manager):
    """Without STACKCHAN_AUDIO_HOOK_URL the gateway ignores inbound
    listen.start (no recording slot opens, no push fires)."""
    from stackchan_mcp.audio_stream import is_recording

    async with _connect(manager) as ws:
        await _complete_handshake(ws)

        await ws.send(_listen_message("start", mode="manual"))
        # Give the gateway time to NOT do anything.
        await asyncio.sleep(0.2)
        assert not is_recording()


async def test_device_driven_listen_cleanup_on_disconnect(manager_with_hook):
    """Disconnecting mid-capture drops the partial buffer rather than
    leaking it into the next connection's recording slot."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook

    async with _connect(mgr) as ws:
        await _complete_handshake(ws)
        await ws.send(_listen_message("start", mode="manual"))
        assert await _wait_until(is_recording)
        await ws.send(b"\x11\x22\x33")
        await asyncio.sleep(0.05)
        # Drop the connection without sending listen.stop.

    # Give the server-side handler's finally clause time to run.
    assert await _wait_until(lambda: not is_recording()), "recording slot was leaked across connections"
    # No push should have fired for the aborted capture.
    assert calls == []


# ---- Phase F: on_device_ready / on_listen_started hooks ---------------------


_HOOK_CASES = [
    ("invokes", "on_device_ready", "_run_device_ready_hook"),
    ("invokes", "on_listen_started", "_run_listen_started_hook"),
    ("swallows", "on_device_ready", "_run_device_ready_hook"),
    ("swallows", "on_listen_started", "_run_listen_started_hook"),
    ("noop", "on_device_ready", "_run_device_ready_hook"),
    ("noop", "on_listen_started", "_run_listen_started_hook"),
]


@pytest.mark.parametrize(
    ("kind", "hook_attr", "run_method"),
    _HOOK_CASES,
    ids=[
        "invokes_device_ready",
        "invokes_listen_started",
        "swallows_device_ready",
        "swallows_listen_started",
        "noop_device_ready",
        "noop_listen_started",
    ],
)
async def test_run_hook(kind, hook_attr, run_method):
    """_run_*_hook awaits a registered callback, swallows callback errors,
    and is a quiet no-op when no callback is registered."""
    mgr = ESP32Manager()

    if kind == "noop":
        assert getattr(mgr, hook_attr) is None
        await getattr(mgr, run_method)()
        return

    fired = asyncio.Event()

    async def cb():
        if kind == "swallows":
            raise RuntimeError("boom")
        fired.set()

    setattr(mgr, hook_attr, cb)
    # Must not raise.
    await getattr(mgr, run_method)()
    if kind == "invokes":
        assert fired.is_set()


async def test_device_driven_listen_fires_listen_started(manager_with_hook):
    """The on_listen_started hook fires the instant a device-driven
    listen opens the recording slot — before the capture finishes."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, _calls = manager_with_hook
    fired = asyncio.Event()

    async def cb():
        fired.set()

    mgr.on_listen_started = cb

    async with _connect(mgr) as ws:
        await _complete_handshake(ws)
        await ws.send(_listen_message("start", mode="manual"))
        # The hook fires at recording start; no listen.stop needed.
        await _wait_until(fired.is_set)
        assert is_recording()
        assert fired.is_set(), "on_listen_started did not fire at record start"
