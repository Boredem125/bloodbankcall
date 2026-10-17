"""Family handoff ("go-pack") for a finished search.

Everything a relative needs to walk into the blood bank: where, who, what to bring,
plus a short WhatsApp/SMS-ready message. The message never carries the patient's name.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from pydantic import BaseModel, Field

from .compat import alternatives_info
from .schemas import COMPONENTS, CallRecord, Search

DOCUMENTS = [
    "Doctor's signed blood requisition form",
    "Patient's blood sample in the tube the blood bank asks for (ask on the phone)",
    "Patient's or attendant's photo ID",
]
DONOR_DOC = "A replacement donor (the blood bank asked for one)"

_HOLD = re.compile(
    r"\b(?:hold|keep)\b[^.\n]*?\bfor\s+((?:one|two|three|half an?|an?|\d+)\s*(?:hours?|hrs?|minutes?|mins?))",
    re.IGNORECASE,
)


class Pickup(BaseModel):
    call_id: str
    name: str
    area: str
    phone: str
    phone_label: str
    maps_url: str
    units: int
    eta_min: int | None = None
    staff: str | None = None
    confirmed_at: float | None = None
    confirmed_at_text: str | None = None
    fee: int | None = None
    hold: str | None = None
    replacement_donor_required: bool = False


class Lead(BaseModel):
    name: str
    area: str
    phone: str
    maps_url: str
    reasons: list[str] = Field(default_factory=list)


class Alternatives(BaseModel):
    groups: list[str]
    note: str
    warning: str


class GoPack(BaseModel):
    search_id: str
    status: str  # done | failed
    blood_group: str
    component: str
    component_label: str
    units_needed: int
    units_covered: int = 0
    hospital: str = ""
    facility: Pickup | None = None  # the chosen blood bank (first pickup)
    pickups: list[Pickup] = Field(default_factory=list)  # more than one when units are split
    documents: list[str] = Field(default_factory=list)
    leads: list[Lead] = Field(default_factory=list)  # failed searches: unconfirmed, call to verify
    alternatives: Alternatives | None = None  # failed searches: informational, doctor decides
    warnings: list[str] = Field(default_factory=list)
    message: str
    whatsapp_url: str
    sms_url: str


def maps_url(name: str, area: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + quote(f"{name}, {area}", safe="")


def hold_text(call: CallRecord) -> str | None:
    will = call.signals.get("will_hold")
    if will is None:
        return None
    if will < 0.5:
        return "Not kept aside: go quickly"
    staff_lines = "\n".join(l for l in call.transcript.splitlines() if l.lower().startswith("user:"))
    m = _HOLD.search(staff_lines)
    return f"Kept aside for {m.group(1).lower()}" if m else "Agreed to keep it aside"


def _pickup(call: CallRecord, units: int) -> Pickup:
    f = call.facility
    return Pickup(
        call_id=call.id,
        name=f.name,
        area=f.area,
        phone=call.phone.number,
        phone_label=call.phone.label,
        maps_url=maps_url(f.name, f.area),
        units=units,
        eta_min=call.eta_min,
        staff=call.staff,
        confirmed_at=call.confirmed_at,
        confirmed_at_text=time.strftime("%H:%M", time.localtime(call.confirmed_at)) if call.confirmed_at else None,
        fee=call.fee,
        hold=hold_text(call),
        replacement_donor_required=call.signals.get("replacement_donor_required", 0) >= 0.5,
    )


def _links(text: str) -> tuple[str, str]:
    q = quote(text, safe="")
    return f"https://wa.me/?text={q}", f"sms:?&body={q}"


def _pickup_lines(p: Pickup) -> list[str]:
    eta = f" (~{p.eta_min} min)" if p.eta_min else ""
    who = f", ask for {p.staff}" if p.staff else ""
    when = f" (confirmed {p.confirmed_at_text})" if p.confirmed_at_text else ""
    lines = [f"{p.name}, {p.area}{eta}", f"Phone: {p.phone}{who}{when}", f"Map: {p.maps_url}"]
    extra = [x for x in (f"Fee about Rs {p.fee}/unit" if p.fee else "", p.hold or "") if x]
    if extra:
        lines.append(". ".join(extra) + ".")
    if p.replacement_donor_required:
        lines.append("Bring a replacement donor.")
    return lines


def build_gopack(search: Search) -> GoPack:
    if search.status not in ("done", "failed"):
        raise ValueError(f"search {search.id} is still {search.status}")
    req = search.request
    label = COMPONENTS[req.component]
    base = dict(
        search_id=search.id, status=search.status, blood_group=req.blood_group, component=req.component,
        component_label=label, units_needed=req.units, hospital=req.hospital, warnings=list(search.warnings),
    )
    for_whom = f" for a patient at {req.hospital}" if req.hospital else ""

    if search.status == "done" and search.chosen_call_id:
        by_id = {c.id: c for c in search.calls}
        plan = search.plan or []
        pickups = [_pickup(by_id[p.call_id], p.units) for p in plan if p.call_id in by_id]
        if not pickups:
            chosen = by_id[search.chosen_call_id]
            pickups = [_pickup(chosen, chosen.units or req.units)]
        covered = sum(p.units for p in pickups)
        documents = DOCUMENTS + ([DONOR_DOC] if any(p.replacement_donor_required for p in pickups) else [])

        count = f"{covered} unit(s)" if covered >= req.units else f"{covered} of {req.units} unit(s)"
        lines = [f"Blood confirmed{for_whom}: {req.blood_group} {label}, {count}."]
        if len(pickups) == 1:
            lines += ["Go to:"] + _pickup_lines(pickups[0])
        else:
            lines.append(f"Split across {len(pickups)} blood banks; send two people if possible.")
            for i, p in enumerate(pickups, 1):
                lines += ["", f"{i}) {p.units} unit(s) from:"] + _pickup_lines(p)
        if covered < req.units:
            lines.append(f"Still short of {req.units - covered} unit(s): keep asking donor groups.")
        lines += ["", "Bring:"] + [f"- {d}" for d in documents]
        lines += ["", "Availability can change. Confirm at the counter on arrival."]
        text = "\n".join(lines)
        wa, sms = _links(text)
        return GoPack(**base, units_covered=covered, facility=pickups[0], pickups=pickups, documents=documents,
                      message=text, whatsapp_url=wa, sms_url=sms)

    # failed (or done without a choice): leads to verify by phone, never presented as available
    leads = [
        Lead(name=c.facility.name, area=c.facility.area, phone=c.phone.number,
             maps_url=maps_url(c.facility.name, c.facility.area), reasons=list(c.gate_reasons))
        for c in search.calls if c.availability == "unconfirmed"
    ]
    alt = Alternatives(**alternatives_info(req.blood_group, req.component))
    lines = [f"No blood bank has confirmed {req.blood_group} {label} yet{for_whom}."]
    if leads:
        lines += ["", "Possible leads, call to verify (NOT confirmed):"]
        lines += [f"- {l.name}, {l.area}: {l.phone}" for l in leads]
    lines += ["", "Please also contact blood donor groups and the hospital's blood bank or "
                  "the state blood transfusion council for donors."]
    if alt.groups:
        lines += ["", f"Ask the treating doctor if another group could be used ({', '.join(alt.groups)}). "
                      "Only if the treating doctor approves."]
    text = "\n".join(lines)
    wa, sms = _links(text)
    return GoPack(**base, leads=leads, alternatives=alt, message=text, whatsapp_url=wa, sms_url=sms)
