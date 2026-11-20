"""Voice stack for a headless server: wake word, hotkeys and dictation.

``--headless`` used to force ``JARVIS_VOICE=0``: no ``SpeechPipeline`` was ever
constructed, so neither the wake word nor the dictation shortcut could work on
a machine that runs the app as a server. Browser voice still worked, which
made the gap easy to miss.

This module builds the same pipeline the desktop app and the watchdog use, from
the same config, with no window attached. It is opt-in: the launcher starts it
only when ``[trigger].wake_word_enabled`` is true (or ``JARVIS_VOICE=1``), and
``--no-voice`` opts out. Every failure is contained — a missing microphone or a
keyless TTS degrades voice and leaves the web server running.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

from loguru import logger as log


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def resolve_plan(cfg: Any) -> Any:
    """The concrete wake-word plan for this configuration."""
    from dodaios.speech.wake_model_fetch import resolve_wake_language
    from dodaios.speech.wake_phrase import resolve_wake_plan

    return resolve_wake_plan(
        cfg.trigger.wake_word,
        local_whisper_available=_module_available("faster_whisper"),
        language=resolve_wake_language(cfg),
    )


def build_pipeline(
    *,
    cfg: Any,
    bus: Any,
    supervisor: Any,
    brain_getter: Any,
) -> Any:
    """Construct a ``SpeechPipeline`` for a host with no GUI."""
    from dodaios.plugins.stt import build_stt_from_config
    from dodaios.plugins.tts import build_tts_from_config
    from dodaios.speech.pipeline import SpeechPipeline

    plan = resolve_plan(cfg)
    if not plan.wake_available:
        log.warning(
            "Wake word unavailable for {!r}: {}", plan.phrase, plan.message
        )
    elif plan.degraded:
        log.warning("Wake word degraded: {}", plan.message)
    else:
        log.info(
            "Wake plan: engine={} phrase={!r} — {}",
            plan.engine,
            plan.phrase,
            plan.message,
        )

    # ``stt`` is the pipeline's LOCAL wake/preview recognizer; the utterance
    # recognizer is built separately from cfg.stt inside SpeechPipeline. On the
    # transcript-match wake path that local instance is mandatory: handed the
    # cloud STT instead, RollingWhisperWake uploads a chunk every couple of
    # seconds to a provider that only wanted post-wake audio (the 2026-11-20
    # Groq 429 storm). Build the same small CPU wake model the desktop uses, and
    # only arm the rolling detector when that build really produced a local one.
    local_wake_stt: Any = None
    if plan.engine == "stt_match":
        try:
            from dodaios.plugins.stt import build_wake_whisper

            stt_language = getattr(cfg.stt, "language", None)
            local_wake_stt = build_wake_whisper(
                cfg.stt,
                language=(
                    None if str(stt_language or "").lower() in ("", "auto") else stt_language
                ),
                wake_phrase=plan.phrase,
                fast_first=True,
            )
            log.info("Wake transcript matcher: local Whisper built for {!r}.", plan.phrase)
        except Exception as exc:  # noqa: BLE001 — wake degrades, dictation does not
            log.warning(
                "Local Whisper for the wake transcript match is unavailable ({}) — "
                "the wake word stays off rather than uploading mic audio.",
                exc,
            )
    stt = local_wake_stt or build_stt_from_config(cfg.stt)
    try:
        tts = build_tts_from_config(cfg.tts)
    except Exception as exc:  # noqa: BLE001 — a mute assistant still listens
        log.warning("TTS unavailable in headless voice: {}", exc)
        tts = None

    async def brain_callback(text: str) -> str:
        brain = brain_getter()
        if brain is None:
            return ""
        return str(await brain.generate(text) or "")

    call_hotkeys, ptt_hotkeys = cfg.trigger.resolve_hotkeys()

    def _hotkeys(value: str) -> tuple[str, ...]:
        text = str(value or "").strip()
        return (text,) if text else ()

    return SpeechPipeline(
        call_hotkeys=call_hotkeys,
        ptt_hotkeys=ptt_hotkeys,
        dictate_hotkeys=_hotkeys(cfg.trigger.hotkey_dictate),
        dictate_toggle_hotkeys=_hotkeys(cfg.trigger.hotkey_dictate_toggle),
        paste_last_hotkeys=_hotkeys(cfg.trigger.hotkey_paste_last),
        hangup_hotkeys=_hotkeys(cfg.trigger.hotkey_hangup),
        dictate_mode=cfg.dictation.mode,
        dictation_config=cfg.dictation,
        stt=stt,
        tts=tts,
        brain_callback=brain_callback,
        enable_openwakeword=_module_available("openwakeword"),
        # The rolling-Whisper transcript matcher is the detector for stt_match
        # only. Left on for a Vosk plan it ALSO listens and sends the rolling
        # buffer to the configured (cloud) STT every couple of seconds — a
        # continuous upload for a phrase Vosk already handles locally, and a
        # 429 storm on a free tier.
        enable_whisper_wake=(
            plan.engine == "stt_match"
            and local_wake_stt is not None
            and _module_available("faster_whisper")
        ),
        continue_listening_after_response=not cfg.trigger.single_turn_mode,
        idle_timeout_s=cfg.trigger.session_idle_timeout_s,
        bus=bus,
        supervisor=supervisor,
        config=cfg,
        input_device=cfg.audio.input_device or None,
        output_device=cfg.audio.output_device or None,
        wake_plan=plan,
    )


async def run_headless_voice(
    *,
    cfg: Any,
    bus: Any,
    supervisor: Any,
    brain_getter: Any,
    app: Any = None,
) -> None:
    """Build, register and run the pipeline until the process stops."""
    from dodaios.core import runtime_refs

    # The STT/TTS/wake providers load models and touch credentials; keep that
    # off the event loop so the web UI never freezes during "Voice starting".
    pipeline = await asyncio.to_thread(
        build_pipeline,
        cfg=cfg,
        bus=bus,
        supervisor=supervisor,
        brain_getter=brain_getter,
    )
    # Expose it before running, so a dictation request that arrives during the
    # warm-up uses the real pipeline instead of the headless stand-in.
    try:
        runtime_refs.set_speech_pipeline(pipeline)
    except Exception:  # noqa: BLE001 — never block voice on a ref write
        log.debug("could not register the headless speech pipeline", exc_info=True)
    if app is not None:
        try:
            app.state.speech_pipeline = pipeline
        except Exception:  # noqa: BLE001
            pass
    log.info("Headless voice pipeline starting (wake word + dictation).")
    try:
        await pipeline.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Headless voice pipeline died — voice offline until restart.")
    finally:
        try:
            runtime_refs.set_speech_pipeline(None)
        except Exception:  # noqa: BLE001
            pass
