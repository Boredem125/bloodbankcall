"""Every question the decision model is asked, defined once.

Each question is atomic (one gut-check judgement about one call). Safety logic
(the verification gate) and ranking combine them in code, not in a prompt.
"""
from __future__ import annotations

from typing import Any

from ..schemas import COMPONENTS, GROUP_SPOKEN, BloodRequest, CallRecord

OUTCOMES = {
    "available": "Has the requested blood group and component now",
    "partial": "Has the requested group but fewer units than needed",
    "not_available": "Does not have the requested group or component now",
    "closed": "Blood bank is closed or cannot issue blood now",
    "wrong_department": "Reached someone who cannot check blood stock and did not transfer",
    "callback_later": "Asked to call back later or was too busy",
}


def _need(req: BloodRequest) -> str:
    return f"{GROUP_SPOKEN[req.blood_group]} ({req.blood_group}) {COMPONENTS[req.component]}"


def call_questions(req: BloodRequest) -> dict[str, Any]:
    need = _need(req)
    return {
        "has_requested": {
            "type": "noul",
            "instructions": f"The blood bank staff said they currently have {need} available.",
        },
        "confirmed_readback": {
            "type": "noul",
            "instructions": (
                f"When the assistant read back {GROUP_SPOKEN[req.blood_group]} and the number of units, "
                "the staff clearly agreed it was correct."
            ),
        },
        "mentioned_other_group": {
            "type": "noul",
            "instructions": (
                f"The staff's answer about what is available names a blood group other than "
                f"{GROUP_SPOKEN[req.blood_group]}, or is ambiguous about positive vs negative."
            ),
        },
        "enough_units": {
            "type": "noul",
            "instructions": f"They have at least {req.units} units available.",
        },
        "replacement_donor_required": {
            "type": "noul",
            "instructions": "The blood bank requires the family to bring a replacement donor.",
        },
        "will_hold": {
            "type": "noul",
            "instructions": "The staff agreed to keep the units aside for this patient for some time.",
        },
        "open_now": {
            "type": "noul",
            "instructions": "The blood bank is open and can issue blood right now.",
        },
        "outcome": {
            "type": "choice",
            "instructions": "What was the outcome of this call?",
            "criteria": dict(OUTCOMES),
        },
    }


def call_state(req: BloodRequest, call: CallRecord) -> dict[str, Any]:
    return {
        "request": {"blood_group": req.blood_group, "component": COMPONENTS[req.component], "units_needed": req.units},
        "facility": call.facility.name,
        "parsed": {"units_said": call.units, "groups_heard": call.groups_heard, "fee_rupees": call.fee},
        "transcript": call.transcript,
    }


def final_questions(req: BloodRequest, candidates: list[CallRecord]) -> dict[str, Any]:
    return {
        "best_facility": {
            "type": "choice",
            "instructions": (
                f"The family needs {req.units} units of {_need(req)} urgently. Which blood bank should "
                "they go to first? Prefer enough units, no replacement donor, units held, and less travel time."
            ),
            "criteria": {
                c.id: (
                    f"{c.facility.name}: {c.units or '?'} units, {c.eta_min} min away"
                    + (", needs replacement donor" if c.signals.get("replacement_donor_required", 0) >= 0.5 else "")
                    + (", will hold" if c.signals.get("will_hold", 0) >= 0.5 else "")
                )
                for c in candidates
            },
        },
    }


def final_state(req: BloodRequest, candidates: list[CallRecord]) -> dict[str, Any]:
    return {
        "request": req.model_dump(include={"blood_group", "component", "units", "area"}),
        "candidates": [
            {
                "id": c.id,
                "facility": c.facility.name,
                "units": c.units,
                "eta_min": c.eta_min,
                "signals": c.signals,
                "rank_score": c.rank_score,
            }
            for c in candidates
        ],
    }
