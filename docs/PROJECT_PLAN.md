# Dodaios — Working Plan and Milestones

Status: **active.** This is the single place to see what is being built, what is
done, and what an agent should pick up next. It is written so that a fresh agent
(or a person returning after a crash) can resume without reading the chat
history.

Last updated: **2026-11-21** (voice/TTS + FreeLLMApi + launcher wave).

How to keep this current: load the `project-roadmap` skill
(`.agents/skills/project-roadmap/SKILL.md`). At the end of any milestone-sized
change, update the **Snapshot**, the milestone row, and **Next actions**, then
append to **Decision log** if a direction changed. Small fixes do not need a
plan edit; anything that changes a milestone's scope does.

---

## 1. Resume protocol (read this first)

1. `git status --short` and `git log --oneline -5` — find uncommitted work.
   Never discard a dirty tree to "start clean".
2. Read **§2 Snapshot** and **§5 Next actions**. Do the first action that is not
   blocked; do not wait for a full re-plan.
3. Run the focused checks named in the milestone, not the whole suite. The
   repository's own contract is `docs/agent-done-gate.md`
   (`python scripts/agent_done_gate.py`); a turn is only done when that gate
   says `DONE`.
4. If a process was left running, the launcher knows the truth:
   `python3 tools/dodaios_launcher.py --status`.
5. If the server tree and the running app disagree, restart through the
   launcher, never by spawning a second server on the same port.

A task is **done** only when: the focused tests pass, the live artifact was
checked (API response, real turn, or built bundle), and this file reflects the
new state.

---

## 2. Snapshot

| Area | State |
| --- | --- |
| Branch | `main` (working tree has the current wave; commit before switching) |
| Server | `tools/dodaios_launcher.py` → `127.0.0.1:47821`, managed by the launcher |
| Frontend | built into `dodaios/ui/web/dist`; `dist/` is not tracked |
| Brain default | `brain.primary = "gemini"` (key currently rejected — see §4) |
| Working chat brain | FreeLLMApi (`http://127.0.0.1:3001`, model `claude-haiku-4-5`) |
| Wake word | `Hakuna matata`, engine resolved to local `stt_match` (phrase is OOV for Vosk) |
| Dictation STT | Groq `whisper-large-v3-turbo` (working) |
| Spoken replies | Kokoro local TTS landed; browser autofill fixed; brain switched to FLA locally |
| FreeLLMApi | running as `node server/dist/index.js` from `~/.local/share/freellmapi-app` |

---

## 3. Milestones

Status legend: **done** · **in progress** · **next** · **blocked** · **design only**

| # | Milestone | Status | Outcome / acceptance |
| --- | --- | --- | --- |
| M0 | Provider and key health | **done** | Every configured provider tested live; bad keys classified as `bad_key`, not "unreachable" |
| M1 | Wake word on Linux | **done** | "Hakuna matata" wakes the assistant; local transcript match; no cloud upload of wake audio |
| M2 | FreeLLMApi as a first-class provider | **in progress** | FLA selectable in home chat and IDE; real turn succeeds; picker shows a distinct FLA section |
| M3 | Live voice replies + natural local TTS | **in progress** | Kokoro plugin + bundle registered; autofill fix; live turn still to verify |
| M4 | Launcher owns the FLA lifecycle | **done** | One Start brings up Dodaios and (optionally) FLA; Stop stops FLA; `--status` reports both |
| M5 | Web-chat bridge (ChatGPT/Gemini web) | **blocked** | Design only until the compliance decision in §6 is made |
| M6 | Multi-agent orchestrator with a reviewing lead | **design only** | Design in `docs/plans/agent-orchestrator.md` |

---

## 4. Milestone detail

### M0 — Provider health (done)

Live checks through `POST /api/providers/{id}/test`. Results at the time of the
wave:

- **Working:** Groq (dictation + polish), NVIDIA (after replacing EOL model ids),
  Codex via the ChatGPT subscription, FreeLLMApi.
- **Rejected:** Gemini API key (HTTP 400 `INVALID_ARGUMENT`, "API key not
  valid"), xAI/Grok (401 "Incorrect API key provided").
- **No balance:** DeepSeek (402).
- **Not configured/running:** Ollama, generic local OpenAI slot, Piper runtime.

Files: `dodaios/brain/provider_test.py`, `dodaios/agent_runtimes/provider_errors.py`,
`dodaios/ui/web/provider_spec.py`, `dodaios/plugins/brain/nvidia.py`.

Acceptance: invalid-key responses classify as `bad_key` in both the manager and
the settings test route; the user sees "key rejected", not "connection failed".

### M1 — Wake word (done)

The legacy `engine = "openwakeword"` mapped to the small Vosk English model, and
"Hakuna matata" is out of its vocabulary. `resolve_wake_plan` now probes the
lexicon (`vosk_model_can_speak`) and falls back to the local-Whisper transcript
matcher (`stt_match`) with `message_code = "vosk_out_of_vocabulary"`. The
headless pipeline passes the **local wake Whisper** as `stt`, so wake audio is
never uploaded.

Files: `dodaios/speech/wake_phrase.py`, `dodaios/speech/headless_voice.py`,
`dodaios/ui/web/settings_routes.py`, wake strings in the five locales.

Acceptance: self-test reports ready with the OOV explanation; log shows
`Wake plan: engine=stt_match` and `Wake transcript matcher: local Whisper built`;
a real wake→turn works. If wake seems deaf, check mic capture first (the
watchdog logs restarts).

### M2 — FreeLLMApi provider (in progress)

Goal: FLA is a provider you can pick for home chat and the IDE, with no key
hunting and no "auto" model surprises.

Done:

- Provider id `freellmapi`, plugin `dodaios/plugins/brain/freellmapi.py`
  (`FreeLLMAPIBrain`, OpenAI-compatible, base `http://127.0.0.1:3001`).
- Key stored as `freellmapi_api_key` / `FREELLMAPI_API_KEY`; a missing key is
  reported as "no key", not "server unreachable".
- Registered in manager, wizard, config, provider spec, model catalog, the API
  worker map and the chat catalog. 1,494 live models; `claude-haiku-4-5` pinned
  as the picker default.
- Real home-chat turn through FLA succeeded (`"route ok"`, `turn_finished: done`).

Picker bug found live (2026-11-21): FLA shared `family="local"` with the generic
local slot. `ComposerBrainPicker` collapses a family to one access choice
(Local / API key / Subscription), so clicking the FLA rail resolved to "Local
OpenAI-compatible" and FLA's models never appeared. Fix: FLA has its own family,
`family="freellmapi"` (`dodaios/agent_chat/catalog.py`).

Remaining acceptance: rebuild `dist` (`npm run build` in
`dodaios/ui/web/frontend`), open home chat → model picker → **FreeLLMApi**, see
its model list, send a turn. The bundle check matters: AGENTS.md says a frontend
change is only done when the running app serves it.

### M3 — Live voice replies and a natural local voice (in progress)

Symptom: after wake, the person's own words appear but no spoken answer. The
log shows the reply being generated ("Yes, I can hear you") and then
`Gemini TTS error (ClientError)` / `returned no audio — Audio stays silent`.
So this is TTS, not the brain.

Decision: local speech by default. Device: GTX 1650 4 GB, 8 CPU cores, 15 GB
RAM. `sherpa-onnx` is the existing local runtime and supports Kokoro, which is
markedly more natural than Piper's small voices. Model:
`kokoro-int8-multi-lang-v1_1` (~170 MB, int8, multiple languages) under
`data/models/`. A new provider `kokoro-local` reuses the local-model install
route, so the card can download it from inside the app.

Design:

- `dodaios/plugins/tts/kokoro_local.py` — `sherpa_onnx.OfflineTts` with
  `OfflineTtsKokoroModelConfig`; voice by language + gender profile
  (`af_heart`/`am_michael` family for English), 24 kHz PCM.
- Add the bundle to `SHERPA_BUNDLES` and a `LOCAL_PROVIDERS` entry
  (`runtime="sherpa-onnx"`, `tier="tts"`, download size ~170 MB).
- Register entry point `kokoro-local` in `pyproject.toml` and the provider
  branch in `dodaios/plugins/tts/__init__.py`; add a `ProviderSpec` with
  `auth_mode="none"` so privacy/airgapped logic treats it as local.
- Set `[tts] provider = "kokoro-local"` only after a synthesized WAV and a real
  live turn both work.

Fallback: if Kokoro latency is unacceptable on CPU, keep `piper-local` (faster
than real time, less natural) and revisit GPU onnxruntime. Kokoro measured so
far: ~2.5 s load, ~9 s for 5.5 s of speech on CPU; int8 should improve this.

Acceptance: a wake turn produces audio locally (no key), the log has no Gemini
TTS error, and a non-English reply either uses a matching Kokoro language or
logs the accent fallback explicitly.

### M4 — Launcher manages FreeLLMApi (done)

`tools/dodaios_launcher.py` now has a FreeLLMApi card:

- Detects the router at `FREELMAPI_HOME` (default
  `~/.local/share/freellmapi-app`), starts `node server/dist/index.js` detached
  with its own log (`~/.local/state/dodaios-launcher/freellmapi.log`), and
  health-checks `http://127.0.0.1:3001/`.
- Start/Stop/Open buttons; Stop refuses to signal a process that is not FLA and
  only uses `killpg` when the process leads its own group.
- "Start FreeLLMApi with Dodaios" option (persisted as `auto_start_fla`), fired
  only after `/api/health` says Dodaios is ready.
- `--status` reports both states as JSON.

Acceptance: with FLA stopped, Start in the launcher brings it up and a Dodaios
provider test for `freellmapi` turns green; Stop leaves port 3001 free. Verify
the GUI on a real display (this repo's launcher is GTK4).

### M5 — Web-chat bridge (blocked, design only)

Goal: use the person's logged-in ChatGPT/Gemini **web** sessions for small
interactions and as a live-conversation option, without paying per token.

Hard constraint: OpenAI and Google terms prohibit automated/scraped access to
their consumer web UIs outside the official APIs. A headless-browser bridge that
sends messages and reads replies to avoid API limits is a terms violation, not
an engineering detail, and can get the person's account restricted. This
milestone therefore does **not** ship an unattended scraper. The design and the
compliant options are in `docs/plans/web-chat-bridge.md`; the decision needed
from the user is recorded in §6. The working compliant path today is the Codex
seat (ChatGPT subscription) already present in the catalog.

### M6 — Multi-agent orchestrator (design only)

Goal: several terminal agents (Freebuff, Codex, Antigravity, Claude Code, plus
API/FLA seats) work on separate tasks at once; a lead agent decomposes, assigns,
reviews, and sends follow-ups.

The existing pieces are a good base: `agent_chat` sessions per seat,
`agent_runtimes` adapters, `missions` workers, the society roster, and the
approval cards. The design is in `docs/plans/agent-orchestrator.md`. First
increment is deliberately small: a lead that runs 2–4 parallel read-only
investigations and returns one merged review — no writes, no autonomous loops.

---

## 5. Next actions (ordered)

1. **Finish M2 verification:** rebuild the frontend bundle, reload the app, pick
   FreeLLMApi, confirm models and a real turn.
2. **Finish M3:** land `kokoro-local`, synthesize a WAV, switch `[tts] provider`,
   run a wake→spoken-reply turn, confirm the log is clean.
3. **Commit the wave** (see §7) once 1–2 pass.
4. **M4 GUI check** on the real desktop: start/stop FLA from the launcher.
5. **M5 decision:** choose compliant web-chat scope (see §6) before any code.
6. **M6 first increment:** design review of `docs/plans/agent-orchestrator.md`,
   then the read-only parallel investigation prototype.

---

## 6. Open decisions (need the person, not an agent)

- **Web-chat bridge:** automated web-UI access is a terms violation. Choose
  (a) official APIs/subscription seats only, (b) a user-driven browser helper
  that drafts but never sends, or (c) drop the milestone. Until then it stays
  blocked; do not implement a scraper.
- **Default brain:** Gemini's key is rejected. Should `brain.primary` move to
  FreeLLMApi (or Codex) by default, or stay Gemini until a new key is pasted?
- **TTS default:** local Kokoro only speaks the languages it ships; if German
  replies matter more than naturalness, keep Piper for German or accept the
  English accent fallback.

---

## 7. Engineering conventions for this plan

- Work on `main` only for small fixes; use a feature branch for a milestone wave
  (`feat/<short-name>`), commit at coherent points, and never rewrite history.
- `dist/` is generated and untracked; rebuild it, do not commit it.
- When the browser handshake times out, the call falls back to the classic
  desktop pipeline instead of failing with 'no reason reported'.
- Tests: run focused files. Known useful commands:
  - `python -m pytest -q tests/unit/agent_chat`
  - `python -m pytest -q tests/unit/brain/test_model_catalog.py tests/unit/test_tier_defaults.py`
  - `python -m pytest -q tests/missions/worker_runtime/test_provider_map.py`
  - `python -m pytest -q tests/unit/missions/test_api_agent_worker_providers.py`
- The done gate: `python scripts/agent_done_gate.py` must say `DONE`.
- Secrets never go in logs, docs, or commit messages. Provider keys live in the
  OS keyring; the FLA key is `freellmapi_api_key`.

---

## 8. Decision log

| Date | Decision | Why |
| --- | --- | --- |
| 2026-11-21 | OOV wake phrases fall through to local transcript match | Vosk's small English model cannot say "Hakuna matata"; cloud wake upload is unacceptable |
| 2026-11-21 | FreeLLMApi is its own provider id and its own picker family | Reuses the OpenAI-compatible brain; the shared "local" family hid it behind the generic local slot |
| 2026-11-21 | Local Kokoro is the preferred natural voice | Local, keyless, more natural than Piper; device has the CPU/RAM for it |
| 2026-11-21 | No unattended web-chat scraping | Consumer web UIs prohibit automated access; account risk outweighs the benefit |
| 2026-11-21 | The launcher owns FLA's lifecycle | One Start for both halves; the alternative is asking the person to run two apps |
