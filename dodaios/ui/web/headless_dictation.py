"""Headless dictation service for when no native SpeechPipeline is available.

Provides a minimal SpeechPipeline-compatible interface that uses
sounddevice for microphone capture and the configured STT provider
(e.g. Groq Whisper API) for transcription.

This enables dictation to work on headless servers where a full
SpeechPipeline cannot run (e.g. no pywebview, no GPU, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import sounddevice as sd

from dodaios.core.config import load_config
from dodaios.plugins.stt import build_stt_from_config
from dodaios.core.events import DictationTranscript, DictationCompleted
from dodaios.core.paths import user_data_dir

log = logging.getLogger(__name__)

# Format constants - same as dictation/audio.py
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
SAMPLE_WIDTH: int = 2  # bytes per sample (signed 16-bit)


class HeadlessDictationPipeline:
    """A SpeechPipeline-compatible interface for headless dictation.

    Captures audio via sounddevice and transcribes using the
    configured STT provider. Emits the same events as the real
    pipeline (DictationTranscript, DictationCompleted).
    """

    def __init__(self, stt: Any | None = None, input_device: str | None = None) -> None:
        """Initialize the headless dictation pipeline.

        Args:
            stt: An STT provider instance. If None, one will be built
                 from the current config.
            input_device: Name or index of the audio input device.
                          If None, the default input device is used.
        """
        self._input_device = input_device or "auto-headset"
        self._stt = stt or self._build_stt()
        self._utterance_stt = self._stt
        self._is_recording = False
        self._record_task: asyncio.Task[None] | None = None
        self._transcript: str = ""
        self._language: str = "en"
        self._output_dir: Path | None = None

        # Set up output directory for failed audio sidecars
        udd = user_data_dir()
        self._output_dir = Path(udd) / "data" / "dictation_audio"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _build_stt(self) -> Any:
        """Build an STT provider from the current configuration."""
        cfg = load_config().stt
        try:
            stt = build_stt_from_config(cfg)
            log.info("Built STT provider: %s (%s)", type(stt).__module__, type(stt).__name__)
            return stt
        except Exception as exc:
            log.warning("Failed to build STT from config: %s", exc)
            return None  # type: ignore[return-value]

    @property
    def dictation_active(self) -> bool:
        """Is the dictation lane engaged right now?"""
        return False

    def dictation_available(self) -> bool:
        """True if the server can run mic-dictation (a mic + an STT exist)."""
        return self._utterance_stt is not None and self._input_device != "none"

    @property
    def dictation_engine_status(self) -> dict[str, Any]:
        """Which recognizer reads the next dictation, for the UI."""
        try:
            # Return a sensible default when STT is available
            return {
                "local": self._utterance_stt is not None,
                "provider": getattr(self._utterance_stt, "name", ""),
                "model": getattr(self._utterance_stt, "model", ""),
                "fallback": "",
                "detail": "",
            }
        except Exception as exc:
            log.debug("dictation engine status unavailable (%s)", exc)
            return {
                "local": False,
                "provider": "",
                "model": "",
                "fallback": "",
                "detail": "",
            }

    async def start_dictation(self, *, target: str = "chat", source: str = "ws") -> bool:
        """Begin a transcribe-only dictation session.

        Args:
            target: Where the transcript goes. "chat" = composer input,
                    "insert" = paste into focused field.
            source: Where the request came from (for logging).

        Returns:
            True if dictation started successfully, False otherwise.
        """
        if self._is_recording:
            log.warning("Dictation already running, refusing to start")
            return False

        self._is_recording = True
        self._transcript = ""
        self._language = "auto"
        self._record_task = asyncio.create_task(self._record_and_transcribe(target, source))
        log.info("Headless dictation started (target=%s, source=%s)", target, source)
        return True

    def stop_dictation(self) -> bool:
        """Finish the running dictation session.

        Idempotent: stopping nothing is not an error.

        Returns:
            True if dictation was stopped (or was already stopped).
            Returns False if no dictation was running.
        """
        if not self._is_recording:
            return False

        self._is_recording = False
        # Cancel the recording task if it exists
        if self._record_task is not None:
            self._record_task.cancel()
            self._record_task = None

        # Emit final transcript/completed event - schedule for later
        # We cannot await here since this is sync, so use create_task
        import asyncio
        asyncio.create_task(self._finish_dictation(outcome="empty" if not self._transcript.strip() else "inserted"))
        log.info("Headless dictation stopped, transcript: %r", self._transcript)
        return True

    async def _record_and_transcribe(self, target: str, source: str) -> None:
        """Record audio from mic and transcribe it."""
        try:
            # Record a single chunk (up to max_seconds from config)
            max_seconds = 1800.0  # default from DictationConfig
            num_frames = int(max_seconds * SAMPLE_RATE)

            log.info("Recording dictation audio for up to %.1f seconds...", max_seconds)

            # Record audio
            recording = sd.rec(
                num_frames,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
            )
            sd.wait()  # Wait until recording is finished

            log.debug("Recording complete, shape: %s", recording.shape)

            # Transcribe using STT
            if self._utterance_stt is not None:
                try:
                    # Use transcribe_pcm if available, otherwise transcribe
                    transcript = await self._transcribe_pcm(recording.tobytes())
                    self._transcript = transcript.text
                    self._language = getattr(transcript, "language", "en") or "en"
                    log.info("Transcription result: %r (language: %s)", self._transcript, self._language)
                except Exception as exc:
                    log.warning("Transcription failed: %s", exc)
                    self._transcript = ""
                    self._language = "en"
            else:
                log.error("No STT provider available for transcription")
                self._transcript = ""

            # Emit completion event
            self._finish_dictation(outcome="empty" if not self._transcript.strip() else "inserted")

        except asyncio.CancelledError:
            log.info("Dictation recording cancelled")
            raise
        except Exception as exc:
            log.error("Error during dictation recording: %s", exc, exc_info=True)
            self._finish_dictation(outcome="failed", detail=str(exc))
        finally:
            self._is_recording = False
            self._record_task = None

    async def _transcribe_pcm(self, pcm_bytes: bytes) -> Any:
        """Transcribe raw PCM bytes using the STT provider."""
        stt = self._utterance_stt
        if stt is None:
            return type("Transcript", (), {"text": "", "language": "unknown", "confidence": 0.0})()

        # Check if the provider has transcribe_pcm method
        if hasattr(stt, "transcribe_pcm"):
            # transcribe_pcm is typically async
            try:
                result = await stt.transcribe_pcm(pcm_bytes, sample_rate=SAMPLE_RATE, language="auto")
                return result
            except TypeError:
                # Fallback: call transcribe_pcm without language
                result = stt.transcribe_pcm(pcm_bytes, sample_rate=SAMPLE_RATE)
                return result
        elif hasattr(stt, "transcribe"):
            # Async transcribe - collect chunks
            # For headless, we have raw PCM so we wrap it
            try:
                # Try the compat shim first
                result = await stt.transcribe_pcm(pcm_bytes, sample_rate=SAMPLE_RATE, language="auto")
                return result
            except TypeError:
                pass

            # Try _post_transcription path
            try:
                wav_bytes = _wrap_pcm_as_wav(pcm_bytes, sample_rate=SAMPLE_RATE, channels=1)
                # Check for _post_transcription
                if hasattr(stt, "_post_transcription"):
                    result = await stt._post_transcription(wav_bytes, language="auto")
                    return result
            except Exception:
                pass

            # Last resort: just return empty
            return type("Transcript", (), {"text": "", "language": "unknown", "confidence": 0.0})()

        # Default fallback
        return type("Transcript", (), {"text": "", "language": "unknown", "confidence": 0.0})()

    async def _finish_dictation(
        self,
        *,
        outcome: str = "inserted",
        text: str | None = None,
        raw_text: str | None = None,
        detail: str = "",
        method: str = "",
        language: str | None = None,
        stt_providers: tuple[str, ...] = (),
        stt_models: tuple[str, ...] = (),
        detected_languages: tuple[str, ...] = (),
        stt_latency_ms: int = 0,
        polish_status: str = "",
        polish_provider: str = "",
        polish_latency_ms: int = 0,
    ) -> None:
        """Publish DictationTranscript and DictationCompleted events."""
        from dodaios.core.runtime_refs import get_default_bus

        bus = get_default_bus()
        text = text or self._transcript
        raw_text = raw_text or self._transcript
        language = language or self._language

        # Publish DictationTranscript (live text feed)
        try:
            await bus.publish(
                DictationTranscript(
                    source_layer="headless.dictation",
                    text=text,
                    is_final=True,
                    language=language,
                )
            )
            log.debug("Published DictationTranscript: %r", text)
        except Exception as exc:
            log.warning("Failed to publish DictationTranscript: %s", exc)

        # Publish DictationCompleted (outcome)
        try:
            await bus.publish(
                DictationCompleted(
                    source_layer="headless.dictation",
                    outcome=outcome,
                    text=text or "",
                    raw_text=raw_text or "",
                    detail=detail or ("" if outcome == "inserted" else "The dictation ended before it produced text."),
                    method=method,
                    language=language or "en",
                    duration_s=0.0,
                    stt_providers=stt_providers,
                    stt_models=stt_models,
                    detected_languages=detected_languages,
                    stt_latency_ms=stt_latency_ms,
                    polish_status=polish_status,
                    polish_provider=polish_provider,
                    polish_latency_ms=polish_latency_ms,
                )
            )
            log.debug("Published DictationCompleted: outcome=%s, text=%s", outcome, text)
        except Exception as exc:
            log.warning("Failed to publish DictationCompleted: %s", exc)

    @staticmethod
    def _wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
        """Wrap raw PCM bytes as a WAV file (minimal header, no extra chunks)."""
        import struct

        n_frames = len(pcm_bytes) // (2 * channels)  # 2 bytes per sample
        if n_frames == 0:
            return b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x10\x00\x00\x64"

        # RIFF header
        riff = b"RIFF"
        file_size = 36 + len(pcm_bytes)  # 8 + 24 + 8 + n_frames*2 - 4
        riff += struct.pack("<I", file_size)
        riff += b"WAVE"

        # fmt sub-chunk
        fmt = b"fmt "
        fmt += struct.pack("<I", 16)  # sub-chunk size
        fmt += struct.pack("<H", 1)   # audio format (PCM)
        fmt += struct.pack("<H", channels)
        fmt += struct.pack("<I", sample_rate)
        byte_rate = sample_rate * channels * 2
        fmt += struct.pack("<I", byte_rate)
        fmt += struct.pack("<H", channels * 2)  # block align
        fmt += struct.pack("<H", 16)  # bits per sample

        # data sub-chunk
        data = b"data"
        data_size = len(pcm_bytes)
        data += struct.pack("<I", data_size)
        data += pcm_bytes

        return riff + fmt + data


# Singleton instance, initialized on first access
_headless_pipeline: HeadlessDictationPipeline | None = None


def get_headless_pipeline() -> HeadlessDictationPipeline | None:
    """Get or create the headless dictation pipeline instance."""
    global _headless_pipeline
    if _headless_pipeline is None:
        try:
            _headless_pipeline = HeadlessDictationPipeline()
            log.info("Headless dictation pipeline created")
        except Exception as exc:
            log.warning("Failed to create headless dictation pipeline: %s", exc)
            _headless_pipeline = None
    return _headless_pipeline


def reset_headless_pipeline() -> None:
    """Reset the headless dictation pipeline (useful for config changes)."""
    global _headless_pipeline
    _headless_pipeline = None