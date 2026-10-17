"""Create the BloodBankCall caller agent on Bolna and print its agent_id.

Usage:  .venv\\Scripts\\python.exe scripts\\create_agent.py [--webhook https://<tunnel>/webhooks/bolna]

Creates an English agent from Bolna's validated minimal config. To speak Hindi, open the agent in
the Bolna dashboard and switch the transcriber to Sarvam (saaras, language "hi") and the voice to a
Sarvam Bulbul voice. Creating an agent costs nothing; calls do.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

PROMPT = (Path(__file__).resolve().parent.parent / "agent" / "blood_bank_caller_prompt.md").read_text(encoding="utf-8")


def agent_body(webhook: str | None) -> dict:
    config = {
        "agent_name": "BloodBankCall caller",
        "agent_welcome_message": "Namaste, I am an AI assistant calling for a patient's family; this call may be recorded. Is this the blood bank?",
        "tasks": [{
            "task_type": "conversation",
            "toolchain": {"execution": "sequential", "pipelines": [["transcriber", "llm", "synthesizer"]]},
            "tools_config": {
                "llm_agent": {
                    "agent_type": "simple_llm_agent",
                    "agent_flow_type": "streaming",
                    "llm_config": {"provider": "openai", "model": "gpt-5.4-mini", "max_tokens": 120, "temperature": 1},
                },
                "synthesizer": {
                    "provider": "elevenlabs",
                    "provider_config": {"voice": "Angelica", "voice_id": "IkSv4tkouLJ6kYsQA7XD", "model": "eleven_turbo_v2_5"},
                    "stream": True, "buffer_size": 250, "audio_format": "wav",
                },
                "transcriber": {
                    "provider": "deepgram", "model": "nova-3", "language": "en", "stream": True,
                    "encoding": "linear16", "sampling_rate": 16000, "endpointing": 250,
                },
                "input": {"provider": "plivo", "format": "wav"},
                "output": {"provider": "plivo", "format": "wav"},
            },
            # Hard cost cap: hang up after 150 s (switchboard transfers take time), or 15 s of silence.
            "task_config": {"call_terminate": 150, "hangup_after_silence": 15},
        }],
    }
    if webhook:
        config["webhook_url"] = webhook
    return {"agent_config": config, "agent_prompts": {"task_1": {"system_prompt": PROMPT}}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", help="public URL of /webhooks/bolna (optional)")
    ap.add_argument("--update", action="store_true", help="push the prompt file to the existing BOLNA_AGENT_ID")
    args = ap.parse_args()
    s = get_settings()
    if not s.bolna_api_key:
        sys.exit("Set BOLNA_API_KEY in .env first.")
    if args.update:
        if not s.bolna_agent_id:
            sys.exit("Set BOLNA_AGENT_ID in .env first.")
        r = httpx.patch(
            f"{s.bolna_base_url}/v2/agent/{s.bolna_agent_id}",
            headers={"Authorization": f"Bearer {s.bolna_api_key}"},
            json={
                "agent_config": {"agent_welcome_message": agent_body(None)["agent_config"]["agent_welcome_message"]},
                "agent_prompts": {"task_1": {"system_prompt": PROMPT}},
            },
            timeout=30,
        )
        if r.status_code >= 400:
            sys.exit(f"Bolna said {r.status_code}: {r.text}")
        print("Prompt updated:", r.json())
        return
    r = httpx.post(
        f"{s.bolna_base_url}/v2/agent",
        headers={"Authorization": f"Bearer {s.bolna_api_key}"},
        json=agent_body(args.webhook),
        timeout=30,
    )
    if r.status_code >= 400:
        sys.exit(f"Bolna said {r.status_code}: {r.text}")
    print("Created. Put this in .env:\nBOLNA_AGENT_ID=" + r.json()["agent_id"])


if __name__ == "__main__":
    main()
