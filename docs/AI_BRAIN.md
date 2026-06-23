# The AI brain

The brain is the agent loop that turns a perception of the robot into actions, using any OpenAI-compatible
vision model. It lives in `autobot/brain/`.

## Loop

The brain is **event-driven**, not a rigid timer. Background tasks keep a live `PerceptionBuffer` (latest
frame, scene caption, telemetry, recent speech) fresh, and a priority event queue feeds a single reasoner:
**speech/manual** (priority 0) preempt **state/touch** (1), which preempt autonomous **idle** wandering (2,
posted every `config.tick_seconds`). Each event runs one `perceive → think → act → observe` cycle.

```
Observation (telemetry + JPEG)  ─►  build messages  ─►  POST /chat/completions (tools, image)
        ▲                                                         │
        │                                                         ▼
   RobotLink  ◄── safety.py ◄── tool calls + assistant reasoning  ─►  WebSocket → UI thought stream
```

## Brain architectures (`AUTOBOT_AI_PROVIDER`)

There are three ways the brain can be wired:

- **single model** (`openai|openrouter|ollama|...`): one OpenAI-compatible **vision** model both sees the
  frame and decides via tool calling. Simplest; needs a model that can see images.
- **vlm** (`AUTOBOT_AI_PROVIDER=vlm`): a local vision model (`scripts/vlm_service.py`) both sees AND picks a
  move (`forward|left|right|back|stop`). Fast and fully local, but it **bypasses tools/memory/skills** — no
  curated memory, no places, no Home Assistant, no MCP.
- **hybrid — REFLEX + CORTEX** (`AUTOBOT_AI_PROVIDER=hybrid`, recommended): the two tiers are split.
  - **Perception tier (the eyes):** the VLM service runs continuously in the background
    (`AUTOBOT_CAPTION_SECONDS`), turning each frame into a concise `SCENE/OBJECTS/PEOPLE/PATHS` description
    (`POST /vlm/perceive`). It never decides a move.
  - **Cortex tier (the thinking):** `AUTOBOT_AI_MODEL` (an OpenAI-compatible model, e.g. `qwen2.5:7b` via
    Ollama) runs the full tool-calling loop — memory, talking, curiosity, places, Home Assistant, MCP. It
    reads the VLM's description as its sight (text-only, so the cortex can be a fast non-vision model).
  - **Reflex layer (no LLM):** a fast watcher stops the robot the instant the ToF/IR sensor reports an
    obstacle closer than `AUTOBOT_REFLEX_STOP_CM`, then arms a "turn, don't push forward" hint for the next
    cortex decision. Still routed through `safety.py`; complements the native deadman watchdog.

```
camera/mic ─► VLM /vlm/perceive (eyes) ─┐
telemetry (battery, ToF, IMU, pose) ────┤─► CORTEX (tool-calling LLM) ─► safety.py ─► RobotLink
memory + curiosity + spatial coverage ──┘        ▲ ToF reflex (non-LLM stop/turn) ──┘
```

## Provider configuration (provider-agnostic)

Set in the UI or `.env`:

- `AUTOBOT_AI_BASE_URL` — e.g. `https://api.openai.com/v1`, `http://localhost:11434/v1` (Ollama),
  `http://localhost:1234/v1` (LM Studio), OpenRouter, a Gemini OpenAI-compat shim, etc.
- `AUTOBOT_AI_API_KEY` — provider key (blank for most local servers).
- `AUTOBOT_AI_MODEL` — e.g. `gpt-4o`, `llama3.2-vision`, `qwen2.5-vl`, ...

The client (`providers/openai_compatible.py`) only assumes Chat Completions with `tools`/`tool_choice` and
image content parts. If the endpoint can't do vision, the brain still runs on telemetry text and says so in
the UI. If it can't do tool calling, the brain falls back to parsing a strict JSON action from the text.

## Skills + tools (the action contract — `autobot/brain/skills/`)

Capabilities are `Skill`s composed by the `SkillRegistry`. Each tool has an **authority** (`anyone` or
`owner`); `owner` tools are gated by the obedience policy (see Identity below) before the handler's own
safety clamps run. Adding a capability = add/extend a skill + its authority + a `safety.py` rule (if it
moves anything) + a row below.

| skill | tool | args | authority / safety |
|-------|------|------|--------------------|
| core | `drive` | `direction` or `{ly,rx}`, `speed`, `duration` | owner; clamped to `max_speed`, duration capped |
| core | `stop` | — | anyone; always allowed |
| core | `look` | — | anyone; fresh snapshot next turn |
| core | `say` | `text` | anyone; dropped unless `talk_enabled` |
| core | `set_eyes` | `animation` | anyone |
| core | `set_toggle` | `feature`∈{night,avoid,fall,patrol,eyes}, `on` | owner |
| core | `dock`/`undock`/`wake`/`sleep` | — | owner |
| core | `wait` | `seconds` | anyone; capped |
| memory | `remember`/`recall`/`forget` | `text`/`query` | anyone |
| recognition | `note_sighting` | `label`,`kind`,`detail` | anyone |
| recognition | `who_do_you_see` | — | anyone |
| recognition | `enroll_face` | `name` | owner (pairing) — only if `face_recognition` installed |
| home_assistant | `list_entities` | `domain?`,`search?` | anyone — only if HASS configured |
| home_assistant | `get_state` | `entity_id` | anyone — only if HASS configured |
| home_assistant | `home_assistant` | `entity_id`,`action`,`data?` | owner — on/off/toggle (+ brightness/color) |
| home_assistant | `ha_service` | `domain`,`service`,`entity_id?`,`data?` | owner — any service (scenes/scripts/climate) |
| mcp | `mcp_<server>_<tool>` | (per MCP tool) | per-server (`authority`); only if `AUTOBOT_MCP_SERVERS` + `mcp` installed |
| places | `save_place`/`go_to_place`/`where_am_i`/`remember_thing`/`where_is`/`list_places` | varies | go_to_place takes safety-clamped steps; owner for save/goto |

## Memory (`memory.py`)

Persistent across restarts under `AUTOBOT_MEMORY_DIR` (default `data/memory/`): long-term `facts.json`
(written atomically), append-only `daily/<date>.jsonl`, and `sightings.jsonl` (both roll over to a `.1`
backup past `AUTOBOT_LOG_MAX_BYTES`). A compact summary is injected into the system prompt each tick, and
`remember`/`recall`/`forget` let the model curate it. `recall` uses keyword scoring by default, or
**semantic embedding similarity** when `AUTOBOT_EMBED_MODEL` is set (OpenAI-compatible `/embeddings`, e.g.
`nomic-embed-text` on Ollama; fail-soft back to keyword). The daily summarizer distills `facts.json` with
the heavy model and prunes daily notes older than `AUTOBOT_DAILY_KEEP_DAYS`. The UI **Memory** tab browses
facts/sightings/notes and can distill, forget, or wipe. On the `vlm` brain path, auto-remembered scene
observations are novelty-gated (via the curiosity signal) so a slowly changing view doesn't spam memory.

## Persona, name, and the addressing gate

`config.py` holds `robot_name`, `persona`, `owner_name`, and two toggles. The name + persona go into the
system prompt. `require_name`: when on, spoken audio is only acted on if it contains the robot's name (UI
chat is always treated as directly addressed). This is how it "only responds to its name".

## Identity + owner authority (`identity.py`)

`obey_owner_only`: when on, `owner`-authority tools only run if the owner is present (recognized by the
recognition skill), the dashboard owner, or within a live approval window. Otherwise the robot "asks its
maker" — it raises a pending approval the owner resolves via `/api/approve` (UI buttons), which opens a
short window. Pairing = `enroll_face` the owner so the robot recognizes them on sight. With no face
recognizer running, the dashboard is trusted as the owner (single-user dev stays usable).

## Voice in (`skills/voice.py`) + recognition (`skills/recognition.py`)

Optional, graceful. Voice taps the robot mic, transcribes ~2.5s windows with `faster-whisper`/`whisper`
(if installed), and writes the transcript to `ctx.heard` → the agent surfaces addressed speech to the
model. Recognition runs face detection/matching on each frame (if `face_recognition` is installed),
updating who's present and logging sightings to memory; without it, `note_sighting` still lets the vision
model remember what it sees.

## System prompt

`agent.py` builds a system prompt that:

- explains the robot (two-wheel EBO, what it can sense/do),
- states the safety rules the model should respect (it is also enforced mechanically),
- instructs the model to *think out loud briefly* then call tools,
- gives the current goal (user-set in the UI, e.g. "explore the room and describe what you see").

## Behavior — when it roams vs observes (`behavior.py`)

The brain does NOT drive every tick. A `BehaviorController` picks a movement **scope** each cycle that the
safety floor hard-enforces: `roam` (drive freely), `adjust` (rotate in place only — no translation), or
`hold` (no AI motion). The default is OBSERVE (`adjust`): it stays put, looks around, and comments. Roaming
only unlocks for a reason:

- it sees a person -> GREET (approach + greet by name),
- it's been idle `AUTOBOT_IDLE_PATROL_SECONDS` -> PATROL (a short look around for anything noteworthy — open
  doors/windows, messes, people/pets — then settle),
- `mode == command` with a directive -> PURSUE, `mode == conversational` -> CONVERSE (adjust),
- a voice order set an override (explore / come here / go home / stop).

`mode == explore` is the "alive at home" companion mode that runs this whole state machine (it does NOT mean
drive constantly). The chosen intent is injected into the prompt as a "RIGHT NOW" line, and `safety.set_scope`
makes it mechanical. Knobs: `AUTOBOT_IDLE_PATROL_SECONDS`, `AUTOBOT_PATROL_SECONDS`, `AUTOBOT_GREET_SECONDS`.

## Voice commands — always respected + adaptive (`commands.py`)

A fast keyword/phrase matcher catches the critical spoken orders instantly (preempting wandering), while the
cortex LLM handles paraphrases via the `behavior` skill tools (`set_mode`, `stay`, `come_here`, `be_quiet`):

- STOP / QUIET / SLEEP / SPEAK_UP / BACK_UP — always honored (even from a non-owner).
- GO EXPLORE / GO HOME (dock) / COME HERE — owner-gated when `obey_owner_only` is on.

Matched orders post a high-priority `command` event handled by `agent._apply_command`; STOP also fires an
immediate stop. STOP/QUIET use a hold scope / `safety.set_quiet`; SLEEP triggers go-dark.

## Curiosity & spatial coverage (`curiosity.py`)

So the robot doesn't roam in circles or narrate the same wall, a curiosity signal tracks **scene novelty**
(how different the current VLM scene description is from recent ones) and **action repetition**, plus a coarse
**visited-cell grid** fed by the VSLAM pose. When it's "bored" (the view stops changing) or repeating a move,
it injects a nudge into the cortex prompt ("go somewhere new — less-explored space is to your left"). It also
novelty-gates the `vlm` path's auto-remember so a slowly changing view doesn't spam memory.

## Tasks / scheduling (`tasks.py` + `tasks` skill)

The robot can schedule things for itself: one-shot reminders (`in_seconds`), daily routines (`daily_time`
"HH:MM"), or repeats (`every_seconds`), stored in `data/tasks.json`. The brain's `_scheduler_loop` fires due
tasks by injecting their text as a high-priority directive (`feed_task`), so the robot reasons and acts on
them through the normal safety floor. Manage via the `add_task`/`list_tasks`/`cancel_task` tools or the UI
Tasks panel (`/api/tasks*`).

## Autonomy modes

- `manual` — loop paused; only UI manual controls move the robot.
- `assist` — loop runs on triggers (speech/step/state), no autonomous idle wandering; actions execute.
- `auto` — full autonomy: idle-wanders every `tick_seconds` plus all triggers.

The user sets autonomy, goal, `max_speed`, and `talk_enabled` in the UI. The **AI cannot change these**.

**Go dark (Sleep):** `POST /api/sleep` is a single kill switch — it stops the robot, sets `asleep=true` +
`autonomy=manual`, and pauses the link (`connection("stop")`: release control + drop inbound media to the
hub, session kept warm). While dark, all brain loops idle, `feed_speech`/`tick`/`chat` and manual
drive/say are refused, and STT/captioner/SLAM get no frames. Wake (`/api/sleep {on:false}`) reverses it and
restores the prior autonomy. See docs/SAFETY.md.

## Hardware-free development

Set `AUTOBOT_ROBOT_LINK=mock` and run `python -m autobot`. The app uses `MockRobotLink` (telemetry + a
generated snapshot + accepts all control calls and logs them) instead of the native TUTK link, so you can
exercise the full loop, UI, and safety floor with no robot and no robot secrets. Useful for prompt
iteration and UI work.
