# The BloodBankCall voice agent

## What it does

The agent phones one blood bank and gets the answer to a single question: is the requested blood group
and component in stock *right now*? It asks one question per turn:

1. Do you have `{{blood_group_spoken}}`, that is `{{blood_group_confirm}}`, `{{component}}`? We need `{{units}}` units.
2. How many units do you have?
3. Is a replacement donor required? What is the processing fee per unit?
4. Can you keep them aside for one hour?
5. The staff member's name.
6. A read-back starting with `Confirming:` that says the group both ways. The transcript checker
   (`app/parsing.py: readback_confirmed`) only trusts an agent line containing "confirm" plus the group,
   followed by a "yes" from staff.
7. It tells staff the family will bring the requisition form and sample, thanks them, and hangs up.

The prompt lives in `agent/blood_bank_caller_prompt.md`. The orchestrator fills its variables on every
`POST /call` through `user_data`: `facility_name`, `phone_label`, `blood_group_spoken`,
`blood_group_confirm`, `component`, `units`, `hospital`, `area` and `language`.

The prompt also covers the things that go wrong on a real blood bank line:

- Hold music, transfers and "ek minute": the agent says "Sure, I'll wait" once, stays silent, then picks
  up at the step it was on.
- "Which hospital?": it answers with `{{hospital}}` in `{{area}}` and goes back to its last question.
  It never shares the patient's name or condition.
- It never says a group as a bare letter ("O"), because a letter alone is easy to mishear on a noisy line.
  It always says both forms, and in Hindi: "O negative, matlab O minus".
- Time: it aims to finish within 2 minutes. If it runs over, it skips straight to the read-back.
  Bolna cuts the line at 150 s (`call_terminate`) whatever happens.
- Recorded menus: the agent cannot press keys (see Known limits), so it says "Blood bank, please" once
  and waits.

## Configuration

`scripts/create_agent.py` builds the agent. These commands are free; only calls cost money.

| Piece | `en` profile (default, validated on 2 live calls) | `hi` / `kn` profiles (not yet tested on a live call) |
| - | - | - |
| STT | Deepgram `nova-3`, `language: en`, `endpointing: 250`, linear16 16 kHz | Sarvam `saaras:v4`, `language: hi` / `kn`, `endpointing: 500`, plus a Sarvam `context` describing the call |
| LLM | OpenAI `gpt-5.4-mini`, `max_tokens: 120` | same |
| TTS | ElevenLabs `eleven_turbo_v2_5`, voice Angelica (`IkSv4tkouLJ6kYsQA7XD`) | Sarvam `bulbul:v4-flash` (or `--tts-model bulbul:v3`), a voice you choose |
| Telephony | Plivo | Plivo |
| Limits | `call_terminate: 150`, `hangup_after_silence: 15` | same |

**Keyword boosting.** Every profile sets transcriber `keywords` to a comma-separated list of blood bank
words: positive, negative, plus, minus, O, A, B, AB, the eight full group names, PRBC, packed cells,
packed red cells, whole blood, platelets, plasma, FFP, units, replacement, donor, crossmatch,
requisition and processing fee. That is 30 terms. Deepgram `nova-3` sends them as keyterms. Sarvam reads
them only on `saaras:v4`, with a limit of 50 terms of up to 64 characters each, and a test enforces that
limit. Bolna strips `term:weight` suffixes, so the list has no weights. Pass `--no-keywords` to get back
the exact config that ran on the live calls.

**Noise cancellation.** `--noise-cancellation 60..100` sets `task_config.noise_cancellation_level`.
It is off by default because it has not been tried on a live call yet.

Useful commands:

```
.venv\Scripts\python scripts\create_agent.py --dry-run                 # print the config, send nothing
.venv\Scripts\python scripts\create_agent.py                           # create the English agent
.venv\Scripts\python scripts\create_agent.py --update                  # push only the prompt + welcome message (PATCH)
.venv\Scripts\python scripts\create_agent.py --replace                 # push the whole config, keywords included (PUT)
.venv\Scripts\python scripts\check_account.py                          # wallet and concurrency
```

An agent created before this change has no keywords. `--update` does not add them, because PATCH only
covers the prompt and welcome message. Run `--replace` once to add them.

## Switching languages

1. List the voices Bolna offers for the language. This sends GET requests only:
   ```
   .venv\Scripts\python scripts\create_agent.py --list-voices --language hi --provider sarvam
   ```
   The script calls `GET /api/v1/voice-config/tts?language=hi`, then
   `GET /api/v1/voice-config/tts/voices` for each supported provider and model. For Hindi, prefer a
   code-mixed `_enhi_` voice. The agent keeps "Confirming", the group and the component in English
   letters, and on `bulbul:v4-flash` the script of the text decides how it is pronounced.
   v4-flash and v3 voice ids are not interchangeable.
2. Create a separate agent for the language:
   ```
   .venv\Scripts\python scripts\create_agent.py --profile hi --voice-id <voice_id> --voice-name "<voice>"
   ```
   The voice is never guessed. Without `--voice-id` and `--voice-name` the script stops and tells you
   to run `--list-voices`. Add `--dry-run` first to check the body.
3. Put the new `agent_id` in `.env` as `BOLNA_AGENT_ID`. Each profile is its own agent; switch by
   changing that id.
4. Set the search's `language` field, for example "Hindi", so the prompt's "Speak {{language}}" matches.

The Hindi and Kannada welcome messages are written in native script. Have a native speaker check the
Kannada one before a demo.

## Retries

Retries are off by default. The orchestrator handles a no-answer by moving on to the facility's next
number, which is faster than any carrier retry. Bolna's auto-retry
(<https://www.bolna.ai/docs/outbound/auto-retry>) is available as an opt-in:

```python
from app.providers.bolna import BolnaProvider, retry_config
provider = BolnaProvider(settings, retry=retry_config(max_retries=1, intervals_minutes=[1]))
```

This adds `retry_config` to `POST /call`: `enabled`, `max_retries` (1 to 3), `retry_on_statuses`
(no-answer and busy), `retry_intervals_minutes`, `retry_on_voicemail: false` and
`prioritize_retries: true`. While Bolna still has retries left, the provider reports a no-answer or
busy status as `retrying`, which is not terminal, so polling continues. Retry intervals are whole
minutes. Keep them shorter than `call_timeout_s` (300 s), or the orchestrator times the call out
before the retry rings. Every retried attempt is billed. For a single test call:
`scripts/test_call.py +91... O- 2 --retry 1`.

## Webhooks

`/webhooks/bolna` feeds `BolnaProvider.ingest_webhook`. Bolna posts the same shape as
`GET /executions/{id}` on every status change: scheduled, queued, initiated, ringing, in-progress,
call-disconnected, then completed. The provider handles these variants:

- The id may arrive as `id`, as `execution_id`, or wrapped in `data`.
- Events are merged per execution, and empty fields never erase known data. This matters because
  transcripts are often empty before `completed`.
- A late non-final event never downgrades a final status. The exception is a Bolna auto-retry, which
  shows up as a higher `retry_count`.
- Once a final status is cached, `status()` answers from the cache. Before that it still polls
  `GET /executions/{id}` and merges the result with the cache.

Bolna sends webhooks from 13.203.39.153, 13.126.9.249 and 13.202.133.53. The endpoint must be public,
for example through a tunnel. Polling works without webhooks.

## Costs

The account is a trial with $5 credit, concurrency 2, and calls only to verified numbers.

The two live test calls lasted 51 to 61 s and cost 6 to 9 cents each, about 7 to 9 cents per minute.
The 150 s cap limits a single call to roughly 20 cents. `max_live_calls_per_search` (default 4, fallback
numbers included) limits one search to about 80 cents worst case, and typically 25 to 35 cents. Agent
create/update, `--list-voices` and `check_account.py` are free. Sarvam STT/TTS pricing may differ from
Deepgram/ElevenLabs; measure it on the first Hindi test call.

## Known limits

- **The agent cannot dial through an IVR.** Bolna's DTMF support (`task_config.dtmf_enabled`,
  <https://www.bolna.ai/docs/inbound/dtmf>) only *receives* keypad digits from the person on the call.
  Bolna delivers them to the LLM as `dtmf_number: <digits>`, on Plivo, Twilio and SIP trunks only.
  Bolna's IVR feature routes *inbound* calls. As of October 2026 the docs list no way for an outbound
  agent to *send* tones, so "press 3 for blood bank" menus are a dead end. The prompt asks for the blood
  bank by voice, which works on menus that accept speech. Otherwise, store a direct blood bank number in
  `data/facilities.json`.
- **Hindi and Kannada transcripts come back in native script.** Sarvam runs in `transcribe` mode, so a
  staff reply such as "ओ नेगेटिव, दो यूनिट" arrives in Devanagari. Today `app/parsing.py` matches only
  Latin text such as "O negative", "2 units" or "haan". The read-back check still works, because the agent's
  own line keeps the group in English letters, but the staff side (units, fee, the "yes" reply) needs
  native-script patterns in the parser before the hi/kn profiles can be trusted.
- The hi and kn profiles have not been tested on a live call. The English profile has.
- Voicemail detection (`task_config.voicemail`) is not enabled. The provider maps
  `answered_by_voice_mail` to the `voicemail` status after the call.
- Concurrency is 2 on the trial account, so at most 2 blood banks are called at a time.
