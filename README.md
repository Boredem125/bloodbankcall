# Phonewala

**An AI that phones the offline world for you.** Say what you need ("AC service tomorrow in HSR, under ₹800"). Phonewala calls 5 local businesses in parallel in Hindi or English and gets quotes. It then decides which one to book and explains why.

> The LLM talks. The decision model decides.

The voice agent (Bolna) runs the conversations. Every judgement goes to a typed decision model instead of free text: **TypeSafe Jev**, with **Cloudflare Clef** as a drop-in fallback. Those judgements are things like "is there a hidden visiting fee?", "can they come in time?", "how committed did they sound?" and "which vendor should we book?". Each answer is a choice, score or probability that code can branch on, and it is logged so you can audit it.

## How it works

```
Request ─▶ intake decision (category, urgency)
        ─▶ Bolna calls N vendors in parallel ── live transcript streams to the dashboard
        ─▶ per-call decisions: got_quote, within_budget, hidden_charges, slot_fits, reliability, outcome
        ─▶ ranking in code (weights per priority: cheapest / fastest / reliable)
        ─▶ too few usable quotes? call more vendors
        ─▶ final decision: best_vendor (choice) + safe_to_auto_book (probability)
        ─▶ auto-book under your limit, or ask you ─▶ booking call
```

| Piece | File |
|---|---|
| Pipeline | `app/orchestrator.py` |
| Questions asked of Jev/Clef | `app/decision/questions.py` |
| Jev + Clef clients, fallback chain | `app/decision/http.py`, `app/decision/base.py` |
| Offline stand-in for the decision model | `app/decision/mock.py` |
| Ranking weights | `app/ranking.py` |
| Bolna calls (live) / simulator (free) | `app/providers/bolna.py`, `app/providers/sim.py` |
| Dashboard | `app/templates/index.html`, `app/static/` |
| Agent prompt | `agent/vendor_caller_prompt.md` |

## Run it (free, simulated)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and press **Start calling**. Simulated vendors have personalities: honest, hidden fees, too expensive, no slot, vague, refuses, no answer. That lets you see every decision path without spending anything.

Tests: `.venv\Scripts\python -m pytest`

## Turn on real decisions

- **Clef:** set `DECISION_BACKEND=clef`, `CF_ACCOUNT_ID` and `CF_API_TOKEN` (Workers AI). Uses `clef-flash` by default.
- **Jev:** set `DECISION_BACKEND=jev` and `TYPESAFE_API_KEY` (early access at console.typesafe.ai). If Jev fails, Phonewala falls back to Clef, then to the mock.

The dashboard's decision log shows which backend answered each question and how fast.

## Turn on real calls (the $5 credit)

Each live call costs money. Develop in sim mode and use live calls only for milestones and the demo video.

1. Put `BOLNA_API_KEY` in `.env`. Then check your balance (free):
   `.venv\Scripts\python scripts\check_account.py`
2. Create the agent (free):
   `.venv\Scripts\python scripts\create_agent.py`
   Copy the `BOLNA_AGENT_ID` it prints into `.env`.
   For Hindi, open the agent in the Bolna dashboard and switch the transcriber to Sarvam (`saaras`, language `hi`) and the voice to a Sarvam Bulbul voice.
3. Make one test call to your own phone and play the shopkeeper:
   `.venv\Scripts\python scripts\test_call.py +91XXXXXXXXXX`
   Check that the prompt variables (`{{business_name}}`, …) were filled in and the price was parsed. Note the cost it prints.
4. Edit `data/vendors.json`: put your phone and two friends' phones on three `ac_repair` vendors. **Never call real businesses while testing.**
5. Set `LIVE_MODE=true` and run the app. Each errand is capped at `MAX_LIVE_CALLS_PER_ERRAND=3` calls. The agent hangs up after 90 seconds, or after 10 seconds of silence.

Suggested budget:
- ~$1 on single test calls
- ~$1 on one 3-way parallel rehearsal
- ~$3 kept for recording the demo

Webhooks are optional. Polling `GET /executions/{id}` works without a public URL. If you run a tunnel (`cloudflared tunnel --url http://localhost:8000`), pass `--webhook https://<tunnel>/webhooks/bolna` to `create_agent.py`.

## Stretch: negotiation during the call

`POST /tools/negotiate` takes `{errand_id, quoted_price}`. It returns `accept` or a `counter` built from the best quote so far. To use it, wire it to a Bolna custom function (needs the tunnel). The agent can then say: "Another service quoted ₹650, can you match it?"

## Demo script (2 minutes)

1. The problem: 60M+ Indian businesses only answer the phone, and getting three quotes takes a dozen calls.
2. Type "AC service tomorrow evening in HSR Layout, under ₹800" and press Start. Five calls light up, with live transcripts in Hindi and English.
3. The decision panel: Sri Sai looked cheapest at ₹399 but had a ₹250 visiting fee, so it was flagged. Raju had no slot. Frosty was over budget. CoolCare won at ₹650 all-in. Show the confidence and the decision log with millisecond latencies.
4. Approve. The booking call confirms the slot.
5. The pitch: "The LLM talks, the decision model decides." Typed, calibrated and auditable decisions are what make voice agents safe to automate. Next steps are supplier quotes, hospital beds, blood banks and business verification.
