"""nerv0x Qwen3-TTS engine — OpenAI-compatible synthesis on the GPU box.

yorishiro-fork-specific engine (not intended for upstream PR).

The gateway runs on the nerv0x host next to the Qwen3-TTS service, so
the ``say()`` tool synthesises speech by POSTing to the
OpenAI-compatible ``/v1/audio/speech`` endpoint (pt-PT reference voice
wired server-side) instead of running a local VOICEVOX engine. The
returned audio (MP3/WAV/OGG — whatever the server produces) is decoded
to 16 kHz mono s16 PCM with PyAV, the same decoder the voice-turn
bridge already uses, so the orchestrator's Opus pipeline is unchanged.

Environment variables:

- ``STACKCHAN_TTS_URL`` — OpenAI-compatible audio endpoint. Defaults
  to ``http://127.0.0.1:8002/v1/audio/speech``.
- ``STACKCHAN_TTS_MODEL`` — model id sent in the request body. Defaults
  to ``Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice``.
- ``STACKCHAN_TTS_TIMEOUT`` — request timeout in seconds. Defaults to 60.

Like the VOICEVOX engine, ``httpx`` is imported lazily so the module
itself imports cleanly without the ``[tts]`` extra. PyAV (``av``) is a
base dependency of the voice-turn bridge and is imported lazily too.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

from .base import TTSEngine

logger = logging.getLogger(__name__)


#: Default OpenAI-compatible audio endpoint on the nerv0x GPU box.
DEFAULT_TTS_URL = "http://127.0.0.1:8002/v1/audio/speech"

#: Model id served by the nerv0x Qwen3-TTS service (pt-PT reference
#: voice wired server-side).
DEFAULT_TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

#: One short spoken reply is a few seconds of audio; even a long reply
#: should synthesise in well under a minute on GPU.
DEFAULT_TTS_TIMEOUT = 60.0

#: Hard ceiling on decoded PCM (decompression-bomb guard): 120 s of
#: 16 kHz mono s16 audio. Mirrors the voice-turn bridge's guard.
MAX_PCM_BYTES = 120 * 16000 * 2


def _audio_bytes_to_pcm16k(data: bytes) -> bytes:
    """Decode arbitrary audio bytes (MP3/WAV/OGG) to 16 kHz mono s16 PCM.

    Uses PyAV so the engine tolerates whatever content type the TTS
    server happens to emit. Raises ValueError if the decoded audio
    exceeds :data:`MAX_PCM_BYTES`.
    """
    import av  # type: ignore[import-not-found]
    from av.audio.resampler import AudioResampler as _AvAudioResampler

    out = bytearray()
    resampler = _AvAudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
            if len(out) > MAX_PCM_BYTES:
                raise ValueError("decoded audio exceeds MAX_PCM_BYTES")
        # Flush the resampler's internal FIFO.
        for rframe in resampler.resample(None):
            out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
    if len(out) > MAX_PCM_BYTES:
        raise ValueError("decoded audio exceeds MAX_PCM_BYTES")
    return bytes(out)


class Nerv0xTTSEngine(TTSEngine):
    """Synthesise text via the nerv0x Qwen3-TTS HTTP endpoint.

    Configuration:

        ``STACKCHAN_TTS_URL``
            Base audio URL. Default ``http://127.0.0.1:8002/v1/audio/speech``.
        ``STACKCHAN_TTS_MODEL``
            Model id. Default ``Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice``.
        ``STACKCHAN_TTS_TIMEOUT``
            Request timeout in seconds. Default 60.
        ``STACKCHAN_TTS_VOICE``
            Default voice id sent when the caller does not pass one.
            Unset = no ``voice`` key (server-side default applies).
    """

    name = "nerv0x"

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        voice: str | None = None,
        transport: Any = None,
    ) -> None:
        """Construct the engine.

        ``transport`` is an :class:`httpx.BaseTransport` (or compatible)
        handed straight to :class:`httpx.AsyncClient` — tests pass an
        :class:`httpx.MockTransport`; production leaves it ``None``.
        """
        env_url = os.getenv("STACKCHAN_TTS_URL")
        self._url = (url or env_url or DEFAULT_TTS_URL).rstrip("/")

        env_model = os.getenv("STACKCHAN_TTS_MODEL")
        self._model_name = model or env_model or DEFAULT_TTS_MODEL

        self._voice = (
            voice or os.getenv("STACKCHAN_TTS_VOICE") or ""
        )

        env_timeout = os.getenv("STACKCHAN_TTS_TIMEOUT")
        if timeout_seconds is not None:
            self._timeout_seconds = timeout_seconds
        elif env_timeout:
            try:
                self._timeout_seconds = float(env_timeout)
            except ValueError:
                logger.warning(
                    "Invalid STACKCHAN_TTS_TIMEOUT=%r, falling back to %s",
                    env_timeout,
                    DEFAULT_TTS_TIMEOUT,
                )
                self._timeout_seconds = DEFAULT_TTS_TIMEOUT
        else:
            self._timeout_seconds = DEFAULT_TTS_TIMEOUT

        self._transport = transport

    @property
    def url(self) -> str:
        """Endpoint the engine will POST to. Useful for diagnostics."""
        return self._url

    @property
    def model_name(self) -> str:
        """Model id sent in the request body."""
        return self._model_name

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        """Synthesise ``text`` via the OpenAI-compatible speech endpoint.

        Returns 16 kHz mono s16 PCM, the documented
        :class:`~stackchan_mcp.tts.base.TTSEngine` contract.

        Recognised opts:

            ``voice``: str | None
                Optional voice id passed through to the server. When
                omitted the server-side default (pt-PT) applies.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("nerv0x TTS synthesize: 'text' must be a non-empty string")

        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via integration
            raise RuntimeError(
                "httpx is not installed. Install with "
                "'pip install stackchan-mcp[tts]' to enable nerv0x TTS support."
            ) from exc

        payload: dict[str, Any] = {
            "model": self._model_name,
            "input": text,
        }
        voice = opts.get("voice") or self._voice
        if isinstance(voice, str) and voice:
            payload["voice"] = voice

        client_kwargs: dict[str, Any] = {"timeout": self._timeout_seconds}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            audio_bytes = resp.content

        if not audio_bytes:
            raise RuntimeError(
                f"nerv0x TTS produced no audio for text {text[:60]!r}"
            )

        pcm = _audio_bytes_to_pcm16k(audio_bytes)
        logger.info(
            "nerv0x TTS synthesised %d bytes PCM (16 kHz mono) for text=%r",
            len(pcm),
            text[:60],
        )
        return pcm
