"""Headless dictation service for when no native SpeechPipeline is available.

The desktop app runs a full ``SpeechPipeline`` that owns the microphone,
dictation hotkeys, history and insertion. A headless server has no such
pipeline (``JARVIS_VOICE=0``), yet the web UI still offers the dictation tab
and its microphone button. This module is the minimal stand-in: it captures
the microphone and transcribes with the configured STT provider.

It deliberately mirrors the *interface* of ``SpeechPipeline`` — methods, not
properties, for ``dictation_active()`` / ``dictation_engine_status()`` — so
the REST routes and the WebSocket handler cannot tell the two apart.

One rule shapes the implementation: capture NEVER runs on the asyncio event
loop. A previous version called ``sounddevice.rec()`` for ``max_seconds``
(1800 s) and then ``sounddevice.wait()`` inside an ``async`` task, which
blocked the server's only event loop for up to half an hour and made the whole
UI freeze. A short ``InputStream.read()`` loop in a daemon thread keeps the
loop responsive and lets ``stop_dictation()`` end a recording immediately.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger as log

from dodaios.core.config import load_config
from dodaios.core.events import DictationCompleted, DictationTranscript
from dodaios.core.paths import user_data_dir
from dodaios.plugins.stt import build_stt_from_config

# Format constants — same as dictation/audio.py
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
SAMPLE_WIDTH: int = 2  # bytes per sample (signed 16-bit)

#: Frames read per blocking call. 1024 @ 16 kHz ≈ 64 ms, small enough that
#: ``stop_dictation()`` is felt immediately and large enough not to spin.
_BLOCK_FRAMES = 1024



class HeadlessDictationPipeline:
    """A ``SpeechPipeline``-compatible dictation surface for headless hosts.

    Only the pieces the browser UI uses are implemented: availability, a
    recording start/stop, transcription with the configured provider, and the
    same bus events a native dictation publishes. Insertion and hotkeys are
    intrinsically absent on a host with no desktop session.
    """

    def __init__(self, stt: Any | None = None, input_device: str | None = None) -> None:
        self._stt = stt or self._build_stt()
        self._utterance_stt = self._stt
        self._input_device = input_device or self._configured_input_device()
        self._device_index = self._resolve_input_device(self._input_device)
        self._is_recording = False
        self._stop_event = threading.Event()
        self._record_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at = 0.0
        self._error: str = ""

        # Failed-audio sidecars live beside the real dictation ones.
        try:
            udd = user_data_dir()
            self._output_dir: Path | None = Path(udd) / "data" / "dictation_audio"
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001 — a missing data dir must not break boot
            self._output_dir = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_stt(self) -> Any:
        """Build the STT provider from ``[stt]``, or ``None`` when unusable."""
        try:
            cfg = load_config().stt
            stt = build_stt_from_config(cfg)
            log.info(
                "Headless dictation STT: {} ({})",
                type(stt).__module__,
                type(stt).__name__,
            )
            return stt
        except Exception as exc:  # noqa: BLE001 — no provider is a clean no-op
            log.warning("Headless dictation: no STT provider ({})", exc)
            return None

    def _configured_input_device(self) -> str:
        try:
            return str(load_config().audio.input_device or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _resolve_input_device(name: str) -> int | None:
        """Turn ``[audio].input_device`` into a real PortAudio input index.

        Accepts the configured name (``pulse``, ``pipewire``, part of a device
        name), ``auto``/``default``/empty for the system default, and finally
        falls back to the first device that has input channels. Returns
        ``None`` when the host has no capture device at all — the honest
        answer for ``dictation_available``.
        """
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            target = (name or "").strip().lower()
            if target and target not in ("auto", "default", "auto-headset"):
                for index, device in enumerate(devices):
                    if device.get("max_input_channels", 0) > 0 and target in str(
                        device.get("name", "")
                    ).lower():
                        return index
            try:
                default_index = sd.default.device[0]
            except Exception:  # noqa: BLE001
                default_index = None
            if (
                isinstance(default_index, int)
                and 0 <= default_index < len(devices)
                and devices[default_index].get("max_input_channels", 0) > 0
            ):
                return default_index
            for index, device in enumerate(devices):
                if device.get("max_input_channels", 0) > 0:
                    return index
        except Exception as exc:  # noqa: BLE001 — no PortAudio is "no microphone"
            log.info("Headless dictation: no audio input available ({})", exc)
        return None

    # ------------------------------------------------------------------
    # SpeechPipeline-compatible status surface
    # ------------------------------------------------------------------

    def dictation_available(self) -> bool:
        """True when a microphone AND an STT provider exist on this host."""
        return self._utterance_stt is not None and self._device_index is not None

    def dictation_active(self) -> bool:
        """Is a dictation recording right now? (Method, like SpeechPipeline.)"""
        return self._is_recording

    def dictation_engine_status(self) -> dict[str, Any]:
        """Which recognizer reads the next dictation, for the UI."""
        stt = self._utterance_stt
        if stt is None:
            return {
                "local": False,
                "provider": "",
                "model": "",
                "fallback": "",
                "detail": "",
            }
        local = bool(getattr(stt, "is_local", False)) or "local" in type(stt).__name__.lower()
        return {
            "local": local,
            "provider": str(
                getattr(stt, "name", "")
                or getattr(stt, "provider", "")
                or type(stt).__name__
            ),
            "model": str(getattr(stt, "model", "") or ""),
            "fallback": "",
            "detail": "",
        }

    def _dictation_stt(self) -> Any:
        """The reader a Restore uses (same recognizer as a live dictation)."""
        return self._utterance_stt

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start_dictation(self, *, target: str = "chat", source: str = "api") -> bool:
        """Begin a dictation. Returns False when it cannot start.

        Synchronous on purpose, exactly like ``SpeechPipeline.start_dictation``:
        the REST routes used to ``await`` this call and broke on the native
        pipeline. It only flips flags and starts a daemon thread, so it is safe
        to call from the event loop.
        """
        if self._is_recording:
            log.info("Headless dictation already running — start refused.")
            return False
        if not self.dictation_available():
            log.info(
                "Headless dictation unavailable (stt={}, device={}).",
                self._utterance_stt is not None,
                self._device_index,
            )
            return False

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # called from a plain thread (CLI/threadpool)
            self._loop = None

        # "auto" has no foreground window to paste into on a headless host;
        # deliver the text to the chat composer instead.
        self._target = "chat" if target in ("", "auto", "chat") else target
        self._is_recording = True
        self._error = ""
        self._started_at = time.monotonic()
        self._stop_event.clear()
        self._record_thread = threading.Thread(
            target=self._capture,
            name="headless-dictation",
            daemon=True,
        )
        self._record_thread.start()
        log.info(
            "Headless dictation started (target={}, source={}, device={}).",
            self._target,
            source,
            self._device_index,
        )
        return True

    def stop_dictation(self) -> bool:
        """End the running dictation and transcribe what was captured."""
        if not self._is_recording:
            return False
        # Never join here: this is called on the event loop, and a wedged
        # driver must not freeze the server. The capture thread stops within
        # one 64 ms block and schedules the transcription on this loop.
        self._stop_event.set()
        log.info("Headless dictation stopped; transcribing.")
        return True

    # ------------------------------------------------------------------
    # Capture (worker thread — never the event loop)
    # ------------------------------------------------------------------

    def _capture(self) -> None:
        """Read microphone blocks until stopped or the duration cap is hit."""
        chunks: list[bytes] = []
        error = ""
        started = time.monotonic()
        max_seconds = self._max_seconds()
        try:
            import sounddevice as sd

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=self._device_index,
                blocksize=_BLOCK_FRAMES,
            ) as stream:
                while not self._stop_event.is_set():
                    if (time.monotonic() - started) >= max_seconds:
                        log.info(
                            "Headless dictation reached its {:.0f} s limit.", max_seconds
                        )
                        break
                    data, _overflowed = stream.read(_BLOCK_FRAMES)
                    if len(data):
                        chunks.append(bytes(data))
        except Exception as exc:  # noqa: BLE001 — surfaced in the outcome
            error = str(exc)
            log.opt(exception=exc).warning("Headless dictation capture failed: {}", exc)
        finally:
            pcm = b"".join(chunks)
            duration = max(0.0, time.monotonic() - started)
            self._dispatch_finish(pcm, duration, error)

    def _max_seconds(self) -> float:
        """The configured ceiling, or ``inf`` when the user set ``0``."""
        try:
            configured = float(load_config().dictation.max_seconds)
        except Exception:  # noqa: BLE001
            return 1800.0
        return configured if configured > 0 else float("inf")

    def _dispatch_finish(self, pcm: bytes, duration: float, error: str) -> None:
        """Hand the finished audio back to the event loop, safely."""
        loop = self._loop
        coro = self._finish(pcm, duration, error)
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            except Exception as exc:  # noqa: BLE001 — a stopped loop is expected
                log.debug("Headless dictation could not schedule finish: {}", exc)
                coro.close()
        # No loop (CLI start): finish synchronously in this thread.
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            log.warning("Headless dictation finish failed: {}", exc)

    # ------------------------------------------------------------------
    # Transcription + delivery
    # ------------------------------------------------------------------

    async def _finish(self, pcm: bytes, duration: float, error: str) -> None:
        raw = ""
        language = "unknown"
        detail = error
        try:
            if pcm and self._utterance_stt is not None:
                transcript = await self._transcribe(pcm)
                raw = str(
                    getattr(transcript, "raw_text", "")
                    or getattr(transcript, "text", "")
                    or ""
                ).strip()
                language = str(getattr(transcript, "language", "") or "unknown")
                if not raw and not error:
                    detail = "The microphone captured no speech."
            elif not error:
                detail = "The microphone captured no audio."
        except Exception as exc:  # noqa: BLE001 — never lose the outcome
            detail = str(exc)
            log.opt(exception=exc).warning("Headless dictation transcription failed: {}", exc)
        finally:
            self._is_recording = False
            self._record_thread = None

        text = raw
        clean_reason = ""
        if raw:
            try:
                from dodaios.dictation.formatting import format_dictation

                cfg = load_config().dictation
                formatted = format_dictation(
                    raw,
                    language=language,
                    fillers=bool(getattr(cfg, "remove_fillers", True)),
                    paragraphs=bool(getattr(cfg, "auto_paragraphs", True)),
                    max_removed_fraction=float(
                        getattr(cfg, "filler_max_removed_fraction", 0.25)
                    ),
                )
                text = formatted.text
                clean_reason = getattr(formatted, "filler_reason", "") or ""
            except Exception:  # noqa: BLE001 — formatting is never allowed to lose text
                log.debug("Headless dictation formatting unavailable")

        outcome = "chat" if text else ("failed" if detail else "empty")
        await self._publish(text=text, raw=raw, language=language, outcome=outcome,
                            detail=detail or "", duration=duration)
        self._record_history(
            raw=raw,
            text=text,
            language=language,
            outcome=outcome,
            detail=detail or "",
            duration=duration,
            clean_reason=clean_reason,
        )

    async def _transcribe(self, pcm: bytes) -> Any:
        stt = self._utterance_stt
        transcribe = getattr(stt, "transcribe_pcm", None)
        if callable(transcribe):
            try:
                return await transcribe(pcm, sample_rate=SAMPLE_RATE, language="auto")
            except TypeError:
                # Older provider without keyword support.
                return await transcribe(pcm)
        # Container-only provider: wrap the PCM and use its file path.
        post = getattr(stt, "_post_transcription", None)
        if callable(post):
            return await post(self._wrap_pcm_as_wav(pcm), language="auto")
        raise RuntimeError("The configured STT provider cannot read raw microphone audio.")

    async def _publish(
        self,
        *,
        text: str,
        raw: str,
        language: str,
        outcome: str,
        detail: str,
        duration: float,
    ) -> None:
        from dodaios.core.runtime_refs import get_default_bus

        bus = get_default_bus()
        if bus is None:
            return
        try:
            await bus.publish(
                DictationTranscript(
                    source_layer="headless.dictation",
                    text=text,
                    is_final=True,
                    target="chat",
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Headless dictation transcript publish failed: {}", exc)
        try:
            await bus.publish(
                DictationCompleted(
                    source_layer="headless.dictation",
                    outcome=outcome,
                    text=text,
                    raw_text=raw,
                    detail=detail,
                    method="chat" if text else "",
                    language=language or "en",
                    duration_s=duration,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Headless dictation completed publish failed: {}", exc)

    def _record_history(
        self,
        *,
        raw: str,
        text: str,
        language: str,
        outcome: str,
        detail: str,
        duration: float,
        clean_reason: str,
    ) -> None:
        try:
            from dodaios.dictation.cleanup import count_words
            from dodaios.dictation.history import DictationHistory

            cfg = load_config().dictation
            if not bool(getattr(cfg, "history_enabled", True)):
                return
            DictationHistory().add(
                raw_text=raw,
                text=text,
                language=language if language != "unknown" else "",
                duration_s=duration,
                outcome=outcome,
                method="chat" if text else "",
                cleanup_reason=clean_reason,
                word_count=count_words(text),
                error=detail or None,
                max_entries=int(getattr(cfg, "history_max_entries", 200) or 200),
                retention_days=int(getattr(cfg, "history_retention_days", 30) or 30),
            )
        except Exception as exc:  # noqa: BLE001 — history is a record, not the delivery
            log.debug("Headless dictation history write skipped: {}", exc)

    @staticmethod
    def _wrap_pcm_as_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
        """Wrap raw int16 PCM as a minimal WAV container."""
        import struct

        if not pcm:
            return b""
        header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        header += b"fmt " + struct.pack("<I", 16)
        header += struct.pack("<H", 1)  # PCM
        header += struct.pack("<H", channels)
        header += struct.pack("<I", sample_rate)
        header += struct.pack("<I", sample_rate * channels * 2)
        header += struct.pack("<H", channels * 2)
        header += struct.pack("<H", 16)
        header += b"data" + struct.pack("<I", len(pcm))
        return header + pcm


# Singleton instance, initialized on first access
_headless_pipeline: HeadlessDictationPipeline | None = None
_headless_lock = threading.Lock()


def get_headless_pipeline() -> HeadlessDictationPipeline | None:
    """Get or create the headless dictation pipeline instance."""
    global _headless_pipeline
    if _headless_pipeline is None:
        with _headless_lock:
            if _headless_pipeline is None:
                try:
                    _headless_pipeline = HeadlessDictationPipeline()
                    log.info("Headless dictation pipeline created")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to create headless dictation pipeline: {}", exc)
                    _headless_pipeline = None
    return _headless_pipeline


def reset_headless_pipeline() -> None:
    """Reset the headless dictation pipeline (useful for config changes)."""
    global _headless_pipeline
    with _headless_lock:
        _headless_pipeline = None
