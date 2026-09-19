"""Shared aiohttp helpers and Ogg/Opus codecs for the gateway services.

Single home for the patterns that used to be copy-pasted per module:

- **aiohttp client** — :func:`post_json` (total-timeout JSON POST that
  logs the upstream body and raises ``RuntimeError`` on any non-200
  answer) and :func:`get_json` (content-type-agnostic GET used by the
  JMA fetcher). Callers: :mod:`stackchan_mcp.hermes_bridge`,
  :mod:`stackchan_mcp.local_llm`, :mod:`stackchan_mcp.web_search`,
  :mod:`stackchan_mcp.weather`.
- **web server** — :func:`json_error`, the ``{"error": ...}`` response
  idiom of :mod:`stackchan_mcp.capture_server`.
- **Ogg/Opus codecs** — :func:`pack_opus_frames_to_ogg` (device Opus
  frames → Ogg/Opus container, :mod:`stackchan_mcp.audio_input_hook`)
  and :func:`ogg_opus_to_pcm16k` (Ogg/Opus → 16 kHz mono s16 PCM via
  PyAV, :mod:`stackchan_mcp.hermes_bridge`).
"""

from __future__ import annotations

import io
import json
import logging
import struct
from collections.abc import AsyncIterator
from typing import Any, Sequence

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)


# --- aiohttp client ----------------------------------------------------------


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    name: str,
    timeout_s: float,
    headers: dict[str, str] | None = None,
    body_snippet: int = 500,
    log_warning: bool = False,
) -> Any:
    """POST ``payload`` as JSON and return the parsed JSON response body.

    Any non-200 answer is logged (upstream body truncated to
    ``body_snippet`` chars) and raises ``RuntimeError(f"{name} returned
    status=...")`` — the shared error discipline of the Hermes / local
    LLM / Tavily callers. aiohttp network errors propagate unchanged.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                (logger.warning if log_warning else logger.error)(
                    "%s status=%d body=%s", name, resp.status, body[:body_snippet]
                )
                raise RuntimeError(f"{name} returned status={resp.status}")
    return json.loads(body)


async def get_json(session: aiohttp.ClientSession, url: str) -> Any:
    """GET ``url`` on an existing session and return parsed JSON.

    The content-type check is skipped (JMA serves JSON with a
    text/plain-ish content type); HTTP errors raise
    ``aiohttp.ClientResponseError`` from ``raise_for_status``.
    """
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def post_sse_events(
    url: str,
    payload: dict[str, Any],
    *,
    name: str,
    timeout_s: float,
    headers: dict[str, str] | None = None,
    body_snippet: int = 500,
) -> AsyncIterator[tuple[str, Any]]:
    """POST JSON and yield ``(event, data)`` for each SSE ``data:`` line.

    The Hermes API server streams agent activity as
    ``text/event-stream``: standard OpenAI ``chat.completion.chunk``
    objects arrive as bare ``data:`` lines (event name ``""``) and
    agent tool runs arrive as ``event: hermes.tool.progress`` followed
    by a ``data:`` JSON body (``{"tool": ..., "label": ...,
    "status": "running"}``). Each line is parsed and yielded with its
    SSE event name so callers can react to tool progress without
    waiting for the final reply.

    Any non-200 answer is logged and raises ``RuntimeError`` exactly
    like :func:`post_json`. The stream is read incrementally
    (``resp.content``), so events surface as they happen; the total
    wall-clock bound is still enforced by ``timeout_s``.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    "%s status=%d body=%s", name, resp.status, body[:body_snippet]
                )
                raise RuntimeError(f"{name} returned status={resp.status}")
            event = ""
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                    continue
                if line.startswith("data:"):
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        return
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "%s: non-JSON SSE data: %s", name, data[:body_snippet]
                        )
                        event = ""
                        continue
                    yield event, parsed
                    event = ""


# --- aiohttp web server helpers ----------------------------------------------


def json_error(message: str, status: int = 400) -> web.Response:
    """A JSON ``{"error": message}`` response with the given status."""
    return web.Response(
        text=json.dumps({"error": message}),
        status=status,
        content_type="application/json",
    )


# --- Ogg/Opus codecs ---------------------------------------------------------
#
# Device audio parameters (firmware xiaozhi-esp32 defaults): 16 kHz
# mono, 60 ms Opus frames, one frame per WebSocket binary message.

DEVICE_SAMPLE_RATE = 16000
DEVICE_FRAME_DURATION_MS = 60

#: Audio samples per Opus frame at the device sample rate.
SAMPLES_PER_FRAME = DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000  # 960

#: Opus granule positions are always expressed in 48 kHz samples, even
#: when the underlying stream is 16 kHz mono (RFC 7845 §4.1.7). So one
#: 60 ms frame advances the granule by 48000 * 60/1000 = 2880.
GRANULE_PER_FRAME = 48000 * DEVICE_FRAME_DURATION_MS // 1000  # 2880

#: How many Opus frames to pack into a single audio page (≈3 s). The
#: Ogg spec allows up to 255 segments per page; smaller pages give
#: finer-grained recovery on corruption but waste header bytes.
_FRAMES_PER_PAGE = 50

#: Upper bound for the decoded PCM (decompression-bomb guard): 120 s of
#: 16 kHz mono s16 audio. A malicious Ogg can expand far beyond its
#: wire size; abort the decode loop once past this.
MAX_PCM_BYTES = 120 * 16000 * 2


def ogg_opus_to_pcm16k(data: bytes) -> bytes:
    """Decode an Ogg/Opus capture to 16 kHz mono s16 PCM via PyAV.

    Raises ValueError if the decoded audio exceeds :data:`MAX_PCM_BYTES`.
    """
    import av
    from av.audio.resampler import AudioResampler

    out = bytearray()
    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
            if len(out) > MAX_PCM_BYTES:
                raise ValueError(f"decoded audio exceeds {MAX_PCM_BYTES} bytes PCM")
        # Flush the resampler's internal FIFO.
        for rframe in resampler.resample(None):
            out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
    if len(out) > MAX_PCM_BYTES:
        raise ValueError(f"decoded audio exceeds {MAX_PCM_BYTES} bytes PCM")
    return bytes(out)


# --- Ogg/Opus encoder (raw Opus frames → Ogg/Opus container) ---
#
# Ogg page layout (RFC 3533 §6):
#   0..3   "OggS"
#   4      stream_structure_version (0)
#   5      header_type_flag (0x02 BOS, 0x04 EOS, 0x01 continued; can OR)
#   6..13  granule_position (int64 LE)
#   14..17 bitstream_serial_number (uint32 LE)
#   18..21 page_sequence_number (uint32 LE)
#   22..25 CRC32 (zeroed during calculation, then patched)
#   26     number_of_page_segments (1..255)
#   27..   segment_table (one byte per segment, 0..255 each)
#   ..     segment data (concatenated)
#
# CRC32 polynomial: 0x04C11DB7, MSB-first, no initial value, no final
# XOR. This differs from zlib.crc32; we precompute a table.

_OGG_MAGIC = b"OggS"
_OPUS_HEAD_MAGIC = b"OpusHead"
_OPUS_TAGS_MAGIC = b"OpusTags"
_HEADER_BOS = 0x02
_HEADER_EOS = 0x04

_OGG_CRC_TABLE: list[int] = []
for _byte in range(256):
    _crc = _byte << 24
    for _ in range(8):
        _crc = (
            ((_crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            if _crc & 0x80000000
            else (_crc << 1) & 0xFFFFFFFF
        )
    _OGG_CRC_TABLE.append(_crc)


def _ogg_crc32(data: bytes) -> int:
    """Compute Ogg's CRC32 over ``data`` (table-driven, MSB-first)."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _OGG_CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return crc


def _packet_to_segments(packet: bytes) -> list[bytes]:
    """Split an Ogg packet into ≤255-byte lacing segments (RFC 3533 §6).

    Packets longer than 255 bytes are split into 255-byte runs; a
    packet whose length is an exact multiple of 255 gets a trailing
    zero-length segment so the parser knows it ended there. VBR Opus
    frames can exceed 255 bytes in practice.
    """
    if not packet:
        return [b""]
    segments = [packet[i : i + 255] for i in range(0, len(packet), 255)]
    if len(packet) % 255 == 0:
        segments.append(b"")
    return segments


def _build_ogg_page(
    *,
    header_type: int,
    granule_position: int,
    serial: int,
    page_sequence: int,
    segments: Sequence[bytes],
) -> bytes:
    """Assemble one Ogg page (RFC 3533 §6) and patch in its CRC."""
    if not 1 <= len(segments) <= 255:
        raise ValueError(f"Ogg page must have 1..255 segments, got {len(segments)}")
    if any(len(s) > 255 for s in segments):
        raise ValueError("Ogg segment exceeds 255 bytes")
    body = b"".join(segments)
    header = (
        struct.pack(
            "<4sBBqII",
            _OGG_MAGIC,
            0,  # stream_structure_version
            header_type,
            granule_position,
            serial,
            page_sequence,
        )
        + b"\x00\x00\x00\x00"  # CRC placeholder
        + bytes([len(segments)])
        + bytes(len(s) for s in segments)
    )
    page = header + body
    return page[:22] + struct.pack("<I", _ogg_crc32(page)) + page[26:]


def _build_opus_head_packet(
    *,
    channels: int = 1,
    pre_skip: int = 0,
    input_sample_rate: int = DEVICE_SAMPLE_RATE,
) -> bytes:
    """OpusHead identification header packet (RFC 7845 §5.1)."""
    return struct.pack(
        "<8sBBHIhB",
        _OPUS_HEAD_MAGIC,
        1,  # version
        channels,
        pre_skip,
        input_sample_rate,  # informational; decoder always runs at 48 kHz
        0,  # output_gain (Q7.8 dB), 0 = unchanged
        0,  # channel_mapping_family: 0 = mono/stereo
    )


def _build_opus_tags_packet(vendor: bytes = b"stackchan-mcp") -> bytes:
    """OpusTags comment header packet (RFC 7845 §5.2); empty comment list."""
    return (
        _OPUS_TAGS_MAGIC
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", 0)  # comment_count
    )


def pack_opus_frames_to_ogg(
    frames: Sequence[bytes],
    *,
    serial: int = 1,
    channels: int = 1,
    pre_skip: int = 0,
) -> bytes:
    """Pack raw Opus frames into a complete Ogg/Opus stream.

    Args:
        frames: One raw Opus packet per element, as emitted by the
            xiaozhi-esp32 firmware (one packet per WebSocket binary
            message). Empty input yields ``b""`` so callers can
            short-circuit "no audio" without raising.
        serial: Ogg bitstream serial number; opaque to the decoder but
            must be present in every page (we never multiplex, so any
            non-zero value works).
        channels: 1 (mono) or 2 (stereo); the firmware sends mono.
        pre_skip: Samples to drop at the start of decoded output, in
            48 kHz units (RFC 7845 §5.1); 0 is the conservative
            default.

    Returns:
        The full Ogg/Opus stream (BOS-OpusHead page, OpusTags page,
        one or more audio pages of ``_FRAMES_PER_PAGE`` frames, the
        last marked EOS) — ready to POST as ``audio/ogg``.
    """
    if not frames:
        return b""

    out = bytearray()
    page_seq = 0

    # Pages 0/1: BOS-OpusHead, then OpusTags (required before audio).
    out += _build_ogg_page(
        header_type=_HEADER_BOS,
        granule_position=0,
        serial=serial,
        page_sequence=page_seq,
        segments=[
            _build_opus_head_packet(
                channels=channels,
                pre_skip=pre_skip,
                input_sample_rate=DEVICE_SAMPLE_RATE,
            )
        ],
    )
    page_seq += 1
    out += _build_ogg_page(
        header_type=0,
        granule_position=0,
        serial=serial,
        page_sequence=page_seq,
        segments=[_build_opus_tags_packet()],
    )
    page_seq += 1

    # Audio pages: _FRAMES_PER_PAGE frames each, last one marked EOS.
    # VBR frames > 255 bytes split into multiple lacing segments, so a
    # page's segment count can exceed the frame count — flush mid-batch
    # when the segment table is about to overflow 255 entries.
    granule = 0
    for start in range(0, len(frames), _FRAMES_PER_PAGE):
        batch = frames[start : start + _FRAMES_PER_PAGE]
        granule += len(batch) * GRANULE_PER_FRAME
        is_last = start + len(batch) == len(frames)
        segments: list[bytes] = []
        for frame in batch:
            frame_segs = _packet_to_segments(frame)
            if len(segments) + len(frame_segs) > 255:
                out += _build_ogg_page(
                    header_type=0,  # continuation page
                    granule_position=granule,
                    serial=serial,
                    page_sequence=page_seq,
                    segments=segments,
                )
                page_seq += 1
                segments = []
            segments.extend(frame_segs)
        if segments:
            out += _build_ogg_page(
                header_type=_HEADER_EOS if is_last else 0,
                granule_position=granule,
                serial=serial,
                page_sequence=page_seq,
                segments=segments,
            )
            page_seq += 1

    return bytes(out)
