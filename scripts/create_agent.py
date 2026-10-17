"""Create the BloodBankCall caller agent on Bolna and print its agent_id.

Usage:
  .venv\\Scripts\\python.exe scripts\\create_agent.py [--profile en] [--webhook https://<tunnel>/webhooks/bolna]
  .venv\\Scripts\\python.exe scripts\\create_agent.py --profile hi --voice-id <id> --voice-name "<name>"
  .venv\\Scripts\\python.exe scripts\\create_agent.py --replace     (PUT the full config onto BOLNA_AGENT_ID)
  .venv\\Scripts\\python.exe scripts\\create_agent.py --list-voices --language hi [--provider sarvam]
  .venv\\Scripts\\python.exe scripts\\create_agent.py --profile kn --voice-id <id> --voice-name "<name>" --dry-run

Profiles:
  en  Deepgram nova-3 English STT + ElevenLabs voice. The validated default.
  hi  Sarvam saaras:v4 STT (language "hi") + a Sarvam Bulbul voice you pick from --list-voices.
  kn  Sarvam saaras:v4 STT (language "kn") + a Sarvam Bulbul voice you pick from --list-voices.

--list-voices and --dry-run only read; creating or updating an agent costs nothing, calls do.
See docs/AGENT.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

PROMPT = (Path(__file__).resolve().parent.parent / "agent" / "blood_bank_caller_prompt.md").read_text(encoding="utf-8")

# STT keyword boosting (transcriber `keywords`, comma-separated). Deepgram nova-3 sends these as
# keyterms; Sarvam reads them on saaras:v4 only, max 50 terms of up to 64 characters each.
BLOOD_KEYWORDS = [
    "blood bank", "positive", "negative", "plus", "minus", "O", "A", "B", "AB",
    "O positive", "O negative", "A positive", "A negative", "B positive", "B negative",
    "AB positive", "AB negative", "PRBC", "packed cells", "packed red cells", "whole blood",
    "platelets", "plasma", "FFP", "units", "replacement", "donor", "crossmatch", "requisition",
    "processing fee",
]
KEYWORDS = ",".join(BLOOD_KEYWORDS)

# Sarvam `context`: a plain description of the call that biases recognition (not an LLM prompt).
STT_CONTEXT = (
    "Phone call to a hospital blood bank in India. Staff answer in English, Hindi or Kannada and "
    "talk about blood groups (O, A, B, AB, positive or negative, plus or minus), units in stock, "
    "packed red cells, platelets, plasma, replacement donors and processing fees."
)

WELCOME = {
    "en": "Namaste, I am an AI assistant calling for a patient's family; this call may be recorded. Is this the blood bank?",
    "hi": "नमस्ते, मैं एक मरीज़ के परिवार की तरफ़ से AI असिस्टेंट हूँ; यह कॉल रिकॉर्ड हो सकती है। क्या यह ब्लड बैंक है?",
    "kn": "ನಮಸ್ಕಾರ, ನಾನು ಒಬ್ಬ ರೋಗಿಯ ಕುಟುಂಬದ ಪರವಾಗಿ ಕರೆ ಮಾಡುತ್ತಿರುವ AI ಸಹಾಯಕ; ಈ ಕರೆ ರೆಕಾರ್ಡ್ ಆಗಬಹುದು. ಇದು ಬ್ಲಡ್ ಬ್ಯಾಂಕ್ ಹೌದಾ?",
}
PROFILES = ("en", "hi", "kn")
SARVAM_TTS_MODELS = ("bulbul:v4-flash", "bulbul:v3")


def transcriber(profile: str, keywords: bool = True) -> dict[str, Any]:
    if profile == "en":
        # Validated on real calls; `keywords` is the only addition (--no-keywords drops it).
        t: dict[str, Any] = {
            "provider": "deepgram", "model": "nova-3", "language": "en", "stream": True,
            "encoding": "linear16", "sampling_rate": 16000, "endpointing": 250,
        }
    else:
        # Sarvam picks sampling_rate/encoding to match telephony. 500 ms endpointing suits callers
        # who pause mid-sentence, which is common on a busy blood bank counter.
        t = {
            "provider": "sarvam", "model": "saaras:v4", "language": profile, "stream": True,
            "endpointing": 500, "context": STT_CONTEXT,
        }
    if keywords:
        t["keywords"] = KEYWORDS
    return t


def synthesizer(profile: str, voice_id: str | None, voice_name: str | None, tts_model: str) -> dict[str, Any]:
    if profile == "en":
        return {
            "provider": "elevenlabs",
            "provider_config": {"voice": "Angelica", "voice_id": "IkSv4tkouLJ6kYsQA7XD", "model": "eleven_turbo_v2_5"},
            "stream": True, "buffer_size": 250, "audio_format": "wav",
        }
    if not (voice_id and voice_name):
        raise ValueError(
            f"--profile {profile} needs --voice-id and --voice-name. Pick one with:\n"
            f"  python scripts/create_agent.py --list-voices --language {profile} --provider sarvam"
        )
    if tts_model not in SARVAM_TTS_MODELS:
        raise ValueError(f"--tts-model must be one of {SARVAM_TTS_MODELS}")
    return {
        "provider": "sarvam",
        "provider_config": {"voice": voice_name, "voice_id": voice_id, "model": tts_model,
                            "language": profile, "speed": 1.0},
        "stream": True, "buffer_size": 150,
    }


def agent_body(webhook: str | None, profile: str = "en", voice_id: str | None = None,
               voice_name: str | None = None, tts_model: str = "bulbul:v4-flash",
               noise_cancellation: int | None = None, keywords: bool = True) -> dict:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    task_config: dict[str, Any] = {"call_terminate": 150, "hangup_after_silence": 15}
    if noise_cancellation is not None:
        if not 60 <= noise_cancellation <= 100:
            raise ValueError("noise cancellation level must be 60-100")
        task_config["noise_cancellation_level"] = noise_cancellation
    config = {
        "agent_name": "BloodBankCall caller" + ("" if profile == "en" else f" ({profile})"),
        "agent_welcome_message": WELCOME[profile],
        "tasks": [{
            "task_type": "conversation",
            "toolchain": {"execution": "sequential", "pipelines": [["transcriber", "llm", "synthesizer"]]},
            "tools_config": {
                "llm_agent": {
                    "agent_type": "simple_llm_agent",
                    "agent_flow_type": "streaming",
                    "llm_config": {"provider": "openai", "model": "gpt-5.4-mini", "max_tokens": 120, "temperature": 1},
                },
                "synthesizer": synthesizer(profile, voice_id, voice_name, tts_model),
                "transcriber": transcriber(profile, keywords),
                "input": {"provider": "plivo", "format": "wav"},
                "output": {"provider": "plivo", "format": "wav"},
            },
            # Hard cost cap: hang up after 150 s (switchboard transfers take time), or 15 s of silence.
            "task_config": task_config,
        }],
    }
    if webhook:
        config["webhook_url"] = webhook
    return {"agent_config": config, "agent_prompts": {"task_1": {"system_prompt": PROMPT}}}


# ---------------------------------------------------------------- voice listing (GET only)
def list_voices(client: httpx.Client, language: str, provider: str | None = None) -> list[dict[str, Any]]:
    """GET /api/v1/voice-config/tts, then GET .../tts/voices per supported provider+model."""
    r = client.get("/api/v1/voice-config/tts", params={"language": language})
    r.raise_for_status()
    rows: list[dict[str, Any]] = []
    for prov in r.json().get("providers", []):
        if not prov.get("is_supported") or (provider and prov.get("slug") != provider):
            continue
        for model in prov.get("models", []):
            if not model.get("is_supported"):
                continue
            page = 1
            while True:
                v = client.get("/api/v1/voice-config/tts/voices", params={
                    "language": language, "provider_id": prov["id"], "model_id": model["id"],
                    "page": page, "page_size": 100,
                })
                v.raise_for_status()
                items = v.json().get("items", [])
                for it in items:
                    rows.append({"provider": prov.get("slug"), "model": model.get("model_id"),
                                 "voice_id": it.get("voice_id"), "voice": it.get("name"),
                                 "gender": it.get("gender"), "source": it.get("source")})
                if len(items) < 100:
                    break
                page += 1
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=PROFILES, default="en", help="language profile (default en)")
    ap.add_argument("--webhook", help="public URL of /webhooks/bolna (optional)")
    ap.add_argument("--update", action="store_true", help="push the prompt and welcome message to the existing BOLNA_AGENT_ID")
    ap.add_argument("--replace", action="store_true",
                    help="PUT the whole profile config (STT keywords, voice, prompt) onto the existing BOLNA_AGENT_ID")
    ap.add_argument("--voice-id", help="Sarvam voice_id for --profile hi/kn (from --list-voices)")
    ap.add_argument("--voice-name", help="Sarvam voice display name for --profile hi/kn (from --list-voices)")
    ap.add_argument("--tts-model", default="bulbul:v4-flash", help="Sarvam TTS model for hi/kn: bulbul:v4-flash | bulbul:v3")
    ap.add_argument("--noise-cancellation", type=int, metavar="60-100",
                    help="optional task_config.noise_cancellation_level (off by default; not yet tested on a live call)")
    ap.add_argument("--no-keywords", action="store_true", help="omit STT keyword boosting")
    ap.add_argument("--dry-run", action="store_true", help="print the request body and exit; sends nothing")
    ap.add_argument("--list-voices", action="store_true", help="list TTS voices (GET only) and exit")
    ap.add_argument("--language", default="hi", help="language code for --list-voices (default hi)")
    ap.add_argument("--provider", help="only list voices from this TTS provider slug, e.g. sarvam")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="backslashreplace")  # Windows consoles can't print Devanagari/Kannada

    if args.dry_run and not args.list_voices:
        try:
            body = agent_body(args.webhook, args.profile, args.voice_id, args.voice_name, args.tts_model,
                              args.noise_cancellation, not args.no_keywords)
        except ValueError as exc:
            sys.exit(str(exc))
        print(json.dumps(body["agent_config"], indent=2, ensure_ascii=False))
        return

    s = get_settings()
    if not s.bolna_api_key:
        sys.exit("Set BOLNA_API_KEY in .env first.")
    headers = {"Authorization": f"Bearer {s.bolna_api_key}"}

    if args.list_voices:
        with httpx.Client(base_url=s.bolna_base_url, headers=headers, timeout=30) as client:
            rows = list_voices(client, args.language, args.provider)
        for row in rows:
            print(f"{row['provider']:<11} {row['model']:<18} {row['voice_id']:<28} {row['voice']}"
                  f"{'  (' + row['gender'] + ')' if row['gender'] else ''}")
        print(f"\n{len(rows)} voices for language {args.language!r}. Use --voice-id <voice_id> --voice-name \"<voice>\".")
        return

    try:
        body = agent_body(args.webhook, args.profile, args.voice_id, args.voice_name, args.tts_model,
                          args.noise_cancellation, not args.no_keywords)
    except ValueError as exc:
        sys.exit(str(exc))

    if args.replace:
        if not s.bolna_agent_id:
            sys.exit("Set BOLNA_AGENT_ID in .env first.")
        r = httpx.put(f"{s.bolna_base_url}/v2/agent/{s.bolna_agent_id}", headers=headers, json=body, timeout=30)
        if r.status_code >= 400:
            sys.exit(f"Bolna said {r.status_code}: {r.text}")
        print("Agent replaced:", r.json())
        return
    if args.update:
        if not s.bolna_agent_id:
            sys.exit("Set BOLNA_AGENT_ID in .env first.")
        r = httpx.patch(
            f"{s.bolna_base_url}/v2/agent/{s.bolna_agent_id}",
            headers=headers,
            json={
                "agent_config": {"agent_welcome_message": body["agent_config"]["agent_welcome_message"]},
                "agent_prompts": {"task_1": {"system_prompt": PROMPT}},
            },
            timeout=30,
        )
        if r.status_code >= 400:
            sys.exit(f"Bolna said {r.status_code}: {r.text}")
        print("Prompt updated:", r.json())
        return
    r = httpx.post(f"{s.bolna_base_url}/v2/agent", headers=headers, json=body, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"Bolna said {r.status_code}: {r.text}")
    print("Created. Put this in .env:\nBOLNA_AGENT_ID=" + r.json()["agent_id"])


if __name__ == "__main__":
    main()
