"""Device audio parameters shared by the STT and TTS pipelines.

Both directions of the voice pipeline (inbound decode + outbound encode)
must agree on the device's Opus parameters, so they are defined once here
and re-exported by :mod:`stackchan_mcp.stt.audio_utils` and
:mod:`stackchan_mcp.tts.audio_utils`.

Device-side Opus parameters come from the firmware's hello handshake
(``firmware/main/protocols/websocket_protocol.cc::GetHelloMessage``)::
    sample_rate      = 16000 Hz
    channels         = 1 (mono)
    frame_duration_ms = OPUS_FRAME_DURATION_MS (60 ms)
"""

from __future__ import annotations

#: Opus sample rate the device encoder/decoder is configured for.
DEVICE_SAMPLE_RATE = 16000

#: Channel count (mono).
DEVICE_CHANNELS = 1

#: Opus frame duration in milliseconds.
DEVICE_FRAME_DURATION_MS = 60

#: Number of PCM samples per Opus frame at the device rate.
SAMPLES_PER_FRAME = DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000
