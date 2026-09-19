"""Declarative MCP tool registry (yorishiro fork).

Every StackChan MCP tool lives here as one ``ToolDef`` row: its name,
description, input schema, and *how it runs* — either a gateway-local
``handler`` coroutine or a ``relay`` to an ESP32 firmware tool.  The MCP
stdio server, the streamable-HTTP server, and the control dashboard all
resolve tools through this single table, so there is exactly one place
where the tool surface is defined.

``list_tools()`` and ``call_tool()`` are the only entry points callers
need; they map 1:1 onto MCP's ``tools/list`` and ``tools/call``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from functools import partial
from typing import Any, Awaitable, Callable

from mcp.types import TextContent, Tool

from . import activity_log, control, notes, switchbot, web_search
from .stt import listen_and_transcribe
from .tts import synthesize_and_send

Handler = Callable[[Any, dict[str, Any]], Awaitable[Any]]
Prepare = Callable[[dict[str, Any]], dict[str, Any]]


def _as_schema(schema: dict[str, Any] | str) -> dict[str, Any]:
    """Return a schema dict, parsing the compact JSON-string form used in
    the tool table so each row stays one line long."""
    if isinstance(schema, str):
        return json.loads(schema)
    return schema


@dataclass(frozen=True)
class ToolDef:
    """One entry in the tool table.

    Exactly one of ``handler`` / ``relay`` selects how the tool runs:
    ``handler`` is a gateway-local coroutine; ``relay`` names the ESP32
    firmware tool the arguments are forwarded to (with an optional
    ``prepare`` transform applied first).
    """

    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler | None = None
    relay: str | None = None
    prepare: Prepare | None = None
    device_required: bool = True

    def to_mcp(self) -> Tool:
        schema = _as_schema(self.schema)
        return Tool(name=self.name, description=self.description, inputSchema=schema)


def _ok(result: Any) -> list[TextContent]:
    """Wrap a JSON-serialisable result as the standard text payload."""
    return [TextContent(type="text", text=json.dumps(result))]


def _err(message: str) -> list[TextContent]:
    """Standard error payload (all gateway tool errors share this shape)."""
    return [TextContent(type="text", text=json.dumps({"error": message}))]


# ---------------------------------------------------------------------------
# Gateway-local handlers
# ---------------------------------------------------------------------------


async def _get_status(gateway: Any, _: dict[str, Any]) -> Any:
    return gateway.esp32.get_status()


async def _get_presence(gateway: Any, _: dict[str, Any]) -> Any:
    monitor = getattr(gateway, "_presence", None)
    return monitor.snapshot() if monitor is not None else {"enabled": False}


async def _say(gateway: Any, arguments: dict[str, Any]) -> Any:
    return await synthesize_and_send(arguments, gateway=gateway)


async def _set_volume(gateway: Any, arguments: dict[str, Any]) -> Any:
    return await control.set_volume(gateway, arguments.get("volume"))


async def _listen(gateway: Any, arguments: dict[str, Any]) -> Any:
    return await listen_and_transcribe(arguments, gateway=gateway)


async def _load_avatar_set(gateway: Any, arguments: dict[str, Any]) -> Any:
    archive_path = arguments.get("archive_path", "")
    mode = arguments.get("mode", "")
    try:
        timeout = float(arguments.get("timeout", 60.0))
    except (TypeError, ValueError):
        timeout = 60.0
    if not archive_path or not isinstance(archive_path, str):
        return {"ok": False, "error": "archive_path is required"}
    if mode not in ("layered", "matrix"):
        return {"ok": False, "error": f"unknown mode: {mode}"}
    return await gateway.load_avatar_set(archive_path, mode, timeout)


async def _switchbot(gateway: Any, arguments: dict[str, Any], name: str = "") -> Any:
    try:
        if name == "switchbot_list_devices":
            return await switchbot.list_devices()
        if name == "switchbot_get_status":
            return await switchbot.get_device_status(arguments.get("device_id", ""))
        result = await switchbot.send_command(
            arguments.get("device_id", ""),
            arguments.get("command", ""),
            arguments.get("parameter", "default"),
            arguments.get("command_type", "command"),
        )
    except (ValueError, RuntimeError) as exc:
        if name == "switchbot_send_command":
            activity_log.append(
                "home", "command",
                subtype=arguments.get("device_id") or None,
                text=arguments.get("command") or None,
                status="error",
                detail={"error": str(exc)},
            )
        return {"error": str(exc)}
    if name == "switchbot_send_command":
        code = result.get("statusCode") if isinstance(result, dict) else None
        activity_log.append(
            "home", "command",
            subtype=arguments.get("device_id") or None,
            text=arguments.get("command") or None,
            status="ok" if code in (None, switchbot.SUCCESS_STATUS_CODE) else "error",
            detail={"statusCode": code} if code is not None else None,
        )
        return {"ok": True, "result": result}
    return result


async def _web_search(gateway: Any, arguments: dict[str, Any]) -> Any:
    # Surface "Searching..." on the device during a voice turn only.
    if getattr(gateway, "voice_turn_active", False):
        await control.set_device_status_text(gateway, control.STATUS_SEARCHING)
    return await web_search.search(arguments.get("query", ""), arguments.get("max_results"))


async def _write_note(gateway: Any, arguments: dict[str, Any]) -> Any:
    return await asyncio.to_thread(
        notes.write_note,
        arguments.get("name", ""),
        arguments.get("content", ""),
        bool(arguments.get("append", False)),
    )


async def _read_note(gateway: Any, arguments: dict[str, Any]) -> Any:
    return await asyncio.to_thread(notes.read_note, arguments.get("name", ""))


async def _list_notes(gateway: Any, _: dict[str, Any]) -> Any:
    return await asyncio.to_thread(notes.list_notes)


HANDLERS: dict[str, Handler] = {
    "get_status": _get_status,
    "get_presence": _get_presence,
    "say": _say,
    "set_volume": _set_volume,
    "listen": _listen,
    "load_avatar_set": _load_avatar_set,
    "switchbot_list_devices": partial(_switchbot, name="switchbot_list_devices"),
    "switchbot_get_status": partial(_switchbot, name="switchbot_get_status"),
    "switchbot_send_command": partial(_switchbot, name="switchbot_send_command"),
    "web_search": _web_search,
    "write_note": _write_note,
    "read_note": _read_note,
    "list_notes": _list_notes,
}


# ---------------------------------------------------------------------------
# Head-speed presets (shared between move_head schema and prepare)
# ---------------------------------------------------------------------------

PRESET_DPS = {"low": 30, "mid": 120, "high": 240}
SPEED_DPS_MAX = 10000
SPEED_DESCRIPTION = """speed (optional): How fast to move the head.
  - "low"  — slow, deliberate, ~30°/s. Good for curious tilts or gentle look-toward.
  - "mid"  — default natural turn, ~120°/s. Use for conversational eye contact.
  - "high" — quick reaction, ~240°/s. Use for surprise / double-take.
  - Or a raw degrees-per-second integer if you need a specific value."""


def _resolve_speed_dps(speed: Any) -> int | None:
    """Return an int speed_dps to forward, or None to omit the field."""
    if speed is None:
        return None
    if isinstance(speed, bool):
        raise TypeError("speed must be a preset string or an integer, not bool")
    if isinstance(speed, str):
        if speed not in PRESET_DPS:
            raise ValueError(
                f"speed preset must be one of {list(PRESET_DPS)}, got {speed!r}"
            )
        return PRESET_DPS[speed]
    if isinstance(speed, int):
        if speed < 1:
            raise ValueError(f"speed integer must be >= 1, got {speed}")
        if speed > SPEED_DPS_MAX:
            raise ValueError(f"speed integer must be <= {SPEED_DPS_MAX}, got {speed}")
        return speed
    raise TypeError(
        f"speed must be 'low' / 'mid' / 'high' / int / None, got {type(speed).__name__}"
    )


# ---------------------------------------------------------------------------
# Argument prepare transforms for relays that need tailored handling
# ---------------------------------------------------------------------------


def _prepare_move_head(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate head angles and resolve optional speed preset → speed_dps."""
    yaw_val = arguments.get("yaw")
    pitch_val = arguments.get("pitch")
    if (
        not isinstance(yaw_val, int)
        or isinstance(yaw_val, bool)
        or not (-90 <= yaw_val <= 90)
    ):
        raise ValueError(f"yaw must be an integer in -90..90 (got {yaw_val!r})")
    if (
        not isinstance(pitch_val, int)
        or isinstance(pitch_val, bool)
        or not (5 <= pitch_val <= 85)
    ):
        raise ValueError(
            "pitch must be an integer in 5..85 "
            "(M5Stack-recommended operating range; for the wider firmware "
            f"hard clamp 0..88 use `set_head_angles`). got {pitch_val!r}"
        )
    prepared = {"yaw": yaw_val, "pitch": pitch_val}
    speed_dps = _resolve_speed_dps(arguments.get("speed"))
    if speed_dps is not None:
        prepared["speed_dps"] = speed_dps
    return prepared


def _prepare_steps_json(arguments: dict[str, Any]) -> dict[str, Any]:
    """Serialise a mouth-sequence step list for the firmware tool."""
    return {"steps_json": json.dumps(arguments.get("steps", []))}


def _prepare_colors_json(arguments: dict[str, Any]) -> dict[str, Any]:
    """Serialise the LED colour array for the firmware tool."""
    return {"colors": json.dumps(arguments.get("colors", []))}


# ---------------------------------------------------------------------------
# Tool table (mirrors the firmware's exposed device surface)
# ---------------------------------------------------------------------------

TOOLS: list[ToolDef] = [
    ToolDef('get_status', "Get the gateway's connection status: whether ESP32 is connected, device info, and list of available device tools.", '{"type": "object", "properties": {}}', handler=HANDLERS['get_status']),
    ToolDef('get_presence', 'Get the room\'s current presence state from the TMOS sensor: state (active=someone present and awake hours / quiet=present during sleeping hours / absent=room empty / unknown=not yet read), seconds since last detection, and the configured thresholds. Gateway-local, no device round-trip. Use this to answer whether anyone is in the room right now. Returns {"enabled": false} when presence monitoring is off.', '{"type": "object", "properties": {}}', handler=HANDLERS['get_presence']),
    ToolDef('get_device_info', 'Get real-time device information from ESP32: battery level, speaker volume, screen brightness, network status, etc.', '{"type": "object", "properties": {}}', relay='self.get_device_status'),
    ToolDef('take_photo', "Take a photo with the robot's camera and ask a question about it. The device captures an image and returns an AI-generated description.", '{"type": "object", "properties": {"question": {"type": "string", "description": "Question to ask about the photo (e.g. \'What do you see?\')"}}, "required": ["question"]}', relay='self.camera.take_photo'),
    ToolDef('set_volume', 'Set the speaker volume (0-100). The value is clamped to 0..100 and persisted in the gateway state, so the chosen level is restored automatically whenever the ESP32 reconnects. Returns the applied volume.', '{"type": "object", "properties": {"volume": {"type": "integer", "description": "Volume level (0-100)"}}, "required": ["volume"]}', handler=HANDLERS['set_volume']),
    ToolDef('set_mic_gain', 'Set the microphone input gain (0-36).', '{"type": "object", "properties": {"gain": {"type": "integer", "description": "Mic gain level (0-36)", "minimum": 0, "maximum": 36}}, "required": ["gain"]}', relay='self.audio_speaker.set_mic_gain'),
    ToolDef('set_brightness', 'Set the screen brightness (0-100).', '{"type": "object", "properties": {"brightness": {"type": "integer", "description": "Brightness level (0-100)"}}, "required": ["brightness"]}', relay='self.screen.set_brightness'),
    ToolDef('move_head', "Move the robot's head to safe, recommended angles. yaw: horizontal (-90 to 90), pitch: vertical (5 to 85, the M5Stack-recommended operating range). Out-of-range requests are rejected at this MCP layer; for advanced callers that need the firmware hard clamp (pitch 0..88), use the firmware-side `set_head_angles` device tool, which exposes a permissive schema and the authoritative two-tier guard described in the README.", {"type": "object", "properties": {"yaw": {"type": "integer", "description": "Horizontal angle in degrees (-90 to 90)", "minimum": -90, "maximum": 90}, "pitch": {"type": "integer", "description": "Vertical angle in degrees (5 to 85, M5Stack-recommended operating range). For the wider firmware hard clamp (0..88), use the `set_head_angles` device tool instead.", "minimum": 5, "maximum": 85}, "speed": {"oneOf": [{"enum": ["low", "mid", "high"]}, {"type": "integer", "minimum": 1, "maximum": SPEED_DPS_MAX}], "description": SPEED_DESCRIPTION}}, "required": ["yaw", "pitch"]}, relay='self.robot.set_head_angles', prepare=_prepare_move_head),
    ToolDef('get_head_angles', "Get the robot's current head angles: yaw and pitch in degrees.", '{"type": "object", "properties": {}}', relay='self.robot.get_head_angles'),
    ToolDef('set_head_wave', "Start a continuous, fluid head-motion wave — Reachy-style 'alive' presence. The firmware renders it natively in its servo task (~50 Hz), so the head glides along a smooth sine instead of hopping between set-points. center_yaw / center_pitch = base pose in degrees; yaw_amp / pitch_amp = oscillation amplitude in degrees (0 holds that axis centered); yaw_freq_mhz / pitch_freq_mhz = frequency in milli-Hz (e.g. 500 = 0.5 Hz sway, 1300 = 1.3 Hz speech bob); yaw_phase_deg / pitch_phase_deg = phase offset in degrees (pitch phase defaults to 90 so pitch wave leads/sways relative to yaw). Stop with clear_head_wave; any explicit set_head_angles / touch / idle-settle also stops it.", '{"type": "object", "properties": {"center_yaw": {"type": "integer", "description": "Base yaw in degrees (-90..90)", "minimum": -90, "maximum": 90}, "center_pitch": {"type": "integer", "description": "Base pitch in degrees (0..88)", "minimum": 0, "maximum": 88}, "yaw_amp": {"type": "integer", "description": "Yaw amplitude in degrees (0..90)", "minimum": 0, "maximum": 90}, "yaw_freq_mhz": {"type": "integer", "description": "Yaw frequency in milli-Hz (0..5000)", "minimum": 0, "maximum": 5000}, "yaw_phase_deg": {"type": "integer", "description": "Yaw phase offset in degrees (0..360)", "minimum": 0, "maximum": 360}, "pitch_amp": {"type": "integer", "description": "Pitch amplitude in degrees (0..80)", "minimum": 0, "maximum": 80}, "pitch_freq_mhz": {"type": "integer", "description": "Pitch frequency in milli-Hz (0..5000)", "minimum": 0, "maximum": 5000}, "pitch_phase_deg": {"type": "integer", "description": "Pitch phase offset in degrees (0..360)", "minimum": 0, "maximum": 360}}, "required": ["center_yaw", "center_pitch", "yaw_amp", "yaw_freq_mhz"]}', relay='self.robot.set_head_wave'),
    ToolDef('clear_head_wave', "Stop a running continuous head wave and let the head settle back toward its current center. Safe to call even when no wave is active (no-op).", '{"type": "object", "properties": {}}', relay='self.robot.clear_head_wave'),
    ToolDef('set_neutral_pose', "Save the head's neutral (rest) pose — the angles it returns to between gestures. yaw: horizontal (-90 to 90), pitch: vertical (5 to 85, the M5Stack-recommended range). Unlike move_head this persists on the device across reboots (NVS), so the head rests at the chosen pose after a power cycle. The firmware applies its own wider hard clamp (pitch 0..88) on top.", '{"type": "object", "properties": {"yaw": {"type": "integer", "description": "Horizontal angle in degrees (-90 to 90)", "minimum": -90, "maximum": 90}, "pitch": {"type": "integer", "description": "Vertical angle in degrees (5 to 85, M5Stack-recommended operating range)", "minimum": 5, "maximum": 85}}, "required": ["yaw", "pitch"]}', relay='self.robot.set_neutral_pose'),
    ToolDef('gpio_test', 'Test GPIO6 pin by toggling HIGH/LOW 5 times. Check if servo reacts.', '{"type": "object", "properties": {}}', relay='self.robot.gpio_test'),
    ToolDef('uart_diag', 'Send raw servo bytes via UART and report write result.', '{"type": "object", "properties": {}}', relay='self.robot.uart_diag'),
    ToolDef('check_vm_en', 'Diagnostic: read PY32 REG_GPIO_O_L and report whether VM EN (pin 0 = servo power) is currently HIGH. Returns {io_expander_present, i2c_read_ok, raw, vm_en_high}.', '{"type": "object", "properties": {}}', relay='self.robot.check_vm_en'),
    ToolDef('set_avatar', "Switch the avatar face shown on the LCD. Choose one of the supported faces; this is the robot's actual visible expression, not just a label. Pass 'off' to hide the avatar and disable blink, exposing the underlying xiaozhi-esp32 screens (WiFi config UI, OTA, settings); any other face brings the avatar back and restores blink.", '{"type": "object", "properties": {"face": {"type": "string", "enum": ["idle", "happy", "thinking", "sad", "surprised", "embarrassed", "off"], "description": "One of: idle, happy, thinking, sad, surprised, embarrassed, off."}}, "required": ["face"]}', relay='self.display.set_avatar'),
    ToolDef('set_mouth', 'Set the avatar mouth shape for lip-sync. The shape is held until the next set_avatar / set_mouth call, or until an autonomous blink restores the resting face. Calling this while a set_mouth_sequence is in flight interrupts the sequence.', '{"type": "object", "properties": {"mouth": {"type": "string", "enum": ["closed", "half", "open", "e", "u"], "description": "One of: closed, half, open, e, u."}}, "required": ["mouth"]}', relay='self.display.set_mouth'),
    ToolDef('set_mouth_sequence', "Queue a lip-sync sequence and play it on the device. Each step holds 'shape' for 'duration_ms' before advancing. The firmware walks the queue locally so there is no per-step network RTT (use this instead of issuing many set_mouth calls back-to-back from a TTS loop). Returns immediately with the queued step count and estimated total duration. Calling set_mouth, set_avatar, or this tool again interrupts the in-flight sequence and replaces it. Autonomous blink is paused while a sequence is playing and resumed when it ends. The final shape is held until the next set_mouth / set_avatar call, or until an autonomous blink restores the resting face — this is the same Phase 2 trade-off that applies to set_mouth, since the blink animation ends by repainting the full face. If the final shape must persist visually, disable blink with set_blink(false) before the sequence (or append a closed step if you just want the mouth to close at the end).", '{"type": "object", "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "object", "properties": {"shape": {"type": "string", "enum": ["closed", "half", "open", "e", "u"], "description": "Mouth shape for this step. One of: closed, half, open, e, u."}, "duration_ms": {"type": "integer", "minimum": 10, "maximum": 10000, "description": "How long to hold this shape before advancing, in ms (10..10000)."}}, "required": ["shape", "duration_ms"]}, "description": "Ordered list of mouth shapes with hold durations (1..256 steps)."}}, "required": ["steps"]}', relay='self.display.set_mouth_sequence', prepare=_prepare_steps_json),
    ToolDef('set_blink', 'Enable or disable autonomous eye blinking. When enabled, the avatar blinks every 3-6 seconds at random.', '{"type": "object", "properties": {"enabled": {"type": "boolean", "description": "True to start blinking, false to stop."}}, "required": ["enabled"]}', relay='self.display.set_blink'),
    ToolDef('set_servo_torque', 'Enable or disable SCS0009 servo torque on the yaw / pitch axes independently. Disabling torque stops motor current on that axis; the head holds via static friction (no motion is commanded). On disable, the firmware also cancels any in-flight MotionDriver interpolation and marks the axis position unknown so a subsequent same-target set_head_angles is re-dispatched rather than no-op-optimized. Re-enabling torque does NOT trigger a move; the next set_head_angles or wobble call will. Diagnostic / power-management primitive used to observe physical head behavior under torque-off (Issue #163; auto release on idle is Issue #152 Phase 4).', '{"type": "object", "properties": {"yaw_enabled": {"type": "boolean", "description": "True to enable yaw axis torque, false to disable."}, "pitch_enabled": {"type": "boolean", "description": "True to enable pitch axis torque, false to disable."}}, "required": ["yaw_enabled", "pitch_enabled"]}', relay='self.robot.set_servo_torque'),
    ToolDef('set_auto_torque_release', 'Enable or disable firmware-side automatic SCS0009 torque release after motion idle timeout. timeout_ms is clamped by the firmware to 500..600000 ms. Disabling this setting does not re-enable torque if it is already released; the next set_head_angles, wobble, or explicit set_servo_torque(true, true) call re-engages torque.', '{"type": "object", "properties": {"enabled": {"type": "boolean", "description": "True to enable idle auto-release, false to disable it."}, "timeout_ms": {"type": "integer", "description": "Idle timeout in milliseconds. Values outside 500..600000 are clamped by the firmware handler."}}, "required": ["enabled", "timeout_ms"]}', relay='self.robot.set_auto_torque_release'),
    ToolDef('get_touch_state', 'Read the head-touch (Si12T) sensor state and the most recent gesture event (tap/stroke/idle). Returns per-zone booleans, the raw output byte, and how long ago the last event fired.', '{"type": "object", "properties": {}}', relay='self.touch.get_touch_state'),
    ToolDef('set_proximity_config', 'Set the proximity hand-wave reaction mode (LTR-553) and its raw PS detection threshold. mode is one of reflex / listen / off. Baseline reads ~380; a hand within 10cm reads ~820+. Both values persist on the device across reboots.', '{"type": "object", "properties": {"mode": {"type": "string", "enum": ["reflex", "listen", "off"], "description": "Reaction on detection: reflex (look up + happy face), listen (tap-equivalent listen), off (no reaction)"}, "threshold": {"type": "integer", "description": "Raw PS counts (0..2047)", "minimum": 0, "maximum": 2047}}, "required": ["mode", "threshold"]}', relay='self.touch.set_proximity_config'),
    ToolDef('set_status_text', "Show a short one-line status string on the device screen under the avatar (e.g. 'I'm listening...', 'Thinking...', 'Searching...'). Pass an empty string to clear it. Used by the gateway's voice pipeline for UI feedback; safe to call directly too.", '{"type": "object", "properties": {"text": {"type": "string", "description": "Status line to show; empty string clears it"}}, "required": ["text"]}', relay='self.display.set_status_text'),
    ToolDef('set_led', 'Set a single RGB LED on the StackChan base. There are 12 LEDs arranged in two rows of 6 (index 0..11). Updates immediately.', '{"type": "object", "properties": {"index": {"type": "integer", "description": "LED index (0..11)", "minimum": 0, "maximum": 11}, "r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255}, "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255}, "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255}}, "required": ["index", "r", "g", "b"]}', relay='self.led.set_color'),
    ToolDef('set_all_leds', 'Set all 12 RGB LEDs on the StackChan base to the same color. Updates immediately.', '{"type": "object", "properties": {"r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255}, "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255}, "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255}}, "required": ["r", "g", "b"]}', relay='self.led.set_all'),
    ToolDef('set_leds', "Set multiple RGB LEDs in one shot. 'colors' is an array of [r,g,b] triples starting at index 0 (e.g. [[255,0,0],[0,255,0]]). Up to 12 entries; extras are ignored, missing entries keep their previous color. Use this for animations / patterns to avoid 12x I2C round-trips.", '{"type": "object", "properties": {"colors": {"type": "array", "description": "Array of [r,g,b] triples, each 0..255", "items": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 255}, "minItems": 3, "maxItems": 3}, "minItems": 1, "maxItems": 12}}, "required": ["colors"]}', relay='self.led.set_many', prepare=_prepare_colors_json),
    ToolDef('clear_leds', 'Turn off all 12 RGB LEDs on the StackChan base.', '{"type": "object", "properties": {}}', relay='self.led.clear'),
    ToolDef('say', "Speak the given text on the device speaker via gateway-side TTS (Phase 4, Issue #70). The gateway synthesises audio, encodes it to Opus, and pushes frames over the existing WebSocket — the device firmware does not change. Engine is selectable via 'voice' (default 'voicevox'). NOTE: this build ships the framework only; concrete engines (VOICEVOX, Irodori) land in follow-up PRs and require the matching optional extra (e.g. 'pip install stackchan-mcp[tts-voicevox]'). Calling this tool before an engine is registered returns a clear error.", '{"type": "object", "properties": {"text": {"type": "string", "description": "Text to speak. Must be non-empty."}, "voice": {"type": "string", "description": "Engine identifier (e.g. \'voicevox\', \'irodori\'). Default \'voicevox\'.", "default": "voicevox"}, "speaker_id": {"type": "integer", "description": "Engine-specific speaker identifier (e.g. a VOICEVOX speaker ID)."}, "reference_audio": {"type": "string", "description": "Path to a reference audio file used by voice-cloning engines (e.g. Irodori). Ignored by engines that do not support it."}}, "required": ["text"]}', handler=HANDLERS['say']),
    ToolDef('listen', "Capture a short utterance from the device microphone and transcribe it via a gateway-side STT engine (Phase 4, Issue #91). The gateway sends a 'listen' notification over the existing WebSocket to put the device firmware into listening mode, buffers the Opus frames the device streams up during the capture window, then decodes and transcribes them once the window closes. Requires a minimal firmware change to handle the inbound 'listen' wire type (paired with this gateway release). Engine is selectable via 'engine' (default 'faster-whisper', local). Optional 'motion' feedback can switch the avatar to 'thinking' during capture ('face-only') or tilt the head up while preserving yaw ('look-up'). Install the relevant extra ('pip install stackchan-mcp[stt-faster-whisper]' or 'stt-openai'); calling this tool before an engine is registered returns a clear error.", '{"type": "object", "properties": {"duration_ms": {"type": "integer", "description": "Capture window in milliseconds. Clamped to [100, 30000].", "default": 5000, "minimum": 100, "maximum": 30000}, "engine": {"type": "string", "description": "Engine identifier (e.g. \'faster-whisper\', \'openai-whisper\'). Default \'faster-whisper\'.", "default": "faster-whisper"}, "language": {"type": "string", "description": "ISO 639-1 language code (e.g. \'ja\'). Pass an empty string or omit for autodetect.", "default": "ja"}, "model": {"type": "string", "description": "Engine-specific model identifier (e.g. \'base\' / \'small\' / \'medium\' for faster-whisper, \'whisper-1\' for OpenAI). Engines fall back to their default when omitted."}, "motion": {"type": "string", "enum": ["none", "face-only", "look-up"], "description": "Optional visible feedback during capture. \'none\' preserves the previous behaviour. \'face-only\' shows the thinking avatar during capture and restores idle at the end. \'look-up\' preserves yaw, tilts pitch to look_up_pitch, and holds the pose on success.", "default": "none"}, "look_up_pitch": {"type": "number", "description": "Pitch angle for motion=\'look-up\'. Must be between 5 and 85 degrees.", "default": 50.0, "minimum": 5, "maximum": 85}}}', handler=HANDLERS['listen']),
    ToolDef('i2c_scan', 'Scan the external I2C bus on Grove Port A and return all 7-bit addresses (probe range 0x08..0x77, excluding I2C reserved ranges) that ACK a probe. Use this to discover attached M5Stack Unit modules (ENV III, ToF, gas sensor, PaHub, etc.). On-board ICs on the internal bus are NOT included (this tool operates on a physically separate bus). Returns {"ok": true, "addresses": [...]}.', '{"type": "object", "properties": {}}', relay='self.i2c.scan'),
    ToolDef('i2c_read', 'Read n_bytes from an I2C device at 7-bit address `addr` on Grove Port A. Use this for protocols that read the device\'s current register / output without a preceding write. For typical \'write register address, then read\' patterns, use `i2c_write_read` instead. Returns {"ok": true, "bytes": [...]} or {"ok": false, "error": "ESP_ERR_TIMEOUT"} on NACK.', '{"type": "object", "properties": {"addr": {"type": "integer", "description": "7-bit I2C address; range 0x08..0x77 (I2C reserved ranges excluded \\u2014 matches the i2c_scan probe range).", "minimum": 8, "maximum": 119}, "n_bytes": {"type": "integer", "description": "Bytes to read (1..256).", "minimum": 1, "maximum": 256}}, "required": ["addr", "n_bytes"]}', relay='self.i2c.read'),
    ToolDef('i2c_write', 'Write bytes to an I2C device at 7-bit address `addr` on Grove Port A. `bytes` is an array of integers (0..255). This tool operates on the external Port A bus only; on-board ICs (PMIC, AW9523, touch, etc.) on the internal bus are not reachable.', '{"type": "object", "properties": {"addr": {"type": "integer", "description": "7-bit I2C address; range 0x08..0x77 (I2C reserved ranges excluded \\u2014 matches the i2c_scan probe range).", "minimum": 8, "maximum": 119}, "bytes": {"type": "array", "description": "Bytes to write (each 0..255).", "items": {"type": "integer", "minimum": 0, "maximum": 255}}}, "required": ["addr", "bytes"]}', relay='self.i2c.write'),
    ToolDef('i2c_write_read', "Write `write_bytes` to an I2C device at 7-bit address `addr` on Grove Port A, then read `n_bytes` back in a single Repeated Start transaction. Common 'set register pointer, then read' idiom: pass write_bytes=[reg_addr] to read from a specific register.", '{"type": "object", "properties": {"addr": {"type": "integer", "description": "7-bit I2C address; range 0x08..0x77 (I2C reserved ranges excluded \\u2014 matches the i2c_scan probe range).", "minimum": 8, "maximum": 119}, "write_bytes": {"type": "array", "description": "Bytes to write before reading (each 0..255).", "items": {"type": "integer", "minimum": 0, "maximum": 255}}, "n_bytes": {"type": "integer", "description": "Bytes to read (1..256).", "minimum": 1, "maximum": 256}}, "required": ["addr", "write_bytes", "n_bytes"]}', relay='self.i2c.write_read'),
    ToolDef('load_avatar_set', 'Load a dynamic avatar set onto the connected ESP32 (Phase 4.5 avatar pipeline). The gateway stages the payload on its HTTP server, notifies the device via WebSocket, and the device fetches + SHA256-verifies + loads it into PSRAM. ``archive_path`` must point to a raw RGB565 file on the gateway host: layered mode = 14 frames (face 6 + eyes 3 + mouth 5) totalling 537,600 bytes; matrix mode = 90 frames (6 × 3 × 5) totalling 3,456,000 bytes. Returns ok / checksum / bytes_transferred / error.', '{"type": "object", "properties": {"archive_path": {"type": "string", "description": "Filesystem path on the gateway host to the raw RGB565 payload."}, "mode": {"type": "string", "enum": ["layered", "matrix"], "description": "\'layered\' (14 frames, ~525 KB) or \'matrix\' (90 frames, ~3.3 MB)."}, "timeout": {"type": "number", "description": "Max seconds to wait for the device\'s avatar_set_loaded reply.", "default": 60.0, "minimum": 5.0, "maximum": 300.0}}, "required": ["archive_path", "mode"]}', handler=HANDLERS['load_avatar_set']),
    ToolDef('switchbot_list_devices', 'List the home\'s SwitchBot devices via the SwitchBot cloud API (v1.1). Returns \'deviceList\' (physical devices: bots, plugs, curtains, sensors, hubs, ...) and \'infraredRemoteList\' (IR appliances learned by a hub: lights, AC, TV, ...), each entry with deviceId / deviceName / deviceType (or remoteType). Call this first to resolve a spoken request to a deviceId — e.g. for "turn on the lights", find the matching light here, then call switchbot_send_command with command \'turnOn\'. Requires SWITCHBOT_TOKEN / SWITCHBOT_SECRET on the gateway; returns a clear error when unset.', '{"type": "object", "properties": {}}', handler=HANDLERS['switchbot_list_devices']),
    ToolDef('switchbot_get_status', "Get the current status of one physical SwitchBot device (power state, temperature/humidity, battery, etc., depending on deviceType) via the SwitchBot cloud API. Infrared remote devices have no status — use this only for deviceIds from 'deviceList'. Get the deviceId from switchbot_list_devices first.", '{"type": "object", "properties": {"device_id": {"type": "string", "description": "deviceId from switchbot_list_devices (physical devices only)."}}, "required": ["device_id"]}', handler=HANDLERS['switchbot_get_status']),
    ToolDef('switchbot_send_command', 'Control a SwitchBot device via the SwitchBot cloud API. Works for both physical devices and infrared remote devices (commandType \'command\' covers both; IR appliances support turnOn / turnOff and type-specific commands like setAll). Typical voice flow: "turn on the lights" → switchbot_list_devices → find the light\'s deviceId → send command \'turnOn\'. Common commands: turnOn, turnOff, toggle, press (Bot), setPosition (Curtain). Use commandType \'customize\' with the button name as \'command\' for user-defined IR buttons.', '{"type": "object", "properties": {"device_id": {"type": "string", "description": "deviceId from switchbot_list_devices (physical or infrared remote device)."}, "command": {"type": "string", "description": "Command name, e.g. \'turnOn\', \'turnOff\', \'press\', \'setPosition\' \\u2014 or a custom IR button name with commandType \'customize\'."}, "parameter": {"type": "string", "description": "Command parameter. Defaults to \'default\'. Command-specific, e.g. \'0,ff,80\' for Curtain setPosition.", "default": "default"}, "command_type": {"type": "string", "description": "\'command\' for standard commands (physical and IR), \'customize\' for user-defined IR buttons. Defaults to \'command\'.", "default": "command"}}, "required": ["device_id", "command"]}', handler=HANDLERS['switchbot_send_command']),
    ToolDef('web_search', "Search the web and return results as JSON. Uses the Tavily search API when TAVILY_API_KEY is set on the gateway (response includes an LLM-composed 'answer' summary), falling back to DuckDuckGo otherwise. Use this for questions about current events, facts you are unsure of, weather, prices, and anything the user asks you to look up. Results carry title / url / snippet — summarise them in your own words for spoken replies.", '{"type": "object", "properties": {"query": {"type": "string", "description": "Search query. Use the user\'s language (Japanese queries are fine)."}, "max_results": {"type": "integer", "description": "Number of results, 1-10.", "default": 5, "minimum": 1, "maximum": 10}}, "required": ["query"]}', handler=HANDLERS['web_search']),
    ToolDef('write_note', "Create or update a text note in the gateway's note directory (~/.stackchan/notes). Use this when the user asks to remember something, take a memo, keep a list, or save a search summary. Notes are plain Markdown/text files the user can open directly. Set append=true to add to an existing note (e.g. growing a shopping list) instead of overwriting.", '{"type": "object", "properties": {"name": {"type": "string", "description": "File name without directories, e.g. \'shopping list.md\'. A bare name gets \'.md\' appended; only .md/.txt allowed."}, "content": {"type": "string", "description": "Note body (UTF-8 text)."}, "append": {"type": "boolean", "description": "Append to the existing note instead of overwriting. Defaults to false.", "default": false}}, "required": ["name", "content"]}', handler=HANDLERS['write_note']),
    ToolDef('read_note', "Read one note from the gateway's note directory. Use list_notes first when unsure of the exact file name.", '{"type": "object", "properties": {"name": {"type": "string", "description": "File name from list_notes."}}, "required": ["name"]}', handler=HANDLERS['read_note']),
    ToolDef('list_notes', "List the notes saved in the gateway's note directory with their sizes and modification times.", '{"type": "object", "properties": {}}', handler=HANDLERS['list_notes']),]


# ---------------------------------------------------------------------------
# Lookup / call resolution
# ---------------------------------------------------------------------------

_TOOL_BY_NAME: dict[str, ToolDef] = {t.name: t for t in TOOLS}


def list_tools() -> list[Tool]:
    """MCP ``tools/list`` payload — all tools in declaration order."""
    return [tool.to_mcp() for tool in TOOLS]


async def call_tool(name: str, arguments: dict[str, Any], gateway: Any) -> list[TextContent]:
    """Run one tool: dispatch local handlers or relay to the ESP32.

    This is the single entry point for MCP ``tools/call`` and the HTTP
    control dashboard; errors and device disconnects are normalised into
    the standard ``{"error": ...}`` payload here.
    """
    tool = _TOOL_BY_NAME.get(name)
    if tool is None:
        return _err(f"Unknown tool: {name}")

    if tool.handler is not None:
        try:
            return _ok(await tool.handler(gateway, arguments))
        except (ValueError, NotImplementedError, RuntimeError, OSError) as exc:
            return _err(str(exc))

    if tool.device_required and not gateway.esp32.device_connected:
        return _err("No ESP32 device connected. Please check the device.")

    try:
        payload = arguments if tool.prepare is None else tool.prepare(arguments)
    except (TypeError, ValueError) as exc:
        return _err(str(exc))

    result, error = await gateway.esp32.call_tool(tool.relay, payload)
    if error:
        return _err(error.get("message", str(error)))

    if isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                return [TextContent(type="text", text="\n".join(texts))]
        return _ok(result)

    return [TextContent(type="text", text=str(result))]
