# BloodBankCall

**At 2 a.m., a family needing O-negative blood calls blood bank after blood bank.** Lines go unanswered. Someone says "yes, O is there" and they drive across the city to hear "we meant O positive". BloodBankCall phones every nearby blood bank at once in English, Hindi or Kannada. It verifies each answer and tells the family where to go, but only when availability was actually confirmed.

> The LLM talks. The decision model decides. The gate decides what we are allowed to say.

## How it works

```
Family dials one number ─▶ intake agent takes group (said two ways + read-back), component, units, area
  ─▶ hangs up ─▶ search below ─▶ calls the family back with where to go, and SMSes it
  (or: Request typed on the dashboard)
Request (group, component, units, area)
  ─▶ call blood banks in waves: 24×7 first, then nearest; Bolna voice agent, 2+ in parallel
       direct line no answer? ─▶ try the switchboard ─▶ "please connect me to the blood bank"
  ─▶ per call: parse transcript (groups heard, units, fee, staff name, read-back)
              + typed decisions from Jev / Clef: has_requested, confirmed_readback,
                mentioned_other_group, enough_units, replacement_donor_required, will_hold, open_now
  ─▶ SAFETY GATE: "confirmed" only if the decision model AND the transcript check agree
  ─▶ rank confirmed (enough units, travel time, hold, no donor needed) ─▶ split plan if needed
  ─▶ "Go to Sanjeevani, 4 units confirmed 11:23 by Ramesh, held 1 h, ~5 min away"
```

## Failure modes we designed for

| Failure | What we do |
|---|---|
| Misheard group ("O negative" heard as "O positive") | The agent always says the group two ways ("O negative, that is O minus") and finishes with an explicit read-back. "Confirmed" needs the model (≥0.9) and a deterministic transcript check to agree, with no other group mentioned. Otherwise the result is **unconfirmed: call to verify**. |
| STT hears "O" as "zero" or "oh" | The group parser normalises it (`zero negative` → `O-`). |
| Blood banks won't reserve by phone | We promise confirmed availability with a time and staff name, not a reservation. We ask whether they'll hold the units, and that boosts ranking. |
| Nobody answers at night | 24×7 banks are called first, in parallel waves. Each facility has several numbers (direct line → switchboard), and no-answer or voicemail counts as **unknown**, never "no stock". |
| Not enough units at one bank | A split plan combines banks, with a warning to send two people. |
| Replacement donor required | Detected and flagged; ranked lower. |
| Decision model down during live calls | Falls back to a heuristic, but the gate refuses to confirm on heuristic answers. |
| Agent says the requirement as stock ("we need 2 units") | The units parser only trusts staff lines or the "Confirming:" read-back. |
| Privacy | No patient name or condition on calls, only the hospital and area. |
| Disclosure | The agent says it is an AI and that the call may be recorded. |
| Runaway cost or overloaded lines | Per-search cap on paid calls, account concurrency limit, search deadline. |

## Run it (free, simulated)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000, pick **O-**, and press **Find blood now**. The simulated blood banks cover every path: stock, partial stock, replacement donor, out of stock, misheard group, ambiguous answer, switchboard transfer, closed, no answer, voicemail.

Pretend a family phoned in: `.venv\Scripts\python scripts\sim_inbound.py O- 2 "HSR Layout"`. It shows up in **Incoming calls**, the search runs, and the simulated callback reads the plan. See [docs/PHONE_INTAKE.md](docs/PHONE_INTAKE.md) for the real thing.

Tests: `.venv\Scripts\python -m pytest`

## Real decisions and real calls

- **Clef:** set `DECISION_BACKEND=clef`, `CF_ACCOUNT_ID` and `CF_API_TOKEN` (Cloudflare Workers AI).
- **Jev:** set `DECISION_BACKEND=jev` and `TYPESAFE_API_KEY`.
- **Bolna:**
  1. `scripts\check_account.py` shows balance and concurrency.
  2. `scripts\create_agent.py` creates the agent; use `--update` to push prompt changes.
  3. `scripts\test_call.py +91XXXXXXXXXX O-` makes one test call to your own phone.

  Trial accounts can only call verified numbers. Put those in `data/facilities_live.json` (gitignored) and set `FACILITIES_FILE` and `LIVE_MODE=true`.
- **Phone intake (voice in, voice out):** `scripts\create_intake_agent.py` creates the intake and callback agents, the post-call extractions and links an inbound number. Steps in [docs/PHONE_INTAKE.md](docs/PHONE_INTAKE.md).

Real calls so far: 51–61 s each, 6–9 cents per call.

## Code map

| Piece | File |
|---|---|
| Search pipeline (waves, fallback numbers, deadline) | `app/orchestrator.py` |
| Safety gate, ranking, split plan | `app/ranking.py` |
| Questions for Jev/Clef | `app/decision/questions.py` |
| Transcript parsing (groups, units, fee, staff, read-back) | `app/parsing.py` |
| Bolna provider / free simulator | `app/providers/` |
| Agent prompt | `agent/blood_bank_caller_prompt.md` |
| Phone intake: webhook → request → callback script | `app/intake.py`, `agent/intake_prompt.md`, `agent/callback_prompt.md` |
| SMS (Plivo/Twilio, optional) | `app/notify.py` |
| Dashboard | `app/templates/index.html`, `app/static/` |
