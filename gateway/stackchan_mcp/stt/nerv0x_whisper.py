"""nerv0x whisper.cpp engine — STT via the shared GPU box.

yorishiro-fork-specific engine (not intended for upstream PR).

The gateway runs on the nerv0x host next to the whisper.cpp server, so
instead of pulling local ``faster-whisper`` / CTranslate2 wheels into
the deployment we hand the decoded 16 kHz PCM to the shared server at
``http://127.0.0.1:9766/inference`` — the exact multipart call shape
documented in the nerv0x-services skill. The box does the thinking; the
gateway stays a thin HTTP client.

Environment variables:

- ``STACKCHAN_WHISPER_URL`` — whisper.cpp ``/inference`` URL. Defaults
  to ``http://127.0.0.1:9766/inference``.
- ``STACKCHAN_WHISPER_TIMEOUT`` — request timeout in seconds. Defaults
  to 30 (device capture windows are capped at 30 s, and the small model
  transcribes far faster than real time on GPU).
- ``STACKCHAN_WHISPER_LANGUAGE`` — optional default language code
  (ISO 639-1, e.g. ``pt``); per-call ``language`` opts win when set.

Like the VOICEVOX engine, ``httpx`` is imported lazily so the module
itself imports cleanly without the ``[tts]`` extra.
"""

from __future__ import annotations

import logging
import os
import wave
from io import BytesIO
from typing import Any

from .audio_utils import DEVICE_SAMPLE_RATE
from .base import STTEngine

logger = logging.getLogger(__name__)


#: Default whisper.cpp inference endpoint on the nerv0x GPU box.
DEFAULT_WHISPER_URL = "http://127.0.0.1:9766/inference"

#: Device captures are capped at 30 s; the ggml-small model on GPU
#: transcribes those in a few seconds. 30 s of headroom is generous.
DEFAULT_WHISPER_TIMEOUT = 30.0

#: whisper.cpp tolerances tuned for short robot utterances, matching
#: the values used in the nerv0x-services skill.
_NO_SPEECH_THOLD = "0.6"


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw signed-16-bit mono PCM in a WAV container in memory."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class Nerv0xWhisperEngine(STTEngine):
    """Transcribe PCM via the nerv0x whisper.cpp HTTP endpoint.

    Configuration:

        ``STACKCHAN_WHISPER_URL``
            Inference URL. Default ``http://127.0.0.1:9766/inference``.
        ``STACKCHAN_WHISPER_TIMEOUT``
            Request timeout in seconds. Default 30.
        ``STACKCHAN_WHISPER_LANGUAGE``
            Default language code; per-call ``language`` opts win.
    """

    name = "nerv0x-whisper"

    def __init__(
        self,
        url: str | None = None,
        timeout_seconds: float | None = None,
        default_language: str | None = None,
        transport: Any = None,
    ) -> None:
        """Construct the engine.

        ``transport`` is an :class:`httpx.BaseTransport` (or compatible)
        handed straight to :class:`httpx.AsyncClient` — tests pass an
        :class:`httpx.MockTransport`; production leaves it ``None``.
        """
        env_url = os.getenv("STACKCHAN_WHISPER_URL")
        self._url = (url or env_url or DEFAULT_WHISPER_URL).rstrip("/")

        env_timeout = os.getenv("STACKCHAN_WHISPER_TIMEOUT")
        if timeout_seconds is not None:
            self._timeout_seconds = timeout_seconds
        elif env_timeout:
            try:
                self._timeout_seconds = float(env_timeout)
            except ValueError:
                logger.warning(
                    "Invalid STACKCHAN_WHISPER_TIMEOUT=%r, falling back to %s",
                    env_timeout,
                    DEFAULT_WHISPER_TIMEOUT,
                )
                self._timeout_seconds = DEFAULT_WHISPER_TIMEOUT
        else:
            self._timeout_seconds = DEFAULT_WHISPER_TIMEOUT

        self._default_language = (
            default_language
            or os.getenv("STACKCHAN_WHISPER_LANGUAGE")
            or ""
        )
        self._transport = transport

    @property
    def url(self) -> str:
        """Inference URL the engine will POST to. Useful for diagnostics."""
        return self._url

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        """Transcribe PCM by POSTing a WAV to whisper.cpp.

        Recognised opts:

            ``language``: str | None
                ISO 639-1 code. ``None`` (or empty) enables
                server-side autodetection, falling back to
                ``STACKCHAN_WHISPER_LANGUAGE`` (env) if set.
            ``model``: str
                Ignored — whisper.cpp serves one server-side model.
        """
        if not pcm:
            raise ValueError("nerv0x-whisper transcribe: empty PCM buffer")

        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via integration
            raise RuntimeError(
                "httpx is not installed. Install with "
                "'pip install stackchan-mcp[tts]' to enable nerv0x STT support."
            ) from exc

        language_raw = opts.get("language")
        if isinstance(language_raw, str) and language_raw:
            language = language_raw
        else:
            language = self._default_language

        wav_bytes = _pcm_to_wav_bytes(pcm, DEVICE_SAMPLE_RATE)

        data: dict[str, str] = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "no_speech_thold": _NO_SPEECH_THOLD,
            "response_format": "verbose_json",
        }
        if language:
            data["language"] = language
        files = {
            "file": (
                "stackchan_listen.wav",
                wav_bytes,
                "audio/wav",
            )
        }

        client_kwargs: dict[str, Any] = {"timeout": self._timeout_seconds}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(self._url, data=data, files=files)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()

        text = result.get("text", "") or ""
        detected = result.get("language", "") or language

        transcription = {
            "text": text.strip(),
            "language": detected,
        }
        # Pass through diagnostics when the server provided them.
        if "duration" in result:
            transcription["duration"] = result["duration"]
        if "segments" in result:
            transcription["segments"] = result["segments"]

        logger.info(
            "nerv0x-whisper transcribed pcm_bytes=%d language=%s text=%r",
            len(pcm),
            detected,
            text[:80],
        )
        return transcription
