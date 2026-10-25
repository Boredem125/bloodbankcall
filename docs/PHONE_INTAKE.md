# Phone intake: the family calls one number

At 2 a.m. nobody opens a dashboard. The family dials one number, says what they need, hangs up,
and gets a call back with where to go, plus an SMS they can forward.

```
family dials BOLNA_INBOUND_NUMBER
  ─▶ intake agent (agent/intake_prompt.md): group said two ways + read-back, component, units, area,
     hospital, callback number. Under two minutes, then it hangs up.
  ─▶ Bolna webhook (status "completed") ─▶ POST /webhooks/bolna?token=...
       extracted_data (Bolna extractions) first, transcript parsing as fallback
       no clear blood group ─▶ rejected: the family is asked to call again; we never guess a group
  ─▶ Search(source="phone") ─▶ the normal pipeline: waves, safety gate, ranking, split plan
  ─▶ SMS with the go-pack text (optional: Plivo or Twilio)
  ─▶ callback agent (agent/callback_prompt.md) rings the caller and reads spoken_plan():
     "Go to Sanjeevani in HSR Layout, about 5 minutes away. They confirmed 4 units at 11:23.
      Ask for Ramesh. Kept aside for one hour. Their number is 9 8 7 6 5, 4 3 2 1 0 ..."
```

Everything the callback says comes from `app/intake.py: spoken_plan()`. The agent prompt forbids
adding anything that is not in that text. A failed search gets an honest callback: nothing
confirmed, here are unconfirmed leads to call yourself, ask the doctor about compatible groups.

The dashboard shows an **Incoming calls** panel: every phoned-in request appears as it arrives
(caller masked to the last 4 digits), with search status, callback status and SMS status. Clicking
one shows the live search. A new phone request is shown automatically unless an operator is
watching a search that is still running.

## Try it free (no Bolna number)

```
.venv\Scripts\python -m uvicorn app.main:app --port 8000
.venv\Scripts\python scripts\sim_inbound.py O- 2 "HSR Layout"
```

`POST /intake/simulate` fakes the finished intake call; the search and the callback run through the
free simulator (`family_callback` persona in `data/sim_scripts.json`). It is disabled when
`LIVE_MODE=true`, because there it would place a real callback.

## Go live

Costs: creating agents and extractions is free. Each intake call, blood bank call and callback is
billed per minute. An Indian number on Bolna needs the compliance application; a US number works
for a demo where the judge dials it (international call from an Indian mobile), or run the intake
through the Bolna dashboard's test call.

1. Expose the app: `ngrok http 8000` (or `cloudflared tunnel --url http://localhost:8000`). Set
   `WEBHOOK_TOKEN` in `.env` to any secret.
2. Intake agent + extractions (one category "Blood request", one extraction per field):
   ```
   .venv\Scripts\python scripts\create_intake_agent.py --role intake --webhook https://<tunnel>/webhooks/bolna?token=<WEBHOOK_TOKEN>
   ```
   Put the printed `BOLNA_INTAKE_AGENT_ID` in `.env`.
3. Callback agent:
   ```
   .venv\Scripts\python scripts\create_intake_agent.py --role callback
   ```
   Put `BOLNA_CALLBACK_AGENT_ID` in `.env`.
4. Number: buy one in the Bolna dashboard (Phone numbers; country IN or US), then
   ```
   .venv\Scripts\python scripts\create_intake_agent.py --list-numbers
   .venv\Scripts\python scripts\create_intake_agent.py --link-number auto
   ```
   Put `BOLNA_INBOUND_NUMBER` in `.env` so the dashboard shows it.
5. Also point the blood bank caller agent at the same webhook so it polls less:
   `scripts\create_agent.py --replace --webhook https://<tunnel>/webhooks/bolna?token=...`
6. Optional SMS: `SMS_PROVIDER=plivo` with `SMS_AUTH_ID`, `SMS_AUTH_TOKEN`, `SMS_FROM`
   (or `twilio`). Without it the SMS text is logged and the dashboard says "skipped".
7. Restart the app with `LIVE_MODE=true`. Call the number.

Trial accounts can only call verified numbers: the callback goes to the caller, so call in from a
verified phone. `MAX_LIVE_CALLS_PER_SEARCH` caps the blood bank calls; the callback is one more.

Hindi/Kannada: `--profile hi|kn --voice-id ... --voice-name ...` exactly as in `docs/AGENT.md`.
The intake agent switches language when the caller does, and the `language` extraction carries it
into the search so the blood bank caller speaks the same language.

## What the webhook needs from Bolna

| Field | Used for |
| - | - |
| `status == "completed"` | only finished calls count; `call-disconnected` fires first with partial data |
| `agent_id == BOLNA_INTAKE_AGENT_ID` | tells intake calls from blood bank calls (fallback: `telephony_data.call_type == "inbound"`) |
| `user_number`, `telephony_data.from_number` | who to call back |
| `extracted_data["Blood request"][name].objective/subjective` | `blood_group`, `component`, `units`, `area`, `hospital`, `callback_number`, `language` |
| `transcript` | fallback parsing with the same group parser the blood bank calls use |
| `id` | dedupe: Bolna re-sends events |

Bolna sends webhooks from `13.203.39.153`, `13.126.9.249` and `13.202.133.53`.
