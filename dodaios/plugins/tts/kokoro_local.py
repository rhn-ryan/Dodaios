"""On-device text-to-speech: Kokoro (natural neural voice) via sherpa-onnx.

Kokoro is the more natural-sounding local option next to Piper: an 82M
parameter model with multiple English voices, running entirely on the CPU.
It needs no key and sends no text anywhere.

One model carries many speaker ids, so unlike Piper (one voice = one model)
this provider keeps a single loaded engine and selects the speaker by id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from dodaios.core.protocols import AudioChunk

log = logging.getLogger(__name__)

_INT16_FULL_SCALE = 32767.0
_DEFAULT_SPEED = 1.0

_MARKUP_RE = re.compile(r"[*_`#~|]+|^[ \t]*(?:[-•>]|\\d+[.)])[ \t]+", re.MULTILINE)


def _speech_text(text: str) -> str:
    return " ".join(_MARKUP_RE.sub(" ", text or "").split())


class KokoroLocalTTS:
    """Local Kokoro text-to-speech (keyless, on-device, English)."""

    name = "kokoro-local"
    supports_streaming = False
    last_voice: str | None = None
    last_voice_provider: str | None = None
    runs_on_device = True

    def __init__(
        self,
        *,
        voice: str | None = None,
        gender: str | None = None,
        num_threads: int = 4,
        speed: float | None = None,
    ) -> None:
        self._preferred_voice = (voice or "").strip() or None
        self._gender = (gender or "").strip().lower() or "feminine"
        self._num_threads = max(1, int(num_threads))
        self._speed = float(speed) if speed is not None and speed > 0 else _DEFAULT_SPEED
        self._engine: Any = None
        self._load_lock = asyncio.Lock()

    # -- voice selection ----------------------------------------------------
    def _installed_models(self) -> list[str]:
        from dodaios.speech.local_models import SHERPA_BUNDLES, bundle_present

        return [
            mid
            for mid, bundle in SHERPA_BUNDLES.items()
            if mid.startswith("kokoro") and bundle_present(mid)
        ]

    def _resolve_voice(self, voice: str | None, language_code: str | None) -> str | None:
        installed = self._installed_models()
        if not installed:
            return None
        requested = (voice or self._preferred_voice or "").strip()
        if requested in installed:
            return requested
        return installed[0]

    # -- engine -------------------------------------------------------------
    def _build_engine(self, model_id: str) -> Any:
        from dodaios.speech.local_models import import_runtime
        from dodaios.speech.sherpa_models import model_paths

        try:
            sherpa_onnx = import_runtime(
                "sherpa-onnx",
                needs=(
                    "OfflineTts",
                    "OfflineTtsConfig",
                    "OfflineTtsModelConfig",
                    "OfflineTtsKokoroModelConfig",
                ),
            )
        except ImportError as exc:
            raise RuntimeError(
                "The local speech runtime (sherpa-onnx) is not installed or "
                f"cannot be loaded ({exc}), so the on-device voice cannot speak."
            ) from exc

        paths = model_paths(model_id)
        model = paths.get("model.onnx")
        tokens = paths.get("tokens.txt")
        data_dir = paths.get("espeak-ng-data")
        voices = paths.get("voices.bin")
        if model is None or tokens is None or data_dir is None or voices is None:
            raise RuntimeError(f"Voice '{model_id}' is not catalogued correctly.")
        missing = [p.name for p in (model, tokens, data_dir, voices) if not p.exists()]
        if missing:
            raise RuntimeError(
                f"The voice '{model_id}' is not fully downloaded (missing: "
                f"{', '.join(missing)}). Download it from the API-Keys view."
            )

        log.info("Loading local Kokoro voice %s", model_id)
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(model),
                    voices=str(voices),
                    tokens=str(tokens),
                    data_dir=str(data_dir),
                ),
                num_threads=self._num_threads,
                provider="cpu",
            ),
        )
        return sherpa_onnx.OfflineTts(config)

    async def _engine_for(self, model_id: str) -> Any:
        if self._engine is not None:
            return self._engine
        async with self._load_lock:
            if self._engine is None:
                self._engine = await asyncio.to_thread(self._build_engine, model_id)
        return self._engine

    # -- synthesis ----------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        language_code: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        spoken = _speech_text(text)
        if not spoken:
            return
        model_id = self._resolve_voice(voice, language_code)
        if model_id is None:
            raise RuntimeError(
                "No local Kokoro voice is downloaded yet, so the on-device voice "
                "cannot speak. Download it from the API-Keys view, or switch "
                "voice output to a provider you have a key for."
            )
        engine = await self._engine_for(model_id)

        def _generate() -> Any:
            return engine.generate(spoken, sid=0, speed=self._speed)

        audio = await asyncio.to_thread(_generate)
        samples = getattr(audio, "samples", None)
        rate = int(getattr(audio, "sample_rate", 0) or getattr(engine, "sample_rate", 0) or 24000)
        if samples is None or len(samples) == 0:
            raise RuntimeError(f"Local Kokoro voice '{model_id}' generated no audio.")
        pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * _INT16_FULL_SCALE).astype(
            np.int16
        ).tobytes()
        self.last_voice = model_id
        self.last_voice_provider = self.name
        yield AudioChunk(pcm=pcm, sample_rate=rate, timestamp_ns=0, channels=1)

    def _ensure_client(self) -> None:
        """Synchronous warm-up: load the default engine so the first turn is fast."""
        model_id = self._resolve_voice(None, None)
        if model_id is None or self._engine is not None:
            return
        try:
            self._engine = self._build_engine(model_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("Local Kokoro warm-up skipped (%s).", exc)

    async def aclose(self) -> None:
        self._engine = None

    def list_voices(self, language: str | None = None) -> list[str]:
        return self._installed_models()
