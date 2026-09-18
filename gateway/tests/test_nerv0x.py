"""Tests for the nerv0x STT/TTS engines (whisper.cpp + Qwen3-TTS).

Both engines are thin HTTP clients, so every behaviour is exercised
against an :class:`httpx.MockTransport` — no network, no GPU.
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")

from _audio_fixtures import make_wav_bytes  # noqa: E402
from stackchan_mcp.stt.nerv0x_whisper import (  # noqa: E402
    DEFAULT_WHISPER_URL,
    Nerv0xWhisperEngine,
)
from stackchan_mcp.tts.nerv0x_tts import (  # noqa: E402
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_URL,
    Nerv0xTTSEngine,
)

# A short 16 kHz mono PCM buffer (~100 ms).
PCM = bytes((i * 7) % 256 for i in range(1600))


def _whisper_handler(captured: list[dict]):
    """Emulate whisper.cpp /inference returning verbose_json."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "content": request.content,
            }
        )
        return httpx.Response(
            200,
            json={
                "text": "qual é a hora",
                "language": "pt",
                "duration": 1.28,
                "segments": [{"start": 0.0, "end": 1.28, "text": "qual é a hora"}],
            },
        )

    return handler


# ---------------------------------------------------------------------------
# nerv0x-whisper (STT)
# ---------------------------------------------------------------------------


def test_whisper_engine_name_is_nerv0x_whisper():
    """The registry uses ``name`` to look up engines from the listen tool."""
    engine = Nerv0xWhisperEngine()
    assert engine.name == "nerv0x-whisper"


def test_whisper_default_url():
    """Defaults to the nerv0x whisper.cpp inference endpoint."""
    engine = Nerv0xWhisperEngine()
    assert engine.url.rstrip("/") == DEFAULT_WHISPER_URL


def test_whisper_transcribe_posts_wav_and_parses_verbose_json():
    """A real multipart POST with the WAV attached; response parsed."""
    captured: list[dict] = []
    engine = Nerv0xWhisperEngine(transport=httpx.MockTransport(_whisper_handler(captured)))

    result = __import__("asyncio").run(
        engine.transcribe(PCM, language="pt")
    )

    assert captured, "engine never issued a request"
    request = captured[0]
    assert request["method"] == "POST"
    assert request["path"] == "/inference"
    # The WAV container must be attached as the file part.
    assert b"RIFF" in request["content"]
    # whisper.cpp tuning fields are sent.
    assert b"response_format" in request["content"]
    assert b"verbose_json" in request["content"]

    assert result["text"] == "qual é a hora"
    assert result["language"] == "pt"
    assert result["duration"] == 1.28
    assert "segments" in result


def test_whisper_language_omitted_when_empty():
    """language=None leaves the field off so the server autodetects."""
    captured: list[dict] = []
    engine = Nerv0xWhisperEngine(transport=httpx.MockTransport(_whisper_handler(captured)))

    __import__("asyncio").run(engine.transcribe(PCM, language=None))

    request = captured[0]
    assert b"language" not in request["content"]


def test_whisper_rejects_empty_pcm():
    """Empty input is a clear ValueError, not a network round-trip."""
    engine = Nerv0xWhisperEngine()
    with pytest.raises(ValueError):
        __import__("asyncio").run(engine.transcribe(b""))


# ---------------------------------------------------------------------------
# nerv0x (TTS)
# ---------------------------------------------------------------------------


def _tts_handler(captured: list[dict], *, sample_rate: int = 24000):
    """Emulate the OpenAI-compatible audio/speech endpoint returning a WAV."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "json": __import__("json").loads(request.content or b"{}"),
            }
        )
        wav = make_wav_bytes(
            sample_rate=sample_rate,
            duration_ms=100,
            samples=[i * 10 for i in range(sample_rate * 100 // 1000)],
        )
        return httpx.Response(200, content=wav)

    return handler


def test_tts_engine_name_is_nerv0x():
    """The registry uses ``name`` to look up engines from the say tool's voice arg."""
    engine = Nerv0xTTSEngine()
    assert engine.name == "nerv0x"


def test_tts_default_url_and_model():
    """Defaults match the nerv0x Qwen3-TTS deployment."""
    engine = Nerv0xTTSEngine()
    assert engine.url.rstrip("/") == DEFAULT_TTS_URL
    assert engine.model_name == DEFAULT_TTS_MODEL


def test_tts_synthesize_posts_and_decodes_to_16k_pcm():
    """Request carries model+input; WAV response decoded to device-rate PCM."""
    captured: list[dict] = []
    engine = Nerv0xTTSEngine(transport=httpx.MockTransport(_tts_handler(captured)))

    pcm = __import__("asyncio").run(engine.synthesize("olá stackchan"))

    assert captured, "engine never issued a request"
    request = captured[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/audio/speech"
    assert request["json"]["model"] == DEFAULT_TTS_MODEL
    assert request["json"]["input"] == "olá stackchan"

    # 100 ms of 24 kHz source resampled to 16 kHz mono s16 ≈ 3200 bytes.
    assert 3000 < len(pcm) <= 3500


def test_tts_rejects_empty_text():
    """Empty text is a clear ValueError, not a network round-trip."""
    engine = Nerv0xTTSEngine()
    with pytest.raises(ValueError):
        __import__("asyncio").run(engine.synthesize("   "))
