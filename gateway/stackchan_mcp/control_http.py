"""Dashboard ``/control/*`` route registry for the StackChan gateway.

Extracted from ``http_server.py`` so the Streamable-HTTP MCP wiring stays
a lean ASGI concern and the dashboard REST contract lives here as one
cohesive, table-driven module. Every endpoint is declared once in
:func:`build_control_routes`; the repetitive shapes (a scalar setting, a
yaw/pitch pair, a boolean toggle, a simple GET) are produced by small
factories instead of a wall of near-identical handlers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import activity_log, control, local_llm, sensors
from .http_common import (
    bool_field,
    control_device_result,
    control_error,
    control_i2c_result,
    control_json,
    device_tool_payload,
    i2c_byte_list,
    i2c_n_bytes,
    int_field,
    parse_activity_feed_limit,
    parse_report_days,
    read_json_body,
    require_device,
)
from .stdio_server import _dispatch_mcp_tool

#: Avatar faces accepted by POST /control/avatar (mirrors the firmware
#: AvatarSet faces plus "off").
CONTROL_AVATAR_FACES = frozenset(
    {"idle", "happy", "thinking", "sad", "surprised", "embarrassed", "off"}
)
#: Upper bound on POST /control/say text (one spoken breath on a 1 W speaker).
CONTROL_SAY_MAX_CHARS = 200

#: Proximity modes accepted by POST /control/proximity.
PROXIMITY_MODES = frozenset({"reflex", "listen", "off"})
#: I2C operations accepted by POST /control/i2c.
I2C_OPS = ("scan", "read", "write", "write_read")

#: JSONL-backed activity sources (written by activity_log.append). ``report``
#: items are merged in from the daily presence reports, not the JSONL.
ACTIVITY_JSONL_SOURCES = frozenset({"heartbeat", "proactive", "presence", "home"})
PRESENCE_REPORT_ENV = "STACKCHAN_PRESENCE_REPORT"

#: Deep-night Obsidian-vault housekeeping crons surfaced in the feed, as
#: ``(log path, label)``. Each job's log mtime is its last-run time; we emit
#: one item per job. The hourly Claude-Code token-usage cron is deliberately
#: absent: per-hour entries flood the feed and duplicate the server tab's CC
#: usage (Kenji's call, 2026-06-27).
CRON_JOBS: Final[tuple[tuple[str, str], ...]] = (
    ("/tmp/inbox-drain.log", "Inbox Drain"),
    ("/tmp/build_moc.log", "MOC Build"),
    ("/tmp/build_notes_review.log", "Notes Review Update"),
    ("/tmp/weekly-digest.log", "Weekly Digest"),
    ("/tmp/notes-tidy.log", "Notes Tidy"),
    ("/tmp/notes-tidy-suggest.log", "Tidy Suggestions"),
    ("/tmp/articles-recommend.log", "Article Recommend"),
    ("/tmp/self-reflect.log", "Self-Reflect"),
)
#: Lower-cased substrings in a cron log's last line that mark a failed run.
_CRON_ERROR_MARKERS: Final = (
    "error",
    "traceback",
    "permission denied",
    "exception",
    "failed",
    "not found",
    "no such file",
)


# ---- activity feed (yorishiro) ----------------------------------------


def _presence_report_dir() -> Path:
    raw = __import__("os").environ.get(PRESENCE_REPORT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".stackchan" / "presence_reports"


def _list_presence_reports(limit: int, *, path: Path | None = None) -> list[dict[str, Any]]:
    """List the most recent daily presence reports as feed link items."""
    if path is None:
        path = _presence_report_dir()
    if limit <= 0 or not path.exists() or not path.is_dir():
        return []
    try:
        files = sorted(path.glob("*.json"))
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for f in files[-limit:]:
        date = f.stem  # YYYY-MM-DD
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        items.append(
            {
                "ts_unix": mtime,
                "source": "report",
                "kind": "daily_report",
                "status": "ok",
                "subtype": date,
                "text": f"Daily occupancy report for {date}",
                "detail": {"date": date},
            }
        )
    return items


def _last_log_line(path: Path, *, max_bytes: int = 4096) -> str:
    """Best-effort last non-empty line of a log, read from the tail (≤200 chars)."""
    import os

    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read()
    except OSError:
        return ""
    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _read_cron_runs(
    jobs: tuple[tuple[str, str], ...] | None = None,
) -> list[dict[str, Any]]:
    """Surface deep-night Obsidian housekeeping crons in the feed.

    Each job's log gives its last-run time (file mtime) plus a one-line tail
    for context; we emit one ``source="cron"`` item per job. Read-only and
    best-effort: a missing log simply yields nothing for that job.

    ``jobs`` defaults to :data:`CRON_JOBS` (resolved at call time so tests
    can monkeypatch it).
    """
    items: list[dict[str, Any]] = []
    for path_str, label in CRON_JOBS if jobs is None else jobs:
        path = Path(path_str)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        text = _last_log_line(path)
        status = (
            "error"
            if text and any(m in text.lower() for m in _CRON_ERROR_MARKERS)
            else "ok"
        )
        items.append(
            {
                "ts_unix": mtime,
                "source": "cron",
                "kind": "run",
                "subtype": label,
                "status": status,
                "text": text,
            }
        )
    return items


def _gather_activity(limit: int, source: str | None) -> list[dict[str, Any]]:
    """Merge autonomous-activity JSONL + cron runs + daily reports, newest first."""
    items: list[dict[str, Any]] = []
    if source is None or source in ACTIVITY_JSONL_SOURCES:
        items += activity_log.read_recent(
            limit, source=source if source in ACTIVITY_JSONL_SOURCES else None
        )
    if source is None or source == "cron":
        items += _read_cron_runs()
    if source is None or source == "report":
        items += _list_presence_reports(14)
    items.sort(key=lambda r: r.get("ts_unix", 0.0), reverse=True)
    return items[:limit]


# ---- gateways into shared control state -------------------------------


async def _heartbeat_status(gateway: Any) -> dict[str, Any] | None:
    runner = getattr(gateway, "_heartbeat", None)
    if runner is None:
        return None
    return {
        "gestures": bool(runner.gestures_enabled),
        "speak": runner._speak is not None,
        "interval_min": runner._interval_min,
    }


async def _proximity_status(gateway: Any) -> dict[str, Any] | None:
    content = await _dispatch_mcp_tool("get_touch_state", {}, gateway)
    payload = device_tool_payload(content)
    if not isinstance(payload, dict):
        return None
    mode = payload.get("prox_mode")
    threshold = payload.get("prox_threshold")
    if mode not in PROXIMITY_MODES or not isinstance(threshold, int):
        return None
    return {"mode": mode, "threshold": threshold}


async def _build_control_status(gateway: Any) -> dict[str, Any]:
    """Assemble the GET /control/status payload (REST contract)."""
    connected = bool(gateway.esp32.device_connected)
    state = control.load_state()
    # Brightness/volume are live device values (unknown when no device);
    # the LED block is a saved preference, always surfaced — the toggle
    # shows what will be applied on (re)connect.
    heartbeat = await _heartbeat_status(gateway)
    proximity = await _proximity_status(gateway) if connected else None
    monitor = getattr(gateway, "_presence", None)
    presence = monitor.snapshot() if monitor is not None else {"enabled": False}
    return {
        "ok": True,
        "esp32_connected": connected,
        "volume": state["volume"] if connected else None,
        "muted": state["muted"],
        "mic_gain": state["mic_gain"],
        "brightness": state["brightness"] if connected else None,
        "led": state["led"],
        "heartbeat": heartbeat,
        "proximity": proximity,
        "presence": presence,
        "routing": {
            "force_hermes": state["force_hermes"],
            "local_enabled": local_llm.is_enabled(),
            "multiturn": state["multiturn"],
        },
        "proactive": {
            # ``enabled`` is the persisted dashboard toggle (runtime truth);
            # ``available`` is whether the speaker was built at all (the
            # STACKCHAN_PROACTIVE env master switch).
            "enabled": state["proactive_enabled"],
            "available": getattr(gateway, "_proactive", None) is not None,
        },
    }


async def _build_preset_snapshot(gateway: Any) -> dict[str, Any]:
    """Snapshot the dashboard-controllable settings for a preset.

    Reuses the same sources as :func:`_build_control_status`. The head's
    neutral pose is intentionally excluded (it depends on where the device
    is placed). ``control.save_preset`` sanitises this.
    """
    state = control.load_state()
    snapshot: dict[str, Any] = {
        "volume": state["volume"],
        "muted": state["muted"],
        "pre_mute_volume": state["pre_mute_volume"],
        "mic_gain": state["mic_gain"],
        "brightness": state["brightness"],
        "led": state["led"],
    }
    proximity = await _proximity_status(gateway)
    if proximity is not None:
        snapshot["proximity"] = proximity
    heartbeat = await _heartbeat_status(gateway)
    if heartbeat is not None and "gestures" in heartbeat:
        snapshot["heartbeat"] = {"gestures": heartbeat["gestures"]}
    return snapshot


def build_control_routes(gateway: Any, dispatch_fn: Any = None) -> list[Route]:
    """Build the ``/control/*`` route set for the dashboard REST contract.

    ``gateway`` is captured by every generated handler. ``dispatch_fn`` is
    unused by this module (device calls go through ``_dispatch_mcp_tool``)
    but accepted so ``http_server.build_app`` can pass the same dispatcher
    through uniformly.
    """

    # Scalar int setting that requires a live device.
    def _scalar(
        field: str,
        lo: int,
        hi: int,
        setter: Callable[..., Awaitable[dict[str, Any]]],
        *,
        add_connected: bool = False,
    ):
        async def handler(request: Request) -> JSONResponse:
            if (err := require_device(gateway)) is not None:
                return err
            body = await read_json_body(request)
            value, err = int_field(body, field, lo, hi)
            if err is not None:
                return control_error(err, status=400)
            assert value is not None
            result = await setter(gateway, value)
            if add_connected:
                result = {**result, "connected": bool(gateway.esp32.device_connected)}
            return control_json(result)

        return handler

    # Yaw/pitch pair that requires a live device.
    def _head(
        setter: Callable[..., Awaitable[dict[str, Any]]],
        *,
        yaw: tuple[int, int] = (-90, 90),
        pitch: tuple[int, int] = (5, 85),
    ):
        async def handler(request: Request) -> JSONResponse:
            if (err := require_device(gateway)) is not None:
                return err
            body = await read_json_body(request)
            yaw_v, err = int_field(body, "yaw", yaw[0], yaw[1])
            if err is not None:
                return control_error(err, status=400)
            pitch_v, err = int_field(body, "pitch", pitch[0], pitch[1])
            if err is not None:
                return control_error(err, status=400)
            assert yaw_v is not None and pitch_v is not None
            return control_json(await setter(gateway, yaw_v, pitch_v))

        return handler

    # Boolean toggle, gateway-local (no device round-trip).
    def _bool_toggle(
        field: str,
        setter: Callable[..., dict[str, Any]],
    ):
        async def handler(request: Request) -> JSONResponse:
            body = await read_json_body(request)
            value, err = bool_field(body, field)
            if err is not None:
                return control_error(err, status=400)
            assert value is not None
            return control_json(setter(value))

        return handler

    # Simple local GET handlers.
    async def _control_status(_request: Request) -> JSONResponse:
        return JSONResponse(await _build_control_status(gateway))

    async def _control_audio_level(_request: Request) -> JSONResponse:
        return JSONResponse(control.get_audio_level())

    async def _control_conversation(_request: Request) -> JSONResponse:
        return JSONResponse(control.get_conversation())

    async def _control_presence(_request: Request) -> JSONResponse:
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return JSONResponse({"ok": True, "enabled": False})
        return JSONResponse({"ok": True, **monitor.snapshot()})

    async def _control_presence_report(request: Request) -> JSONResponse:
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return JSONResponse({"ok": True, "enabled": False})
        days = parse_report_days(request.query_params.get("days"))
        report = await asyncio.to_thread(monitor.build_report, days=days)
        return JSONResponse({"ok": True, **report})

    async def _control_presence_config(request: Request) -> JSONResponse:
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return control_error("presence monitor not running", status=503)
        body = await read_json_body(request)
        absent_after_s = body.get("absent_after_s")
        sleep_window = body.get("sleep_window")
        if absent_after_s is None and sleep_window is None:
            return control_error(
                "provide absent_after_s and/or sleep_window", status=400
            )
        if absent_after_s is not None and (
            not isinstance(absent_after_s, int) or isinstance(absent_after_s, bool)
        ):
            return control_error("absent_after_s must be an integer", status=400)
        if sleep_window is not None and not isinstance(sleep_window, str):
            return control_error("sleep_window must be a string", status=400)
        result = monitor.update_config(
            absent_after_s=absent_after_s, sleep_window=sleep_window
        )
        return control_json(result, status=200 if result.get("ok") else 400)

    async def _control_activity(request: Request) -> JSONResponse:
        limit = parse_activity_feed_limit(request.query_params.get("limit"))
        source = request.query_params.get("source") or None
        items = await asyncio.to_thread(_gather_activity, limit, source)
        return JSONResponse({"ok": True, "items": items})

    async def _control_led(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        slot = body.get("slot")
        if slot not in control.LED_SLOTS:
            return control_error(f"slot must be one of {list(control.LED_SLOTS)}", status=400)
        # Missing color channels default to 0 (dashboard sends partial RGB).
        defaulted = {**body, "r": body.get("r", 0), "g": body.get("g", 0), "b": body.get("b", 0)}
        rgb = {}
        for key in ("r", "g", "b"):
            val, err = int_field(defaulted, key, 0, 255)
            if err is not None:
                return control_error(err, status=400)
            assert val is not None
            rgb[key] = val
        on = body.get("on")
        if slot == "idle" and not isinstance(on, bool):
            return control_error("on must be a boolean for the idle slot", status=400)
        return control_json(await control.set_led(gateway, slot, on=on, **rgb))

    async def _control_led_test(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        slot = body.get("slot")
        if slot not in control.LED_SLOTS:
            return control_error(f"slot must be one of {list(control.LED_SLOTS)}", status=400)
        return control_json(await control.preview_led(gateway, slot))

    async def _control_mute(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        muted, err = bool_field(body, "muted")
        if err is not None:
            return control_error(err, status=400)
        assert muted is not None
        result = await (control.mute(gateway) if muted else control.unmute(gateway))
        return control_json(result)

    async def _control_listen(_request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        result = await control.trigger_listen(gateway)
        status = 409 if result.get("error") == "already listening" else 200
        return control_json(result, status=status)

    async def _control_proximity(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        mode = body.get("mode")
        if mode not in PROXIMITY_MODES:
            return control_error("mode must be one of: reflex, listen, off", status=400)
        threshold, err = int_field(body, "threshold", 0, 2047)
        if err is not None:
            return control_error(err, status=400)
        assert threshold is not None
        content = await _dispatch_mcp_tool(
            "set_proximity_config", {"mode": mode, "threshold": threshold}, gateway
        )
        return control_device_result(content, mode=mode, threshold=threshold)

    async def _control_heartbeat(request: Request) -> JSONResponse:
        runner = getattr(gateway, "_heartbeat", None)
        if runner is None:
            return control_error("heartbeat not running", status=503)
        body = await read_json_body(request)
        gestures, err = bool_field(body, "gestures")
        if err is not None:
            return control_error(err, status=400)
        assert gestures is not None
        runner.set_gestures(gestures)
        return control_json({"ok": True, "gestures": runner.gestures_enabled})

    async def _control_avatar(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        face = body.get("face")
        if face not in CONTROL_AVATAR_FACES:
            return control_error(f"face must be one of {sorted(CONTROL_AVATAR_FACES)}", status=400)
        content = await _dispatch_mcp_tool("set_avatar", {"face": face}, gateway)
        return control_device_result(content, face=face)

    async def _control_say(request: Request) -> JSONResponse:
        if (err := require_device(gateway)) is not None:
            return err
        body = await read_json_body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return control_error("text must be a non-empty string", status=400)
        if len(text) > CONTROL_SAY_MAX_CHARS:
            return control_error(f"text exceeds {CONTROL_SAY_MAX_CHARS} characters", status=400)
        from .tts.orchestrator import synthesize_and_send

        try:
            result = await synthesize_and_send({"text": text}, gateway=gateway)
        except (ValueError, NotImplementedError, RuntimeError, ConnectionError) as exc:
            return control_error(f"say failed: {exc}", status=502)
        return control_json({"ok": True, "tts": result})

    async def _control_i2c(request: Request) -> JSONResponse:
        # Debug relay onto the Grove Port A I2C bus (yorishiro sensor
        # bring-up, "Port A"):
        # Body: {"op": "scan"|"read"|"write"|"write_read", ...args}.
        if not gateway.esp32.device_connected:
            return control_error("no device connected", status=503)
        body = await read_json_body(request)
        op = body.get("op")
        if op == "scan":
            content = await _dispatch_mcp_tool("i2c_scan", {}, gateway)
            return control_i2c_result(content)
        if op not in I2C_OPS:
            return control_error("op must be one of: scan, read, write, write_read", status=400)
        addr = body.get("addr")
        if not isinstance(addr, int) or isinstance(addr, bool) or not 0x08 <= addr <= 0x77:
            return control_error("addr must be an integer 0x08..0x77", status=400)
        if op == "read":
            n = i2c_n_bytes(body.get("n_bytes"))
            if n is None:
                return control_error("n_bytes must be an integer 1..256", status=400)
            content = await _dispatch_mcp_tool("i2c_read", {"addr": addr, "n_bytes": n}, gateway)
        elif op == "write":
            data = i2c_byte_list(body.get("bytes"))
            if data is None:
                return control_error("bytes must be a non-empty list of integers 0..255", status=400)
            content = await _dispatch_mcp_tool("i2c_write", {"addr": addr, "bytes": data}, gateway)
        else:  # write_read
            data = i2c_byte_list(body.get("write_bytes"))
            n = i2c_n_bytes(body.get("n_bytes"))
            if data is None:
                return control_error("write_bytes must be a non-empty list of integers 0..255", status=400)
            if n is None:
                return control_error("n_bytes must be an integer 1..256", status=400)
            content = await _dispatch_mcp_tool(
                "i2c_write_read", {"addr": addr, "write_bytes": data, "n_bytes": n}, gateway
            )
        return control_i2c_result(content)

    async def _control_sensors(_request: Request) -> JSONResponse:
        # Live Port A sensor snapshot (TMOS PIR / PAJ7620). Per-sensor errors
        # are nested in the payload, so top-level ok stays True.
        if not gateway.esp32.device_connected:
            return control_error("no device connected", status=503)
        data = await sensors.read_all(lambda n, a: _dispatch_mcp_tool(n, a, gateway))
        return control_json({"ok": True, **data})

    async def _control_sensors_init(_request: Request) -> JSONResponse:
        if not gateway.esp32.device_connected:
            return control_error("no device connected", status=503)
        data = await sensors.init_all(lambda n, a: _dispatch_mcp_tool(n, a, gateway))
        return control_json({"ok": True, **data})

    async def _control_presets_save(request: Request) -> JSONResponse:
        if not gateway.esp32.device_connected:
            # Need the device to snapshot proximity, so require connection.
            return control_error("no device connected", status=503)
        body = await read_json_body(request)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return control_error("name must be 1..32 chars without / \\\\ or ..", status=400)
        snapshot = await _build_preset_snapshot(gateway)
        result = await control.save_preset(name, snapshot, overwrite=bool(body.get("overwrite", False)))
        if result.get("ok"):
            return control_json(result)
        status = 409 if "already exists" in result.get("error", "") else 502
        return control_json(result, status=status)

    async def _control_presets_apply(request: Request) -> JSONResponse:
        if not gateway.esp32.device_connected:
            return control_error("no device connected", status=503)
        body = await read_json_body(request)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return control_error("invalid preset name", status=400)
        result = await control.apply_preset(gateway, name)
        if result.get("ok"):
            return control_json(result)
        err = result.get("error", "")
        status = 404 if "not found" in err else 409 if "busy" in err else 502
        return control_json(result, status=status)

    async def _control_presets_delete(request: Request) -> JSONResponse:
        body = await read_json_body(request)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return control_error("invalid preset name", status=400)
        result = await control.delete_preset(name)
        if result.get("ok"):
            return control_json(result)
        return control_json(result, status=404)

    async def _control_presets_list(_request: Request) -> JSONResponse:
        return control_json({"ok": True, "presets": await control.list_presets()})

    route = Route

    routes = [
        route("/control/status", _control_status, methods=["GET"]),
        route("/control/audio_level", _control_audio_level, methods=["GET"]),
        route("/control/conversation", _control_conversation, methods=["GET"]),
        route("/control/presence", _control_presence, methods=["GET"]),
        route("/control/presence/report", _control_presence_report, methods=["GET"]),
        route("/control/presence/config", _control_presence_config, methods=["POST"]),
        route("/control/activity", _control_activity, methods=["GET"]),
        # Scalar settings requiring a live device.
        route("/control/volume", _scalar("volume", 0, 100, control.set_volume), methods=["POST"]),
        route("/control/mic_gain", _scalar("gain", 0, 36, control.set_mic_gain, add_connected=True), methods=["POST"]),
        route("/control/brightness", _scalar("brightness", 0, 100, control.set_brightness), methods=["POST"]),
        route("/control/led_brightness", _scalar("brightness", 0, 100, control.set_led_brightness), methods=["POST"]),
        route("/control/head", _head(control.set_head_angle), methods=["POST"]),
        route("/control/neutral_pose", _head(control.set_neutral_pose), methods=["POST"]),
        # Boolean toggles (gateway-local persisted state).
        route("/control/routing", _bool_toggle("force_hermes", control.set_routing_force_hermes), methods=["POST"]),
        route("/control/multiturn", _bool_toggle("enabled", control.set_multiturn), methods=["POST"]),
        route("/control/proactive", _bool_toggle("proactive_enabled", control.set_proactive_enabled), methods=["POST"]),
        # Bespoke device-facing endpoints.
        route("/control/led", _control_led, methods=["POST"]),
        route("/control/led_test", _control_led_test, methods=["POST"]),
        route("/control/mute", _control_mute, methods=["POST"]),
        route("/control/listen", _control_listen, methods=["POST"]),
        route("/control/proximity", _control_proximity, methods=["POST"]),
        route("/control/heartbeat", _control_heartbeat, methods=["POST"]),
        route("/control/avatar", _control_avatar, methods=["POST"]),
        route("/control/say", _control_say, methods=["POST"]),
        route("/control/i2c", _control_i2c, methods=["POST"]),
        route("/control/sensors", _control_sensors, methods=["GET"]),
        route("/control/sensors/init", _control_sensors_init, methods=["POST"]),
        route("/control/presets/list", _control_presets_list, methods=["GET"]),
        route("/control/presets/save", _control_presets_save, methods=["POST"]),
        route("/control/presets/apply", _control_presets_apply, methods=["POST"]),
        route("/control/presets/delete", _control_presets_delete, methods=["POST"]),
    ]
    return routes
