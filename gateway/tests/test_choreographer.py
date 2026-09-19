"""Tests for the interaction choreographer (in-conversation body language)."""

import asyncio
import json

from stackchan_mcp.choreographer import (
    Choreographer,
    _talking_steps,
)


class FakeESP32:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.device_connected = True
        self.angles = {"yaw": 0, "pitch": 40}
        self.fail_angles = False

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "self.robot.get_head_angles":
            if self.fail_angles:
                return None, {"code": -32000, "message": "boom"}
            return {
                "content": [{"type": "text", "text": json.dumps(self.angles)}]
            }, None
        return {"ok": True}, None


class FakeMultiturn:
    turn_count = 1


class FakeGateway:
    def __init__(self):
        self.esp32 = FakeESP32()
        self.multiturn = FakeMultiturn()


def _calls_by(ch, name: str):
    return [a for n, a in ch._gateway.esp32.calls if n == name]


async def _phase(ch, name: str, *args, **kwargs):
    """Call a fire-and-forget phase method and wait for its body to finish."""
    getattr(ch, name)(*args, **kwargs)
    # The phase spawns its background task synchronously before returning, so
    # if no task exists right after the call, none was started (e.g. a
    # no-op first-tool-flag) and there is nothing to wait on.
    task = ch._active
    if task is not None:
        await task


def run_phase(ch, name: str, *args, **kwargs):
    asyncio.run(_phase(ch, name, *args, **kwargs))


# ---- _talking_steps -------------------------------------------------


def test_talking_steps_size_matches_duration():
    steps = _talking_steps(3000)
    assert steps
    assert sum(s["duration_ms"] for s in steps) == 3000
    assert all(s["shape"] for s in steps)
    assert len(steps) <= 256


def test_talking_steps_zero_and_negative():
    assert _talking_steps(0) == []
    assert _talking_steps(-5) == []


# ---- whole-phase flows ----------------------------------------------


def test_engage_welcome_glance():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "engage")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "idle"
    assert _calls_by(ch, "self.display.set_blink") == [{"enabled": True}]
    # engage read home, stood at home + glance, then returned to home.
    moves = _calls_by(ch, "self.robot.set_head_angles")
    assert len(moves) >= 2
    assert moves[-1] == {"yaw": 0, "pitch": 40}  # lands back at home


def test_thinking_pensive_weave():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "thinking")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "thinking"
    moves = _calls_by(ch, "self.robot.set_head_angles")
    # weave swings to +swing and -swing around home (0/40).
    assert len(moves) >= 4
    yaws = {m["yaw"] for m in moves}
    assert 7 in yaws and -7 in yaws


def test_talk_dispatches_mouth_sequence_and_nods():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "talk", 900)
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "happy"
    mouth = _calls_by(ch, "self.display.set_mouth_sequence")
    assert mouth and "steps" in mouth[0]
    assert len(mouth[0]["steps"]) > 0
    moves = _calls_by(ch, "self.robot.set_head_angles")
    # speech nods dip pitch below home then return.
    assert any(m["pitch"] < 40 for m in moves)


def test_tool_step_only_first_tilts():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "tool_step", is_first=True)
    assert _calls_by(ch, "self.robot.set_head_angles")  # one consult-tilt

    ch2 = Choreographer(FakeGateway())
    run_phase(ch2, "tool_step", is_first=False)
    assert _calls_by(ch2, "self.robot.set_head_angles") == []


def test_release_restores_home_and_idle():
    ch = Choreographer(FakeGateway())
    ch._home = (10, 40)
    run_phase(ch, "release")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "idle"
    assert _calls_by(ch, "self.display.set_blink") == [{"enabled": True}]
    moves = _calls_by(ch, "self.robot.set_head_angles")
    assert moves[-1] == {"yaw": 10, "pitch": 40}


def test_unreadable_home_moves_nothing():
    gw = FakeGateway()
    gw.esp32.fail_angles = True
    ch = Choreographer(gw)
    run_phase(ch, "engage")
    # Face/blink still applied; no head motion without a home pose.
    assert _calls_by(ch, "self.robot.set_head_angles") == []
    assert _calls_by(ch, "self.display.set_avatar")
