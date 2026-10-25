"""Set up the phone-in flow on Bolna: intake agent (+ extractions), callback agent, inbound number.

Usage (creating/updating agents is free; only calls cost):
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --role intake --webhook https://<public>/webhooks/bolna
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --role callback
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --role intake --update      (PATCH prompt + welcome)
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --list-numbers
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --link-number auto         (first number -> intake agent)
  .venv\\Scripts\\python.exe scripts\\create_intake_agent.py --role intake --dry-run

Profiles (--profile en|hi|kn) and --voice-id/--voice-name are the same as scripts/create_agent.py.
The webhook URL must be public (ngrok/cloudflared to :8000). Add ?token=<WEBHOOK_TOKEN> if you set one.
See docs/PHONE_INTAKE.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from app.config import get_settings  # noqa: E402
from create_agent import PROFILES, synthesizer, transcriber  # noqa: E402

PROMPTS = {
    "intake": (HERE.parent / "agent" / "intake_prompt.md").read_text(encoding="utf-8"),
    "callback": (HERE.parent / "agent" / "callback_prompt.md").read_text(encoding="utf-8"),
}
WELCOME = {
    "intake": {
        "en": "Namaste, BloodBankCall here. I am an AI assistant and this call may be recorded. I will take the blood request and call you back with where to go. Which blood group does the patient need?",
        "hi": "नमस्ते, BloodBankCall। मैं एक AI असिस्टेंट हूँ, यह कॉल रिकॉर्ड हो सकती है। मैं ब्लड की ज़रूरत नोट करके आपको वापस कॉल करूँगी। मरीज़ को कौन सा ब्लड ग्रुप चाहिए?",
        "kn": "ನಮಸ್ಕಾರ, BloodBankCall. ನಾನು AI ಸಹಾಯಕ, ಈ ಕರೆ ರೆಕಾರ್ಡ್ ಆಗಬಹುದು. ರಕ್ತದ ಅಗತ್ಯವನ್ನು ಬರೆದುಕೊಂಡು ನಿಮಗೆ ಮರಳಿ ಕರೆ ಮಾಡುತ್ತೇನೆ. ರೋಗಿಗೆ ಯಾವ ರಕ್ತದ ಗುಂಪು ಬೇಕು?",
    },
    "callback": {
        "en": "Namaste, this is BloodBankCall calling back about the {{blood_group_spoken}} blood you asked for. Can you hear me clearly?",
        "hi": "नमस्ते, BloodBankCall से बोल रही हूँ, आपने {{blood_group_spoken}} ब्लड के लिए कॉल किया था। क्या आप मुझे साफ़ सुन पा रहे हैं?",
        "kn": "ನಮಸ್ಕಾರ, BloodBankCall ನಿಂದ ಕರೆ ಮಾಡುತ್ತಿದ್ದೇನೆ, ನೀವು {{blood_group_spoken}} ರಕ್ತಕ್ಕಾಗಿ ಕರೆ ಮಾಡಿದ್ದಿರಿ. ನನ್ನ ಮಾತು ಸ್ಪಷ್ಟವಾಗಿ ಕೇಳುತ್ತಿದೆಯಾ?",
    },
}

# Post-call extractions (Bolna dispositions). Names are the keys app/intake.py reads.
EXTRACTIONS: list[dict[str, Any]] = [
    {
        "name": "blood_group", "question": "Which blood group did the caller ask for, as finally confirmed in the read-back?",
        "is_objective": True,
        "objective_options": [{"value": g, "condition": f"The caller needs {g[:-1]} {'positive' if g.endswith('+') else 'negative'} ({g[:-1]} {'plus' if g.endswith('+') else 'minus'})"}
                              for g in ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")]
        + [{"value": "unclear", "condition": "No single blood group was clearly stated or confirmed"}],
    },
    {
        "name": "component", "question": "Which blood component is needed?", "is_objective": True,
        "objective_options": [
            {"value": "prbc", "condition": "Packed red cells, PRBC, packed cells, or the caller did not know"},
            {"value": "whole_blood", "condition": "Whole blood"},
            {"value": "platelets", "condition": "Platelets, SDP or RDP"},
            {"value": "ffp", "condition": "Plasma or FFP"},
        ],
    },
    {"name": "units", "question": "How many units are needed? Answer with a number only.", "is_subjective": True,
     "subjective_type": "numeric"},
    {"name": "area", "question": "Which area or locality of the city is the patient in? Answer with the area name only.",
     "is_subjective": True},
    {"name": "hospital", "question": "Which hospital is the patient admitted to? Hospital name only; never the patient's name.",
     "is_subjective": True},
    {"name": "callback_number", "question": "If the caller asked to be called back on a different phone number, give that number as digits only. Otherwise answer with an empty string.",
     "is_subjective": True},
    {
        "name": "language", "question": "Which language did the caller mainly speak?", "is_objective": True,
        "objective_options": [{"value": "en", "condition": "English"}, {"value": "hi", "condition": "Hindi or Hinglish"},
                              {"value": "kn", "condition": "Kannada"}],
    },
]


def agent_body(role: str, webhook: str | None, profile: str = "en", voice_id: str | None = None,
               voice_name: str | None = None, tts_model: str = "bulbul:v4-flash") -> dict[str, Any]:
    if role not in PROMPTS:
        raise ValueError("role must be intake or callback")
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    task_config: dict[str, Any] = {"call_terminate": 180 if role == "intake" else 150, "hangup_after_silence": 15}
    config: dict[str, Any] = {
        "agent_name": f"BloodBankCall {role}" + ("" if profile == "en" else f" ({profile})"),
        "agent_welcome_message": WELCOME[role][profile],
        "tasks": [{
            "task_type": "conversation",
            "toolchain": {"execution": "sequential", "pipelines": [["transcriber", "llm", "synthesizer"]]},
            "tools_config": {
                "llm_agent": {
                    "agent_type": "simple_llm_agent", "agent_flow_type": "streaming",
                    "llm_config": {"provider": "openai", "model": "gpt-5.4-mini", "max_tokens": 150, "temperature": 1},
                },
                "synthesizer": synthesizer(profile, voice_id, voice_name, tts_model),
                "transcriber": transcriber(profile, keywords=True),
                "input": {"provider": "plivo", "format": "wav"},
                "output": {"provider": "plivo", "format": "wav"},
            },
            "task_config": task_config,
        }],
    }
    if webhook:
        config["webhook_url"] = webhook
    return {"agent_config": config, "agent_prompts": {"task_1": {"system_prompt": PROMPTS[role]}}}


def create_extractions(client: httpx.Client, agent_id: str, model: str = "gpt-4.1-mini") -> None:
    """One category 'Blood request' + one disposition per field, via the Dispositions API."""
    r = client.post(f"/agent/{agent_id}/extraction-categories", json={"name": "Blood request", "model": model})
    if r.status_code >= 400:
        sys.exit(f"extraction category: Bolna said {r.status_code}: {r.text}")
    category_id = r.json().get("id")
    for ex in EXTRACTIONS:
        body = {"agent_id": agent_id, "category_id": category_id, **ex}
        r = client.post("/dispositions/", json=body)
        if r.status_code >= 400:
            sys.exit(f"extraction {ex['name']}: Bolna said {r.status_code}: {r.text}")
        print(f"  extraction {ex['name']}: ok")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=("intake", "callback"), default="intake")
    ap.add_argument("--profile", choices=PROFILES, default="en")
    ap.add_argument("--webhook", help="public URL of /webhooks/bolna (intake needs it; add ?token=... if WEBHOOK_TOKEN is set)")
    ap.add_argument("--voice-id")
    ap.add_argument("--voice-name")
    ap.add_argument("--tts-model", default="bulbul:v4-flash")
    ap.add_argument("--update", action="store_true", help="PATCH prompt + welcome onto the existing agent for this role")
    ap.add_argument("--no-extractions", action="store_true", help="skip creating the post-call extractions (intake)")
    ap.add_argument("--list-numbers", action="store_true", help="GET /phone-numbers/all and exit")
    ap.add_argument("--link-number", metavar="ID|auto", help="POST /inbound/setup: route this number to the intake agent")
    ap.add_argument("--dry-run", action="store_true", help="print the agent body and exit; sends nothing")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="backslashreplace")

    if args.dry_run:
        print(json.dumps(agent_body(args.role, args.webhook, args.profile, args.voice_id, args.voice_name, args.tts_model),
                         indent=2, ensure_ascii=False))
        return

    s = get_settings()
    if not s.bolna_api_key:
        sys.exit("Set BOLNA_API_KEY in .env first.")
    client = httpx.Client(base_url=s.bolna_base_url, headers={"Authorization": f"Bearer {s.bolna_api_key}"}, timeout=30)

    if args.list_numbers or args.link_number:
        r = client.get("/phone-numbers/all")
        if r.status_code >= 400:
            sys.exit(f"Bolna said {r.status_code}: {r.text}")
        numbers = r.json() if isinstance(r.json(), list) else r.json().get("data") or r.json().get("items") or []
        for n in numbers:
            print(f"{n.get('id')}  {n.get('phone_number')}  {n.get('telephony_provider')}")
        if not numbers:
            print("No phone numbers on this account. Buy one in the Bolna dashboard (Phone numbers), country IN.")
        if not args.link_number:
            return
        if not s.bolna_intake_agent_id:
            sys.exit("Set BOLNA_INTAKE_AGENT_ID in .env first (create the intake agent).")
        pick = numbers[0] if args.link_number == "auto" and numbers else next((n for n in numbers if n.get("id") == args.link_number), None)
        if not pick:
            sys.exit("No such phone number id.")
        r = client.post("/inbound/setup", json={"agent_id": s.bolna_intake_agent_id, "phone_number_id": pick["id"]})
        if r.status_code >= 400:
            sys.exit(f"inbound/setup: Bolna said {r.status_code}: {r.text}")
        print("Linked. Put this in .env:\nBOLNA_INBOUND_NUMBER=" + str(r.json().get("phone_number") or pick.get("phone_number")))
        return

    body = agent_body(args.role, args.webhook, args.profile, args.voice_id, args.voice_name, args.tts_model)
    env_key = "BOLNA_INTAKE_AGENT_ID" if args.role == "intake" else "BOLNA_CALLBACK_AGENT_ID"
    existing = s.bolna_intake_agent_id if args.role == "intake" else s.bolna_callback_agent_id

    if args.update:
        if not existing:
            sys.exit(f"Set {env_key} in .env first.")
        r = client.patch(f"/v2/agent/{existing}", json={
            "agent_config": {"agent_welcome_message": body["agent_config"]["agent_welcome_message"]},
            "agent_prompts": body["agent_prompts"],
        })
        if r.status_code >= 400:
            sys.exit(f"Bolna said {r.status_code}: {r.text}")
        print("Prompt updated:", r.json())
        return

    if args.role == "intake" and not args.webhook:
        print("WARNING: no --webhook. Without it finished intake calls never reach the app.", file=sys.stderr)
    r = client.post("/v2/agent", json=body)
    if r.status_code >= 400:
        sys.exit(f"Bolna said {r.status_code}: {r.text}")
    agent_id = r.json()["agent_id"]
    print(f"Created {args.role} agent. Put this in .env:\n{env_key}={agent_id}")
    if args.role == "intake" and not args.no_extractions:
        print("Creating post-call extractions:")
        create_extractions(client, agent_id)


if __name__ == "__main__":
    main()
