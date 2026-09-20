"""Shared JSON/field helpers for the gateway's HTTP control plane.

Extracted from ``http_server.py`` so the Streamable-HTTP MCP wiring and
the dashboard ``/control/*`` route registry (:mod:`stackchan_mcp.control_http`)
can both use them without a circular import. Pure functions — no gateway
state, no HTTP framework beyond Starlette's ``JSONResponse``.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse


async def read_json_body(request: Request) -> dict[str, Any]:
    """Best-effort JSON body as a dict ({} for empty / non-object)."""
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def require_device(gateway: Any) -> JSONResponse | None:
    """Return a 503 response when no device is connected, else None."""
    if not gateway.esp32.device_connected:
        return control_error("no device connected", status=503)
    return None


def int_field(body: dict[str, Any], key: str, lo: int, hi: int) -> tuple[int | None, str | None]:
    """Validate an integer body field in [lo, hi]; (value, None) or (None, error)."""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, f"{key} must be an integer {lo}..{hi}"
    return value, None


def bool_field(body: dict[str, Any], key: str) -> tuple[bool | None, str | None]:
    """Validate a boolean body field; (value, None) or (None, error)."""
    value = body.get(key)
    if not isinstance(value, bool):
        return None, f"{key} must be a boolean"
    return value, None


def control_json(payload: dict[str, Any], *, status: int = 200) -> JSONResponse:
    code = status
    if not payload.get("ok", True) and status == 200:
        # A device-call failure without an explicit status maps to 502.
        code = 502
    return JSONResponse(payload, status_code=code)


def control_error(message: str, *, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def device_tool_payload(content: list[Any]) -> Any:
    """Extract the JSON payload (or raw text) from an ESP32 tool result.

    The device tools come back as a list of ``TextContent``; the first
    text item is usually a JSON document (e.g. get_touch_state) but can
    also be a plain string. Returns a dict when parseable, the raw
    string otherwise, or None when there is no text content.
    """
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return None


def content_has_error(content: list[Any]) -> str | None:
    """Return the error string when a device tool result carried one."""
    payload = device_tool_payload(content)
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])
    return None


def control_device_result(content: list[Any], **extra: Any) -> JSONResponse:
    """Map a device tool dispatch result to a control JSON response."""
    error = content_has_error(content)
    if error is not None:
        return control_error(error, status=502)
    return control_json({"ok": True, **extra})


def control_i2c_result(content: list[Any]) -> JSONResponse:
    """Map an ``i2c_*`` device dispatch to a control JSON response.

    Surfaces the device payload verbatim (e.g. ``{"bytes": [...]}`` for a
    read, or the scan's address list) so dashboard / probe scripts get the
    raw values back instead of a flattened ``ok``.
    """
    error = content_has_error(content)
    if error is not None:
        return control_error(error, status=502)
    payload = device_tool_payload(content)
    if payload is None:
        return control_error("empty device response", status=502)
    if isinstance(payload, dict):
        return control_json({"ok": True, **payload})
    return control_json({"ok": True, "result": payload})


def i2c_byte_list(value: Any) -> list[int] | None:
    """Validate a JSON array as I2C bytes (each 0..255); None if invalid."""
    if not isinstance(value, list) or not value:
        return None
    out: list[int] = []
    for b in value:
        if not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= 255:
            return None
        out.append(b)
    return out


def i2c_n_bytes(value: Any) -> int | None:
    """Validate an I2C read length (1..256); None if invalid."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 256 else None


def parse_clamped(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Clamp a numeric query value to [lo, hi]; malformed -> default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def parse_activity_feed_limit(value: Any) -> int:
    """Clamp an activity-feed ``?limit=`` query to 1..200 (default 80)."""
    return parse_clamped(value, default=80, lo=1, hi=200)


def parse_report_days(value: Any) -> int:
    """Clamp a presence-report ``?days=`` query to 1..28 (default 7)."""
    return parse_clamped(value, default=7, lo=1, hi=28)
