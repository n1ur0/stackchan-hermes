from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.types import ErrorData, TextContent

from stackchan_mcp.http_server import (
    AUTH_FAILURE_MESSAGE,
    BYPASS_TOOLS,
    DISCONNECTED_DEVICE_PAYLOAD,
    HOST_FAILURE_MESSAGE,
    ORIGIN_FAILURE_MESSAGE,
    MCP_HTTP_ALLOWED_HOSTS_ENV,
    build_app,
    make_dispatch_fn,
)
from stackchan_mcp import sensors
from stackchan_mcp.queue import CommandQueue, QueueFull, QueueItem, build_queue_full_error

class FakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []

    def get_status(self) -> dict:
        return {
            "connected": self.device_connected,
            "device": "fake-stackchan",
        }

    async def call_tool(self, name: str, arguments: dict) -> tuple[dict, None]:
        self.calls.append((name, arguments))
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"name": name, "arguments": arguments}),
                }
            ],
        }, None

class FakeGateway:
    def __init__(self, *, connected: bool = True) -> None:
        self.esp32 = FakeESP32(connected=connected)

@contextlib.asynccontextmanager
async def _client(app, *, base_url: str = "http://127.0.0.1:8767") -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client

def _headers(session_id: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {"accept": "application/json"}
    if session_id is not None:
        headers[MCP_SESSION_ID_HEADER] = session_id
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers

async def _initialize(client: httpx.AsyncClient, *, token: str | None = None, request_id: int = 1) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        headers=_headers(token=token),
    )
    assert response.status_code == 200
    session_id = response.headers[MCP_SESSION_ID_HEADER]
    initialized = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers(session_id, token),
    )
    assert initialized.status_code == 202
    return session_id

async def _call_tool(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    name: str,
    arguments: dict | None = None,
    request_id: int | str = 2,
    token: str | None = None,
) -> httpx.Response:
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers=_headers(session_id, token),
    )

async def _wait_for_queue_depth(queue: CommandQueue, depth: int) -> None:
    for _ in range(50):
        if queue.depth == depth:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"queue depth did not reach {depth}")

class GatedCommandQueue(CommandQueue):
    def __init__(self, capacity: int) -> None:
        super().__init__(capacity=capacity)
        self.allow_get = asyncio.Event()
        self.get_started = asyncio.Event()
        self.enqueued_task: asyncio.Task | None = None
        self.last_enqueued_item: QueueItem | None = None

    def enqueue(self, item: QueueItem) -> None:
        task = asyncio.current_task()
        self.enqueued_task = task if isinstance(task, asyncio.Task) else None
        self.last_enqueued_item = item
        super().enqueue(item)

    async def get(self) -> QueueItem:
        self.get_started.set()
        await self.allow_get.wait()
        return await super().get()

async def test_queue_ordering_fifo_completion() -> None:
    queue = CommandQueue(capacity=3)
    observed: list[str] = []
    loop = asyncio.get_running_loop()
    futures = [loop.create_future() for _ in range(3)]

    for index, future in enumerate(futures):
        queue.enqueue(
            QueueItem(
                correlation_id=f"item-{index}",
                client_session_id=None,
                client_request_id=index,
                tool_name=f"tool-{index}",
                arguments={},
                response_future=future,
                enqueued_at=0.0,
            )
        )

    async def dispatch(item: QueueItem) -> str:
        observed.append(item.tool_name)
        return item.tool_name

    dispatcher = asyncio.create_task(queue.run_dispatcher(dispatch))
    try:
        results = await asyncio.gather(*futures)
    finally:
        dispatcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher

    assert observed == ["tool-0", "tool-1", "tool-2"]
    assert results == observed

async def test_queue_full_returns_jsonrpc_error_response() -> None:
    queue = CommandQueue(capacity=1)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        first = asyncio.create_task(
            _call_tool(client, session_id=session_id, name="get_device_info", request_id=10)
        )
        await _wait_for_queue_depth(queue, 1)
        second = await _call_tool(
            client,
            session_id=session_id,
            name="get_head_angles",
            request_id=11,
        )
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

    assert second.status_code == 200
    payload = second.json()
    assert payload["id"] == 11
    assert payload["error"] == build_queue_full_error(1)

async def test_cancelled_client_item_is_not_dispatched() -> None:
    queue = GatedCommandQueue(capacity=2)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        call_task = asyncio.create_task(
            _call_tool(
                client,
                session_id=session_id,
                name="get_device_info",
            )
        )
        await _wait_for_queue_depth(queue, 1)

        assert queue.enqueued_task is not None
        queue.enqueued_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call_task
        assert queue.last_enqueued_item is not None
        assert queue.last_enqueued_item.response_future.cancelled()

        queue.allow_get.set()
        await _wait_for_queue_depth(queue, 0)
        await asyncio.sleep(0.01)

    assert dispatched == []

async def test_lifespan_shutdown_drains_pending_queue_items() -> None:
    queue = GatedCommandQueue(capacity=3)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    loop = asyncio.get_running_loop()
    futures = [loop.create_future() for _ in range(2)]

    async with app.router.lifespan_context(app):
        for index, future in enumerate(futures):
            queue.enqueue(
                QueueItem(
                    correlation_id=f"pending-{index}",
                    client_session_id=None,
                    client_request_id=index,
                    tool_name=f"tool-{index}",
                    arguments={},
                    response_future=future,
                    enqueued_at=0.0,
                )
            )
        await _wait_for_queue_depth(queue, 2)

    assert queue.depth == 0
    assert dispatched == []
    for future in futures:
        assert future.done()
        result = future.result()
        assert isinstance(result, ErrorData)
        assert result.code == -32000
        assert result.message == "stackchan MCP HTTP server is shutting down"
        assert result.data == {"reason": "server_shutdown"}

async def test_auth_rejection_and_successful_bearer_reaches_dispatcher() -> None:
    queue = CommandQueue(capacity=4)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token="secret",
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        missing = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=_headers(),
        )
        wrong = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=_headers(token="wrong"),
        )
        session_id = await _initialize(client, token="secret")
        ok = await _call_tool(
            client,
            session_id=session_id,
            token="secret",
            name="get_device_info",
        )

    assert missing.status_code == 401
    assert missing.text == AUTH_FAILURE_MESSAGE
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert dispatched == ["get_device_info"]

async def test_host_and_origin_rebinding_guards_return_403() -> None:
    app = build_app(
        CommandQueue(capacity=2),
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
    )

    async with _client(app) as client:
        bad_host = await client.get(
            "/healthz",
            headers={"host": "evil.example:8767"},
        )
        bad_origin = await client.get(
            "/healthz",
            headers={"origin": "http://evil.example:8767"},
        )

    assert bad_host.status_code == 403
    assert bad_host.text == HOST_FAILURE_MESSAGE
    assert bad_origin.status_code == 403
    assert bad_origin.text == ORIGIN_FAILURE_MESSAGE

async def test_wildcard_bind_allows_loopback_and_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        MCP_HTTP_ALLOWED_HOSTS_ENV,
        "192.168.1.10, https://stackchan.example.test:9443",
    )
    app = build_app(
        CommandQueue(capacity=2),
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="0.0.0.0",
        port=8767,
    )

    async with _client(app) as client:
        loopback = await client.get("/healthz", headers={"host": "127.0.0.1:8767"})
        lan = await client.get("/healthz", headers={"host": "192.168.1.10:8767"})
        origin = await client.get(
            "/healthz",
            headers={
                "host": "stackchan.example.test:9443",
                "origin": "https://stackchan.example.test:9443",
            },
        )

    assert loopback.status_code == 200
    assert lan.status_code == 200
    assert origin.status_code == 200

async def test_response_correlation_for_two_concurrent_clients() -> None:
    queue = CommandQueue(capacity=4)

    async def dispatch(item: QueueItem):
        await asyncio.sleep(0.01)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "client_request_id": item.client_request_id,
                        "tool_name": item.tool_name,
                    }
                ),
            )
        ]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_a = await _initialize(client, request_id=1)
        session_b = await _initialize(client, request_id=2)
        response_a, response_b = await asyncio.gather(
            _call_tool(
                client,
                session_id=session_a,
                name="get_device_info",
                request_id="client-a",
            ),
            _call_tool(
                client,
                session_id=session_b,
                name="get_head_angles",
                request_id="client-b",
            ),
        )

    payload_a = response_a.json()
    payload_b = response_b.json()
    assert payload_a["id"] == "client-a"
    assert payload_b["id"] == "client-b"
    body_a = json.loads(payload_a["result"]["content"][0]["text"])
    body_b = json.loads(payload_b["result"]["content"][0]["text"])
    assert body_a["client_request_id"] == "client-a"
    assert body_b["client_request_id"] == "client-b"

async def test_bypass_tool_get_status_does_not_enter_dispatcher() -> None:
    assert BYPASS_TOOLS == frozenset(
        {
            "get_status",
            "get_presence",
            "switchbot_list_devices",
            "switchbot_get_status",
            "switchbot_send_command",
            "web_search",
            "write_note",
            "read_note",
            "list_notes",
        }
    )
    queue = CommandQueue(capacity=2)

    async def dispatch(_item: QueueItem):
        raise AssertionError("get_status must bypass the command queue")

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        response = await _call_tool(client, session_id=session_id, name="get_status")

    payload = response.json()
    status = json.loads(payload["result"]["content"][0]["text"])
    assert status["connected"] is True
    assert queue.depth == 0

async def test_switchbot_tools_exposed_and_bypass_device_queue(monkeypatch) -> None:
    """SwitchBot tools are listed over Streamable HTTP and dispatch
    gateway-locally: no queue entry, no ESP32 — even when the device is
    disconnected the call reaches the SwitchBot layer (here: the
    unconfigured-credentials error, not the disconnected-device payload)."""
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    queue = CommandQueue(capacity=2)

    async def dispatch(_item: QueueItem):
        raise AssertionError("switchbot tools must bypass the command queue")

    app = build_app(
        queue,
        gateway=FakeGateway(connected=False),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        listing = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            headers=_headers(session_id),
        )
        response = await _call_tool(
            client, session_id=session_id, name="switchbot_list_devices"
        )

    tool_names = {tool["name"] for tool in listing.json()["result"]["tools"]}
    assert {
        "switchbot_list_devices",
        "switchbot_get_status",
        "switchbot_send_command",
    } <= tool_names
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert "SWITCHBOT_TOKEN" in payload["error"]
    assert queue.depth == 0

async def test_dispatcher_returns_stdio_disconnect_payload_as_tool_result() -> None:
    queue = CommandQueue(capacity=2)
    gateway = FakeGateway(connected=False)
    app = build_app(
        queue,
        gateway=gateway,
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=make_dispatch_fn(gateway),
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        response = await _call_tool(
            client,
            session_id=session_id,
            name="get_device_info",
        )

    payload = response.json()
    assert "error" not in payload
    result_text = payload["result"]["content"][0]["text"]
    assert json.loads(result_text) == DISCONNECTED_DEVICE_PAYLOAD

async def test_healthz_is_liveness_only_and_status_requires_auth_for_details() -> None:
    queue = CommandQueue(capacity=2)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token="secret",
    )

    async with _client(app) as client:
        health = await client.get("/healthz")
        unauthenticated_status = await client.get("/status")
        status = await client.get("/status", headers=_headers(token="secret"))

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert set(health.json()) == {"ok"}
    assert unauthenticated_status.status_code == 401

    status_payload = status.json()
    assert status.status_code == 200
    assert status_payload["connected"] is True
    assert status_payload["esp32_connected"] is True
    assert status_payload["queue_depth"] == 0
    assert status_payload["queue_capacity"] == 2
    assert status_payload["owner_id"] == "owner-test"
    assert status_payload["connected_clients"] == 0

async def test_command_queue_raises_queue_full_directly() -> None:
    queue = CommandQueue(capacity=1)
    loop = asyncio.get_running_loop()
    queue.enqueue(
        QueueItem(
            correlation_id="first",
            client_session_id=None,
            client_request_id=1,
            tool_name="get_device_info",
            arguments={},
            response_future=loop.create_future(),
            enqueued_at=0.0,
        )
    )
    with pytest.raises(QueueFull):
        queue.enqueue(
            QueueItem(
                correlation_id="second",
                client_session_id=None,
                client_request_id=2,
                tool_name="get_head_angles",
                arguments={},
                response_future=loop.create_future(),
                enqueued_at=0.0,
            )
        )

# ---- Phase F dashboard /control/* routes -----------------------------

class FakeHeartbeat:
    def __init__(self, *, gestures: bool = True, speak: bool = False, interval: float = 30.0):
        self._gestures = gestures
        self._speak = object() if speak else None
        self._interval_min = interval

    @property
    def gestures_enabled(self) -> bool:
        return self._gestures

    def set_gestures(self, enabled: bool) -> None:
        self._gestures = bool(enabled)

class ControlFakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []
        self.listen_calls: list[tuple[str, str]] = []
        self.recording = False
        # get_touch_state payload exposed to /control/status.
        self.touch_payload = {"prox_mode": "listen", "prox_threshold": 600}

    def get_status(self) -> dict:
        return {"connected": self.device_connected}

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "self.touch.get_touch_state":
            payload = self.touch_payload
        else:
            payload = {"ok": True, "name": name, "arguments": arguments}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_calls.append((state, mode))

class FakePresenceMonitor:
    """Stand-in for PresenceMonitor in HTTP handler tests.

    The monitor's own logic is covered in test_presence.py; here we only
    drive the route handler (None vs present, snapshot pass-through, the
    update_config call and its result -> status-code mapping).
    """

    def __init__(self, snapshot=None, *, config_result=None, report=None) -> None:
        self._snapshot = (
            snapshot
            if snapshot is not None
            else {
                "enabled": True,
                "state": "active",
                "allows_heartbeat": True,
                "last_seen_s_ago": 3.0,
                "poll_sec": 10.0,
                "config": {"absent_after_s": 120, "sleep_window": "22:00-06:30"},
                "tmos": {"present": True, "presence": 770},
            }
        )
        self._config_result = config_result
        self._report = (
            report
            if report is not None
            else {
                "empty": False,
                "basic": {"samples": 42},
                "recommendation": {
                    "recommended_absent_after_s": 540,
                    "auto_apply": False,
                },
            }
        )
        self.update_calls: list[tuple] = []
        self.report_calls: list[int] = []

    def snapshot(self) -> dict:
        return self._snapshot

    def build_report(self, *, days: int = 7) -> dict:
        self.report_calls.append(days)
        return self._report

    def update_config(self, *, absent_after_s=None, sleep_window=None) -> dict:
        self.update_calls.append((absent_after_s, sleep_window))
        if self._config_result is not None:
            return self._config_result
        return {
            "ok": True,
            "config": {
                "absent_after_s": absent_after_s if absent_after_s is not None else 120,
                "sleep_window": sleep_window if sleep_window is not None else "22:00-06:30",
            },
        }

class ControlFakeGateway:
    def __init__(
        self,
        *,
        connected: bool = True,
        heartbeat: object | None = None,
        presence: object | None = None,
        proactive: object | None = None,
    ) -> None:
        self.esp32 = ControlFakeESP32(connected=connected)
        self._heartbeat = heartbeat
        self._presence = presence
        self._proactive = proactive
        self.voice_turn_active = False

@pytest.fixture(autouse=True)
def _control_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "STACKCHAN_CONTROL_STATE", str(tmp_path / "control_state.json")
    )
    monkeypatch.setenv("STACKCHAN_PRESETS_DIR", str(tmp_path / "presets"))

def _build_control_app(gateway, *, token: str | None = None):
    return build_app(
        CommandQueue(capacity=4),
        gateway=gateway,
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token=token,
    )

async def _request_case(case: dict, *, method: str = "post") -> tuple[httpx.Response, ControlFakeGateway]:
    """Drive one control-route case: build gateway/app, run any setup
    requests, then the main request. Shared by the family tables below."""
    gateway = ControlFakeGateway(**case.get("gateway_kwargs", {}))
    if case.get("voice_turn_active"):
        gateway.voice_turn_active = True
    app = _build_control_app(gateway)
    async with _client(app) as client:
        for _setup_method, setup_path, setup_json in case.get("setup", []):
            await client.post(setup_path, json=setup_json)
        if method == "get":
            resp = await client.get(case["path"])
        else:
            resp = await client.post(case["path"], json=case.get("json"))
    return resp, gateway

def _check_case(resp: httpx.Response, gateway: ControlFakeGateway, case: dict) -> None:
    """Apply the assertions a case row declares — mirroring the original
    per-endpoint tests assertion-for-assertion."""
    assert resp.status_code == case["status"]
    body = resp.json()
    if case.get("ok_false"):
        assert body["ok"] is False
    if case.get("ok_true"):
        assert body["ok"] is True
    if "body_full" in case:
        assert body == case["body_full"]
    for dotted, expected in case.get("body_paths", {}).items():
        node = body
        for part in dotted.split("."):
            node = node[part]
        assert node == expected
    if case.get("calls_empty"):
        assert gateway.esp32.calls == []
    for call in case.get("calls_contain", []):
        assert call in gateway.esp32.calls
# GET /control/status: connected/disconnected payloads, proactive
# availability, and the presence block — one table, one assertion shape.
_STATUS_CASES = [
    {
        "id": "test_control_status_connected_reports_full_payload",
        "path": "/control/status",
        "status": 200,
        "gateway_kwargs": {"heartbeat": FakeHeartbeat(gestures=True, speak=True)},
        "body_paths": {
            "ok": True,
            "esp32_connected": True,
            "volume": 50,  # default
            "muted": False,
            "mic_gain": 30,  # default
            "brightness": 75,  # default (matches firmware NVS default)
            "led.brightness": 100,  # default
            "led.idle": {"on": False, "r": 30, "g": 144, "b": 255},  # default
            "led.listening": {"r": 0, "g": 210, "b": 90},
            "led.hermes": {"r": 148, "g": 108, "b": 255},
            "heartbeat": {"gestures": True, "speak": True, "interval_min": 30.0},
            "proximity": {"mode": "listen", "threshold": 600},
        },
    },
    {
        "id": "test_control_status_disconnected_nulls_device_fields",
        "path": "/control/status",
        "status": 200,
        "gateway_kwargs": {"connected": False, "heartbeat": None},
        "body_paths": {
            "esp32_connected": False,
            "volume": None,
            "brightness": None,  # live value unknown when no device
            "led.idle.on": False,  # saved preference still surfaced
            "heartbeat": None,
            "proximity": None,
        },
    },
    {"id": "test_control_status_proactive_unavailable_when_no_speaker", "path": "/control/status", "status": 200, "del_env": ["STACKCHAN_PROACTIVE"], "body_paths": {"proactive": {"available": False, "enabled": False}}},
    {"id": "test_control_status_includes_presence_block", "path": "/control/status", "status": 200, "gateway_kwargs": {"presence": FakePresenceMonitor()}, "body_paths": {"presence.state": "active"}},
    {"id": "test_control_status_presence_disabled_without_monitor", "path": "/control/status", "status": 200, "body_paths": {"presence": {"enabled": False}}},
]

@pytest.mark.parametrize("case", _STATUS_CASES, ids=[c["id"] for c in _STATUS_CASES])
async def test_control_status_cases(case, monkeypatch) -> None:
    for env in case.get("del_env", []):
        monkeypatch.delenv(env, raising=False)
    resp, gateway = await _request_case(case, method="get")
    _check_case(resp, gateway, case)

# Control "scalar setter" families: volume / brightness / led_brightness /
# mic_gain / head / neutral_pose all share the same three-way shape —
# sets-and-persists, rejects out-of-range, 503 when disconnected. One table
# drives all six; every assertion below mirrors the original per-endpoint
# tests exactly (response payloads, device tool calls, error statuses).
_SCALAR_FAMILIES = [
    {
        "name": "volume",
        "path": "/control/volume",
        "set_body": {"volume": 70},
        "set_resp": {"ok": True, "volume": 70, "muted": False},
        "set_call": ("self.audio_speaker.set_volume", {"volume": 70}),
        "bad_bodies": [{"volume": 200}],
        "has_503": True,
    },
    {
        "name": "brightness",
        "path": "/control/brightness",
        "set_body": {"brightness": 40},
        "set_resp": {"ok": True, "brightness": 40},
        "set_call": ("self.screen.set_brightness", {"brightness": 40}),
        "bad_bodies": [{"brightness": 200}],
        "has_503": True,
    },
    {
        "name": "led_brightness",
        "path": "/control/led_brightness",
        "set_body": {"brightness": 60},
        "set_resp": {"ok": True, "brightness": 60},
        "set_call": None,
        "bad_bodies": [{"brightness": 200}],
        "has_503": False,
    },
    {
        "name": "mic_gain",
        "path": "/control/mic_gain",
        "set_body": {"gain": 24},
        "set_resp": {"ok": True, "gain": 24, "connected": True},
        "set_call": ("self.audio_speaker.set_mic_gain", {"gain": 24}),
        "bad_bodies": [{"gain": 37}, {"gain": -1}],
        "has_503": True,
    },
    {
        "name": "head",
        "path": "/control/head",
        "set_body": {"yaw": 25, "pitch": 55},
        "set_resp": {"ok": True, "yaw": 25, "pitch": 55, "connected": True},
        "set_call": ("self.robot.set_head_angles", {"yaw": 25, "pitch": 55}),
        "bad_bodies": [{"yaw": 200, "pitch": 30}, {"yaw": 0, "pitch": 1}],
        "has_503": True,
    },
    {
        "name": "neutral_pose",
        "path": "/control/neutral_pose",
        "set_body": {"yaw": -10, "pitch": 40},
        "set_resp": {"ok": True, "yaw": -10, "pitch": 40, "connected": True},
        "set_call": ("self.robot.set_neutral_pose", {"yaw": -10, "pitch": 40}),
        "bad_bodies": [{"yaw": 0, "pitch": 999}],
        "has_503": True,
    },
]

@pytest.mark.parametrize("family", _SCALAR_FAMILIES, ids=[f["name"] for f in _SCALAR_FAMILIES])
async def test_control_scalar_sets_and_persists(family) -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(family["path"], json=family["set_body"])
    assert resp.status_code == 200
    assert resp.json() == family["set_resp"]
    if family["set_call"] is not None:
        assert family["set_call"] in gateway.esp32.calls

@pytest.mark.parametrize(
    "family",
    [f for f in _SCALAR_FAMILIES if f["bad_bodies"]],
    ids=[f["name"] for f in _SCALAR_FAMILIES if f["bad_bodies"]],
)
async def test_control_scalar_rejects_out_of_range(family) -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        for bad in family["bad_bodies"]:
            resp = await client.post(family["path"], json=bad)
            assert resp.status_code == 400
            assert resp.json()["ok"] is False

@pytest.mark.parametrize(
    "family",
    [f for f in _SCALAR_FAMILIES if f["has_503"]],
    ids=[f["name"] for f in _SCALAR_FAMILIES if f["has_503"]],
)
async def test_control_scalar_503_when_disconnected(family) -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(family["path"], json=family["set_body"])
    assert resp.status_code == 503

# Control LED: /control/led slot writers, validation rejects, and the
# /control/led_test preview are one table — every assertion mirrors the
# original per-endpoint tests. The disconnected 503 row lives in
# _CONTROL_503_CASES below.
_LED_CASES = [
    {
        "id": "test_control_led_idle_on_sets_all",
        "path": "/control/led",
        "json": {"slot": "idle", "on": True, "r": 10, "g": 20, "b": 30},
        "status": 200,
        "body_paths": {"led.idle": {"on": True, "r": 10, "g": 20, "b": 30}},
        "calls_contain": [("self.led.set_all", {"r": 10, "g": 20, "b": 30})],
    },
    {
        "id": "test_control_led_idle_off_clears",
        "path": "/control/led",
        "json": {"slot": "idle", "on": False},
        "status": 200,
        "calls_contain": [("self.led.clear", {})],
    },
    {
        "id": "test_control_led_listening_persists_without_device_call",
        "path": "/control/led",
        "json": {"slot": "listening", "r": 5, "g": 6, "b": 7},
        "status": 200,
        "body_paths": {"led.listening": {"r": 5, "g": 6, "b": 7}},
        "calls_empty": True,  # listening is persisted only
    },
    {
        "id": "test_control_led_rejects_unknown_slot",
        "path": "/control/led",
        "json": {"slot": "nope", "r": 1},
        "status": 400,
    },
    {
        "id": "test_control_led_rejects_bad_rgb",
        "path": "/control/led",
        "json": {"slot": "idle", "on": True, "r": 300, "g": 0, "b": 0},
        "status": 400,
        "ok_false": True,
    },
    {
        "id": "test_control_led_idle_requires_boolean_on",
        "path": "/control/led",
        "json": {"slot": "idle", "on": "yes"},
        "status": 400,
    },
    {
        "id": "test_control_led_test_previews_slot",
        "path": "/control/led_test",
        "json": {"slot": "hermes"},
        "status": 200,
        "body_full": {"ok": True, "slot": "hermes"},
        # hermes colour shown, then revert to idle (default off -> clear).
        "calls_contain": [
            ("self.led.set_all", {"r": 148, "g": 108, "b": 255}),
            ("self.led.clear", {}),
        ],
        "zero_preview_seconds": True,
    },
    {
        "id": "test_control_led_test_rejects_unknown_slot",
        "path": "/control/led_test",
        "json": {"slot": "nope"},
        "status": 400,
    },
]

@pytest.mark.parametrize("case", _LED_CASES, ids=[c["id"] for c in _LED_CASES])
async def test_control_led_cases(case, monkeypatch) -> None:
    if case.get("zero_preview_seconds"):
        from stackchan_mcp import control

        monkeypatch.setattr(control, "LED_PREVIEW_SECONDS", 0)
    resp, gateway = await _request_case(case)
    _check_case(resp, gateway, case)
async def test_control_mute_then_unmute() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/volume", json={"volume": 60})
        muted = await client.post("/control/mute", json={"muted": True})
        unmuted = await client.post("/control/mute", json={"muted": False})
    assert muted.json() == {"ok": True, "volume": 0, "muted": True}
    assert unmuted.json() == {"ok": True, "volume": 60, "muted": False}

# /control/listen: start vs already-listening differ only in the audio
# stream state; one table keeps the identical assertions.
_LISTEN_CASES = [
    {
        "id": "test_control_listen_triggers_start",
        "recording": False,
        "status": 200,
        "body_full": {"ok": True},
        "listen_calls": [("start", "manual")],
    },
    {
        "id": "test_control_listen_already_listening_returns_409",
        "recording": True,
        "status": 409,
        "body_full": {"ok": False, "error": "already listening"},
    },
]

@pytest.mark.parametrize("case", _LISTEN_CASES, ids=[c["id"] for c in _LISTEN_CASES])
async def test_control_listen_cases(case, monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: case["recording"])
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/listen")
    assert resp.status_code == case["status"]
    assert resp.json() == case["body_full"]
    if "listen_calls" in case:
        assert gateway.esp32.listen_calls == case["listen_calls"]
# /control/proximity: dispatch plus the two validation rejects.
_PROXIMITY_CASES = [
    {
        "id": "test_control_proximity_dispatches",
        "path": "/control/proximity",
        "json": {"mode": "reflex", "threshold": 700},
        "status": 200,
        "body_full": {"ok": True, "mode": "reflex", "threshold": 700},
        "calls_contain": [
            ("self.touch.set_proximity_config", {"mode": "reflex", "threshold": 700})
        ],
    },
    {
        "id": "test_control_proximity_validates_threshold",
        "path": "/control/proximity",
        "json": {"mode": "listen", "threshold": 9999},
        "status": 400,
    },
    {
        "id": "test_control_proximity_validates_mode",
        "path": "/control/proximity",
        "json": {"mode": "bogus", "threshold": 600},
        "status": 400,
    },
]

@pytest.mark.parametrize("case", _PROXIMITY_CASES, ids=[c["id"] for c in _PROXIMITY_CASES])
async def test_control_proximity_cases(case) -> None:
    resp, gateway = await _request_case(case)
    _check_case(resp, gateway, case)
async def test_control_heartbeat_toggles_gestures() -> None:
    heartbeat = FakeHeartbeat(gestures=True)
    gateway = ControlFakeGateway(heartbeat=heartbeat)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/heartbeat", json={"gestures": False})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "gestures": False}
    assert heartbeat.gestures_enabled is False

# Routing / multiturn / proactive "set enabled" share one shape: POST a
# bool flag, read it back from GET /control/status. Each row keeps the
# original response and status-block assertions verbatim.
_POLICY_SET_CASES = [
    {
        "id": "test_control_routing_sets_force_hermes",
        "path": "/control/routing",
        "json": {"force_hermes": True},
        "status": 200,
        "del_env": ("STACKCHAN_LOCAL_LLM_MODEL", "STACKCHAN_MULTITURN"),
        "body_full": {"ok": True, "force_hermes": True},
        "status_paths": {
            "routing": {"force_hermes": True, "local_enabled": False, "multiturn": False}
        },
    },
    {
        "id": "test_control_multiturn_sets_enabled",
        "path": "/control/multiturn",
        "json": {"enabled": True},
        "status": 200,
        "del_env": ("STACKCHAN_LOCAL_LLM_MODEL", "STACKCHAN_MULTITURN"),
        "body_full": {"ok": True, "multiturn": True},
        "status_paths": {"routing.multiturn": True},
    },
    {
        "id": "test_control_proactive_sets_enabled",
        "path": "/control/proactive",
        "json": {"proactive_enabled": True},
        "status": 200,
        "del_env": ("STACKCHAN_PROACTIVE",),
        "gateway_kwargs": {"proactive": object()},  # speaker built
        "body_full": {"ok": True, "proactive_enabled": True},
        "status_paths": {"proactive": {"enabled": True, "available": True}},
    },
]

@pytest.mark.parametrize("case", _POLICY_SET_CASES, ids=[c["id"] for c in _POLICY_SET_CASES])
async def test_control_policy_set_cases(case, monkeypatch) -> None:
    for env in case["del_env"]:
        monkeypatch.delenv(env, raising=False)
    gateway = ControlFakeGateway(**case.get("gateway_kwargs", {}))
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(case["path"], json=case["json"])
        _check_case(resp, gateway, case)
        # The persisted flag is surfaced back in GET /control/status.
        body = (await client.get("/control/status")).json()
    for dotted, expected in case["status_paths"].items():
        node = body
        for part in dotted.split("."):
            node = node[part]
        assert node == expected

# Multiturn / proactive / routing / mute all reject a non-boolean body.
_NON_BOOL_CASES = [
    {"id": "test_control_multiturn_rejects_non_bool", "path": "/control/multiturn", "json": {"enabled": "yes"}},
    {"id": "test_control_proactive_rejects_non_bool", "path": "/control/proactive", "json": {"proactive_enabled": "yes"}},
    {"id": "test_control_routing_rejects_non_bool", "path": "/control/routing", "json": {"force_hermes": "yes"}},
    {"id": "test_control_mute_requires_boolean", "path": "/control/mute", "json": {"muted": "yes"}},
]

@pytest.mark.parametrize("case", _NON_BOOL_CASES, ids=[c["id"] for c in _NON_BOOL_CASES])
async def test_control_rejects_non_bool(case) -> None:
    resp, _ = await _request_case(case)
    assert resp.status_code == 400
# /control/avatar: happy face dispatches to the device, unknown faces 400.
_AVATAR_CASES = [
    {
        "id": "test_control_avatar_dispatches",
        "path": "/control/avatar",
        "json": {"face": "happy"},
        "status": 200,
        "body_full": {"ok": True, "face": "happy"},
        "calls_contain": [("self.display.set_avatar", {"face": "happy"})],
    },
    {
        "id": "test_control_avatar_rejects_unknown_face",
        "path": "/control/avatar",
        "json": {"face": "angry"},
        "status": 400,
    },
]

@pytest.mark.parametrize("case", _AVATAR_CASES, ids=[c["id"] for c in _AVATAR_CASES])
async def test_control_avatar_cases(case) -> None:
    resp, gateway = await _request_case(case)
    _check_case(resp, gateway, case)
# /control/say: TTS dispatch (orchestrator patched) and the two 400
# validation bodies — same endpoint, same assertion shape.
_SAY_CASES = [
    {
        "id": "test_control_say_speaks",
        "path": "/control/say",
        "json": {"text": "hello"},
        "status": 200,
        "body_full": {"ok": True, "tts": {"frame_count": 3}},
        "seen_text": "hello",
        "patch_orchestrator": True,
    },
    {
        "id": "test_control_say_rejects_empty_and_too_long",
        "path": "/control/say",
        "json_list": [{"text": "   "}, {"text": "a" * 201}],
        "status": 400,
    },
]

@pytest.mark.parametrize("case", _SAY_CASES, ids=[c["id"] for c in _SAY_CASES])
async def test_control_say_cases(case, monkeypatch) -> None:
    if case.get("patch_orchestrator"):
        import stackchan_mcp.tts.orchestrator as orchestrator

        seen = {}

        async def fake_send(arguments, *, gateway=None, **kw):
            seen["text"] = arguments["text"]
            return {"frame_count": 3}

        monkeypatch.setattr(orchestrator, "synthesize_and_send", fake_send)
    bodies = case["json_list"] if "json_list" in case else [case["json"]]
    for body in bodies:
        resp, gateway = await _request_case({**case, "json": body})
        _check_case(resp, gateway, case)
    if case.get("patch_orchestrator"):
        assert seen["text"] == case["seen_text"]
# /control/audio_level: idle vs recording differ only in stream state.
_AUDIO_LEVEL_CASES = [
    {
        "id": "test_control_audio_level_idle",
        "recording": False,
        "level": 0.0,
        "body_full": {"ok": True, "recording": False, "level": 0.0},
    },
    {
        "id": "test_control_audio_level_recording",
        "recording": True,
        "level": 0.55,
        "body_paths": {"ok": True, "recording": True, "level": 0.55},
    },
]

@pytest.mark.parametrize("case", _AUDIO_LEVEL_CASES, ids=[c["id"] for c in _AUDIO_LEVEL_CASES])
async def test_control_audio_level_cases(case, monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: case["recording"])
    monkeypatch.setattr(audio_stream, "get_input_level", lambda: case["level"])
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/audio_level")
    assert resp.status_code == 200
    body = resp.json()
    if "body_full" in case:
        assert body == case["body_full"]
    for key, expected in case.get("body_paths", {}).items():
        assert body[key] == expected

# GET control routes with a configured bearer token: no token 401, valid 200.
# (status also probes a wrong-token POST and the 401 body text.)
_TOKEN_GET_CASES = [
    {"id": "test_control_audio_level_requires_token", "path": "/control/audio_level"},
    {"id": "test_control_conversation_requires_token", "path": "/control/conversation"},
    {
        "id": "test_control_routes_require_token",
        "path": "/control/status",
        "gateway_kwargs": {"heartbeat": FakeHeartbeat()},
        "post_json": {"volume": 10},
        "auth_text": True,
    },
]

@pytest.mark.parametrize("case", _TOKEN_GET_CASES, ids=[c["id"] for c in _TOKEN_GET_CASES])
async def test_control_get_requires_token(case) -> None:
    gateway = ControlFakeGateway(**case.get("gateway_kwargs", {}))
    app = _build_control_app(gateway, token="secret")
    async with _client(app) as client:
        missing = await client.get(case["path"])
        wrong = await client.post(
            case["path"], json=case.get("post_json"), headers=_headers(token="wrong")
        )
        ok = await client.get(case["path"], headers=_headers(token="secret"))
    assert missing.status_code == 401
    if case.get("auth_text"):
        assert missing.text == AUTH_FAILURE_MESSAGE
    assert wrong.status_code == 401
    assert ok.status_code == 200
async def test_control_mic_gain_reflected_in_status() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/mic_gain", json={"gain": 12})
        status = await client.get("/control/status")
    assert status.json()["mic_gain"] == 12

async def test_control_conversation_empty() -> None:
    from stackchan_mcp import control

    control._CONVERSATION.clear()
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/conversation")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "turns": []}

async def test_control_conversation_returns_recorded_turns() -> None:
    from stackchan_mcp import control

    control._CONVERSATION.clear()
    control.record_conversation_turn("good morning", "good morning!", "local", {"total": 480})
    control.record_conversation_turn("weather?", "sunny", "hermes", {"total": 1500})
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/conversation")
    body = resp.json()
    assert body["ok"] is True
    assert [t["transcript"] for t in body["turns"]] == ["good morning", "weather?"]
    assert body["turns"][0]["route"] == "local"
    assert body["turns"][1]["timings_ms"] == {"total": 1500}
    control._CONVERSATION.clear()

# ---- mode presets -----------------------------------------------------

async def test_control_presets_save_and_list() -> None:
    gateway = ControlFakeGateway(heartbeat=FakeHeartbeat(gestures=True))
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/volume", json={"volume": 70})
        saved = await client.post("/control/presets/save", json={"name": "night"})
        listed = await client.get("/control/presets/list")
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    body = listed.json()
    assert body["ok"] is True
    assert [p["name"] for p in body["presets"]] == ["night"]

# Preset error variants: bad name, unknown apply target, and apply busy
# during a voice turn — the 503-disconnected save row lives in
# _CONTROL_503_CASES below.
_PRESET_ERROR_CASES = [
    {
        "id": "test_control_presets_save_rejects_bad_name",
        "path": "/control/presets/save",
        "json": {"name": "../x"},
        "status": 400,
    },
    {
        "id": "test_control_presets_apply_not_found",
        "path": "/control/presets/apply",
        "json": {"name": "ghost"},
        "status": 404,
    },
    {
        "id": "test_control_presets_apply_busy_during_voice_turn",
        "path": "/control/presets/apply",
        "json": {"name": "scene"},
        "status": 409,
        "voice_turn_active": True,
        "setup": [("post", "/control/presets/save", {"name": "scene"})],
    },
]

@pytest.mark.parametrize("case", _PRESET_ERROR_CASES, ids=[c["id"] for c in _PRESET_ERROR_CASES])
async def test_control_preset_error_cases(case) -> None:
    resp, gateway = await _request_case(case)
    _check_case(resp, gateway, case)
async def test_control_presets_save_conflict_without_overwrite() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        first = await client.post("/control/presets/save", json={"name": "m"})
        dup = await client.post("/control/presets/save", json={"name": "m"})
        forced = await client.post(
            "/control/presets/save", json={"name": "m", "overwrite": True}
        )
    assert first.status_code == 200
    assert dup.status_code == 409
    assert forced.status_code == 200

async def test_control_presets_apply_resends_and_reports() -> None:
    from stackchan_mcp import control

    gateway = ControlFakeGateway(heartbeat=FakeHeartbeat(gestures=False))
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/volume", json={"volume": 80})
        await client.post("/control/presets/save", json={"name": "scene"})
        await client.post("/control/volume", json={"volume": 20})
        gateway.esp32.calls.clear()
        applied = await client.post("/control/presets/apply", json={"name": "scene"})
    assert applied.status_code == 200
    assert applied.json()["ok"] is True
    tools = [name for name, _ in gateway.esp32.calls]
    assert "self.audio_speaker.set_volume" in tools
    assert "self.touch.set_proximity_config" in tools
    assert control.load_state()["volume"] == 80

async def test_control_presets_delete() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/presets/save", json={"name": "m"})
        deleted = await client.post("/control/presets/delete", json={"name": "m"})
        missing = await client.post("/control/presets/delete", json={"name": "m"})
        listed = await client.get("/control/presets/list")
    assert deleted.status_code == 200
    assert missing.status_code == 404
    assert listed.json()["presets"] == []

# ---- POST /control/i2c (Port A sensor bring-up, "Port A") -----------------

# POST /control/i2c (Port A sensor bring-up): the four ops dispatch
# verbatim to the device; the unknown-op reject rides along on the same
# scaffold (addr / n_bytes / bytes keep their value tables below).
_I2C_DISPATCH_CASES = [
    {
        "id": "test_control_i2c_scan_dispatches",
        "path": "/control/i2c",
        "json": {"op": "scan"},
        "status": 200,
        "ok_true": True,
        "calls_contain": [("self.i2c.scan", {})],
    },
    {
        # The STHS34PF80 WHO_AM_I idiom: set register pointer 0x0F, read 1 byte.
        "id": "test_control_i2c_write_read_dispatches_who_am_i",
        "path": "/control/i2c",
        "json": {"op": "write_read", "addr": 0x5A, "write_bytes": [0x0F], "n_bytes": 1},
        "status": 200,
        "calls_contain": [
            ("self.i2c.write_read", {"addr": 0x5A, "write_bytes": [0x0F], "n_bytes": 1})
        ],
    },
    {
        "id": "test_control_i2c_read_dispatches",
        "path": "/control/i2c",
        "json": {"op": "read", "addr": 0x5A, "n_bytes": 2},
        "status": 200,
        "calls_contain": [("self.i2c.read", {"addr": 0x5A, "n_bytes": 2})],
    },
    {
        "id": "test_control_i2c_write_dispatches",
        "path": "/control/i2c",
        "json": {"op": "write", "addr": 0x5A, "bytes": [0x20, 0x13]},
        "status": 200,
        "calls_contain": [("self.i2c.write", {"addr": 0x5A, "bytes": [0x20, 0x13]})],
    },
    {"id": "test_control_i2c_rejects_unknown_op", "path": "/control/i2c", "json": {"op": "nope"}, "status": 400, "calls_empty": True},
]

@pytest.mark.parametrize("case", _I2C_DISPATCH_CASES, ids=[c["id"] for c in _I2C_DISPATCH_CASES])
async def test_control_i2c_dispatch_cases(case) -> None:
    resp, gateway = await _request_case(case)
    _check_case(resp, gateway, case)
async def test_control_i2c_surfaces_device_bytes() -> None:
    gateway = ControlFakeGateway()

    async def _read_d3(name, arguments):
        payload = {"ok": True, "bytes": [0xD3]}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None

    gateway.esp32.call_tool = _read_d3  # type: ignore[assignment]
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/i2c",
            json={"op": "write_read", "addr": 0x5A, "write_bytes": [0x0F], "n_bytes": 1},
        )
    assert resp.status_code == 200
    assert resp.json()["bytes"] == [0xD3]  # WHO_AM_I reads back verbatim

async def test_control_i2c_device_error_maps_502() -> None:
    gateway = ControlFakeGateway()

    async def _nack(name, arguments):
        payload = {"ok": False, "error": "ESP_ERR_TIMEOUT"}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None

    gateway.esp32.call_tool = _nack  # type: ignore[assignment]
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/i2c", json={"op": "read", "addr": 0x5A, "n_bytes": 1}
        )
    assert resp.status_code == 502
    assert resp.json()["error"] == "ESP_ERR_TIMEOUT"

@pytest.mark.parametrize("addr", [0x07, 0x78, "0x5A", True, None])
async def test_control_i2c_rejects_out_of_range_addr(addr) -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/i2c", json={"op": "read", "addr": addr, "n_bytes": 1}
        )
    assert resp.status_code == 400
    assert gateway.esp32.calls == []

@pytest.mark.parametrize("n", [0, 257, True, "2", None])
async def test_control_i2c_rejects_bad_n_bytes(n) -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/i2c", json={"op": "read", "addr": 0x5A, "n_bytes": n}
        )
    assert resp.status_code == 400

@pytest.mark.parametrize("data", [[], [256], [-1], [True], "x", [1, "2"]])
async def test_control_i2c_rejects_bad_bytes(data) -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/i2c", json={"op": "write", "addr": 0x5A, "bytes": data}
        )
    assert resp.status_code == 400

def _sensor_call_tool(reg_map, *, recorded=None):
    """Programmable esp32.call_tool: serve register bytes for write_read,
    ack writes, optionally recording (esp32_name, args)."""

    async def _fake(name, arguments):
        if recorded is not None:
            recorded.append((name, arguments))
        if name == "self.i2c.write_read":
            addr = arguments["addr"]
            reg = arguments["write_bytes"][0]
            data = reg_map.get((addr, reg), [0] * arguments["n_bytes"])
            payload = {"ok": True, "bytes": data}
        else:
            payload = {"ok": True}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None

    return _fake

async def test_control_sensors_reads_both() -> None:
    gateway = ControlFakeGateway()
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x04],  # PRES flag
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x2C, 0x01],  # 300
        (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TAMBIENT_L): [0xB8, 0x0B],  # 30.00 C
        (0x73, sensors.GESTURE_RESULT_0): [0x01, 0x00],  # up
    }
    gateway.esp32.call_tool = _sensor_call_tool(reg_map)  # type: ignore[assignment]
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/sensors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tmos"]["present"] is True
    assert body["tmos"]["presence"] == 300
    assert body["tmos"]["ambient_c"] == 30.0
    assert body["gesture"]["gesture"] == "up"

async def test_control_sensors_partial_error_stays_200() -> None:
    # TMOS reads fine; the gesture unit NACKs -> nested error, top-level ok.
    gateway = ControlFakeGateway()
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x00],
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TAMBIENT_L): [0x00, 0x00],
    }

    async def _fake(name, arguments):
        if name == "self.i2c.write_read" and arguments["addr"] == 0x73:
            return {"content": [{"type": "text", "text": json.dumps({"error": "NACK"})}]}, None
        if name == "self.i2c.write" and arguments["addr"] == 0x73:
            return {"content": [{"type": "text", "text": json.dumps({"error": "NACK"})}]}, None
        if name == "self.i2c.write_read":
            reg = arguments["write_bytes"][0]
            data = reg_map.get((arguments["addr"], reg), [0] * arguments["n_bytes"])
            return {"content": [{"type": "text", "text": json.dumps({"ok": True, "bytes": data})}]}, None
        return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}, None

    gateway.esp32.call_tool = _fake  # type: ignore[assignment]
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/sensors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tmos"]["present"] is False
    assert "error" in body["gesture"]

async def test_control_sensors_init_writes_gesture_array() -> None:
    gateway = ControlFakeGateway()
    recorded: list[tuple[str, dict]] = []
    reg_map = {
        (0x5A, sensors.TMOS_WHO_AM_I): [0xD3],
        (0x73, sensors.GESTURE_PART_ID_L): [0x20, 0x76],  # 0x7620
    }
    gateway.esp32.call_tool = _sensor_call_tool(reg_map, recorded=recorded)  # type: ignore[assignment]
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/sensors/init")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tmos"]["ok"] is True
    assert body["gesture"]["ok"] is True
    gesture_writes = [
        tuple(a["bytes"])
        for n, a in recorded
        if n == "self.i2c.write" and a["addr"] == 0x73
    ]
    # A representative slice of the init array made it through verbatim.
    assert (0xEF, 0x00) in gesture_writes
    assert (0x41, 0xFF) in gesture_writes

# Every control route that 503s when its backing service is unavailable:
# device disconnected (LED / i2c / sensors / sensors-init / preset save),
# no heartbeat runner, or no presence monitor. One table, one assertion.
_CONTROL_503_CASES = [
    {"id": "test_control_led_503_when_disconnected", "path": "/control/led", "json": {"slot": "idle", "on": True}, "gateway_kwargs": {"connected": False}},
    {"id": "test_control_heartbeat_503_when_no_runner", "path": "/control/heartbeat", "json": {"gestures": True}},
    {"id": "test_control_presets_save_requires_device", "path": "/control/presets/save", "json": {"name": "x"}, "gateway_kwargs": {"connected": False}},
    {"id": "test_control_i2c_503_when_disconnected", "path": "/control/i2c", "json": {"op": "scan"}, "gateway_kwargs": {"connected": False}},
    {"id": "test_control_sensors_503_when_disconnected", "method": "get", "path": "/control/sensors", "gateway_kwargs": {"connected": False}},
    {"id": "test_control_sensors_init_503_when_disconnected", "path": "/control/sensors/init", "gateway_kwargs": {"connected": False}},
    {"id": "test_control_presence_config_503_when_no_monitor", "path": "/control/presence/config", "json": {"absent_after_s": 60}},
]

@pytest.mark.parametrize("case", _CONTROL_503_CASES, ids=[c["id"] for c in _CONTROL_503_CASES])
async def test_control_route_503_when_unavailable(case) -> None:
    resp, _ = await _request_case(case, method=case.get("method", "post"))
    assert resp.status_code == 503
# ---- /control/presence (presence state machine) ----------------------

# GET /control/presence (and /report): disabled vs monitor-backed — same
# scaffold and assertion shapes; one table.
_PRESENCE_GET_CASES = [
    {"id": "test_control_presence_disabled_when_no_monitor", "path": "/control/presence", "status": 200, "body_full": {"ok": True, "enabled": False}},
    {
        "id": "test_control_presence_reports_snapshot",
        "path": "/control/presence",
        "status": 200,
        "gateway_kwargs": {"presence": FakePresenceMonitor()},
        "body_paths": {"ok": True, "enabled": True, "state": "active", "config.absent_after_s": 120},
    },
    {
        "id": "test_control_presence_report_returns_payload",
        "path": "/control/presence/report",
        "status": 200,
        "gateway_kwargs": {"presence": FakePresenceMonitor()},
        "body_paths": {"ok": True, "basic.samples": 42, "recommendation.auto_apply": False},
        "report_calls": [7],  # default days
    },
    {"id": "test_control_presence_report_disabled_when_no_monitor", "path": "/control/presence/report", "status": 200, "body_full": {"ok": True, "enabled": False}},
]

@pytest.mark.parametrize("case", _PRESENCE_GET_CASES, ids=[c["id"] for c in _PRESENCE_GET_CASES])
async def test_control_presence_get_cases(case) -> None:
    resp, gateway = await _request_case(case, method="get")
    _check_case(resp, gateway, case)
    if "report_calls" in case:
        presence = gateway._presence
        assert isinstance(presence, FakePresenceMonitor) and presence.report_calls == case["report_calls"]

async def test_control_presence_report_days_query() -> None:
    monitor = FakePresenceMonitor()
    gateway = ControlFakeGateway(presence=monitor)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.get("/control/presence/report?days=14")
        await client.get("/control/presence/report?days=999")  # clamp to 28
        await client.get("/control/presence/report?days=abc")  # fallback to 7
    assert monitor.report_calls == [14, 28, 7]

async def test_control_presence_config_updates() -> None:
    monitor = FakePresenceMonitor()
    gateway = ControlFakeGateway(presence=monitor)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/presence/config",
            json={"absent_after_s": 60, "sleep_window": "23:00-07:00"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert monitor.update_calls == [(60, "23:00-07:00")]

# /control/presence/config validation rejects: empty body, wrong absent_after_s
# type, and a malformed window the monitor rejects (mapped to 400).
_PRESENCE_CONFIG_400_CASES = [
    {"id": "test_control_presence_config_400_when_empty", "json": {}, "status": 400},
    {"id": "test_control_presence_config_400_bad_absent_type", "json": {"absent_after_s": "soon"}, "status": 400},
    {
        # The monitor rejects a malformed window; the handler maps it to 400.
        "id": "test_control_presence_config_400_on_bad_window",
        "json": {"sleep_window": "nonsense"},
        "status": 400,
        "monitor_result": {"ok": False, "error": "sleep_window must be 'HH:MM-HH:MM' or 'off'"},
        "ok_false": True,
    },
]

@pytest.mark.parametrize("case", _PRESENCE_CONFIG_400_CASES, ids=[c["id"] for c in _PRESENCE_CONFIG_400_CASES])
async def test_control_presence_config_invalid(case) -> None:
    monitor = FakePresenceMonitor(config_result=case.get("monitor_result"))
    gateway = ControlFakeGateway(presence=monitor)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/presence/config", json=case["json"])
    assert resp.status_code == case["status"]
    if case.get("ok_false"):
        assert resp.json()["ok"] is False