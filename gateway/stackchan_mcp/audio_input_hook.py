"""Device-driven listen audio forwarding to an external HTTP hook.

When the ESP32 device autonomously enters listening mode — wake word
detection (``WakeWordInvoke``), button press, or LCD touch
(``ToggleChatState``) — the gateway's MCP-driven STT pipeline is not
running because there is no concurrent ``listen()`` tool call to open a
recording slot (see :mod:`stackchan_mcp.audio_stream`).

This module fills that gap: it opens a recording slot on inbound
``{"type":"listen","state":"start"}`` messages, buffers the Opus
frames, packs them into an Ogg/Opus container (via
:mod:`stackchan_mcp.http`) on ``{"state":"stop"}``, and POSTs the
payload to the hook.

Configuration:

- ``STACKCHAN_AUDIO_HOOK_URL`` — HTTP(S) URL of the receiver. The
  device-driven capture path is silently disabled when unset.
- ``STACKCHAN_AUDIO_HOOK_TOKEN`` — Bearer token; falls back to
  ``STACKCHAN_TOKEN`` so a single-token setup works without extra
  configuration.
"""

from __future__ import annotations

import logging
from typing import Sequence

import aiohttp

from .http import (
    GRANULE_PER_FRAME,
    _build_opus_head_packet,
    _ogg_crc32,
    pack_opus_frames_to_ogg,
)

logger = logging.getLogger(__name__)

#: Public surface — the Ogg/Opus names are re-exported from
#: :mod:`stackchan_mcp.http` (tests import them from this module).
__all__ = [
    "GRANULE_PER_FRAME",
    "_build_opus_head_packet",
    "_ogg_crc32",
    "pack_opus_frames_to_ogg",
    "push_audio_capture",
]


async def push_audio_capture(
    hook_url: str,
    token: str,
    frames: Sequence[bytes],
    *,
    session_id: str = "",
    timeout_s: float = 10.0,
) -> bool:
    """POST a device-driven listen capture to the configured hook URL.

    Args:
        hook_url: Receiver URL (typically the SAIVerse-side
            ``audio_input_relay`` endpoint). Must be set.
        token: Bearer token for ``Authorization: Bearer <token>``.
            Empty string disables auth header (mirroring
            ``STACKCHAN_TOKEN`` semantics — gateway logs a warning at
            startup when the token is unset).
        frames: Raw Opus packets from the device for this listen window.
        session_id: ESP32 connection session ID, forwarded to the
            receiver via the ``X-StackChan-Session`` header so the
            receiver can correlate captures with vessel pairing.
        timeout_s: Total HTTP timeout (default 10s; an Ogg blob for a
            5-minute capture is well under 1 MB so this is generous).

    Returns:
        ``True`` if the POST returned 2xx, ``False`` otherwise (including
        on Ogg pack failure or network error). Failures are logged at
        WARNING; callers do not need to log again.
    """
    if not frames:
        logger.debug(
            "audio_input_hook: skipping push, no frames (session=%s)", session_id
        )
        return False

    try:
        ogg_payload = pack_opus_frames_to_ogg(frames)
    except Exception as exc:
        logger.warning(
            "audio_input_hook: Ogg pack failed for %d frames (session=%s): %s",
            len(frames), session_id, exc,
        )
        return False

    headers = {
        "Content-Type": "audio/ogg",
        "X-StackChan-Session": session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                hook_url,
                data=ogg_payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                if 200 <= response.status < 300:
                    logger.info(
                        "audio_input_hook: pushed %d frames (%d bytes) "
                        "session=%s status=%d",
                        len(frames), len(ogg_payload), session_id,
                        response.status,
                    )
                    return True
                body_snippet = (await response.text())[:200]
                logger.warning(
                    "audio_input_hook: POST returned status=%d session=%s "
                    "body=%r",
                    response.status, session_id, body_snippet,
                )
                return False
    except aiohttp.ClientError as exc:
        logger.warning(
            "audio_input_hook: POST failed (network error) session=%s: %s",
            session_id, exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "audio_input_hook: POST failed (unexpected) session=%s: %s",
            session_id, exc,
        )
        return False
