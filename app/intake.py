"""Phone intake: the family calls us, we call every blood bank, then we call the family back.

  family dials BOLNA_INBOUND_NUMBER
    -> Bolna intake agent asks group (both ways + read-back), component, units, area, hospital
    -> Bolna webhook (status "completed") lands on POST /webhooks/bolna with transcript + extracted_data
    -> intake_request(): extractions first, transcript parsing as fallback, never guess the group
    -> Search(source="phone") runs the normal orchestrator (same safety gate)
    -> callback agent rings the caller and reads spoken_plan(); SMS carries the go-pack text
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from .config import Settings
from .facilities import AREAS
from .gopack import build_gopack
from .notify import send_sms
from .orchestrator import Orchestrator
from .parsing import parse_groups, words_to_digits
from .providers.bolna import _flatten
from .schemas import BLOOD_GROUPS, COMPONENTS, GROUP_SPOKEN, TERMINAL_STATUSES, BloodRequest, Facility, Phone, Search
from .store import Store

log = logging.getLogger(__name__)

COMPONENT_WORDS = {
    "prbc": ("prbc", "packed", "red cell", "rbc", "pcv"),
    "whole_blood": ("whole blood", "whole"),
    "platelets": ("platelet", "sdp", "rdp"),
    "ffp": ("ffp", "plasma", "frozen"),
}
_UNITS = re.compile(r"(?<!\d)(\d{1,2})\s*(?:units?|bags?|bottles?|pints?|packets?)\b", re.I)
_DIGITS = re.compile(r"\D")


class IntakeError(ValueError):
    """The call did not give us enough to search safely (no clear blood group, no caller)."""


class Intake(BaseModel):
    request: BloodRequest
    caller: str
    execution_id: str | None = None
    notes: list[str] = Field(default_factory=list)  # what was assumed; read back to the family


# ----------------------------------------------------------------- parsing the finished intake call
def e164(number: str | None, default_cc: str = "+91") -> str | None:
    """'98765 43210' -> '+919876543210'; keeps a leading country code; None if not a phone number."""
    if not number:
        return None
    digits = _DIGITS.sub("", str(number))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return default_cc + digits
    if len(digits) == 11 and digits.startswith("0"):
        return default_cc + digits[1:]
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def caller_of(payload: dict[str, Any]) -> str | None:
    tel = payload.get("telephony_data") or {}
    inbound = (tel.get("call_type") or "").lower() == "inbound"
    for cand in (payload.get("user_number"), tel.get("from_number") if inbound else None,
                 payload.get("recipient_phone_number"), tel.get("to_number") if not inbound else None):
        n = e164(cand)
        if n:
            return n
    return None


def user_lines(transcript: str) -> str:
    return "\n".join(ln.split(":", 1)[1] for ln in transcript.splitlines() if ln.lower().startswith("user:"))


def match_group(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    if v in BLOOD_GROUPS:
        return v
    found = parse_groups(v)
    return found[0] if len(set(found)) == 1 else None


def match_component(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).lower()
    if v in COMPONENTS:
        return v
    for key, words in COMPONENT_WORDS.items():
        if any(w in v for w in words):
            return key
    return None


def match_area(value: Any) -> str | None:
    if not value:
        return None
    v = str(value).lower()
    for area in AREAS:
        a = area.lower()
        if a in v or a.replace(" layout", "") in v:
            return area
    return None


def match_units(value: Any) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d{1,2}", words_to_digits(str(value)))
    return int(m.group()) if m else None


def intake_request(payload: dict[str, Any], default_area: str = "HSR Layout") -> Intake:
    """Build the search request from a finished intake call.

    Extractions (Bolna dispositions, flattened) win; the transcript is the fallback. The blood group
    is the one thing we never default: with no clear single group the intake is rejected and the
    family is told to call again."""
    ex = _flatten(payload.get("extracted_data") or {})
    transcript = payload.get("transcript") or ""
    said = user_lines(transcript)
    notes: list[str] = []

    group = match_group(ex.get("blood_group"))
    if group is None:
        heard = parse_groups(said)
        if heard and len(set(heard)) == 1:
            group = heard[0]
        elif heard:
            # the family may have corrected themselves: trust the agent's final "Confirming:" read-back
            # only if it names one group and the caller's last group agrees with it
            readbacks = [ln for ln in transcript.splitlines()
                         if ln.lower().startswith("assistant:") and "confirm" in ln.lower()]
            in_readback = set(parse_groups(readbacks[-1])) if readbacks else set()
            group = heard[-1] if in_readback == {heard[-1]} else None
    if group is None:
        raise IntakeError("no clear blood group on the intake call")

    component = match_component(ex.get("component")) or match_component(said) or "prbc"
    if match_component(ex.get("component")) is None and match_component(said) is None:
        notes.append("component assumed: packed red cells")

    units = match_units(ex.get("units"))
    if units is None:
        m = _UNITS.search(words_to_digits(said))
        units = int(m.group(1)) if m else None
    if units is None:
        units = 1
        notes.append("units assumed: 1")
    units = min(max(units, 1), 10)

    area = match_area(ex.get("area")) or match_area(said) or match_area(transcript)
    if area is None:
        area = default_area
        notes.append(f"area not recognised, searching around {default_area}")

    hospital = str(ex.get("hospital") or "").strip()[:120]
    if hospital.lower() in {"none", "null", "unknown", "not mentioned", "n/a", ""}:
        hospital = ""

    lang = str(ex.get("language") or "").lower()
    language = {"hi": "Hindi", "hindi": "Hindi", "kn": "Kannada", "kannada": "Kannada"}.get(lang, "English or Hindi")

    caller = e164(ex.get("callback_number")) or caller_of(payload)
    if not caller:
        raise IntakeError("no caller number on the intake call")

    return Intake(
        request=BloodRequest(blood_group=group, component=component, units=units, area=area,
                             hospital=hospital, language=language),
        caller=caller,
        execution_id=str(payload.get("id") or payload.get("execution_id") or "") or None,
        notes=notes,
    )


# ------------------------------------------------------------------------- what we say on callback
def spoken_plan(search: Search) -> str:
    """Plain sentences for the callback agent to read: no URLs, numbers the TTS can say."""
    req = search.request
    group = GROUP_SPOKEN[req.blood_group]
    comp = COMPONENTS[req.component].split(" (")[0]
    if search.status == "done" and search.chosen_call_id:
        g = build_gopack(search)
        parts = []
        if len(g.pickups) > 1:
            parts.append(f"Blood is confirmed, split across {len(g.pickups)} blood banks. Send two people if you can.")
        else:
            parts.append(f"Good news. {group} {comp} is confirmed.")
        by_id = {c.id: c for c in search.calls}
        for i, p in enumerate(g.pickups, 1):
            lead = f"Number {i}: " if len(g.pickups) > 1 else "Go to "
            eta = f", about {p.eta_min} minutes from {req.area}" if p.eta_min else ""
            who = f" Ask for {p.staff}." if p.staff else ""
            when = f" at {p.confirmed_at_text}" if p.confirmed_at_text else ""
            # what the bank said it has, not just the share of the request we assigned to it
            stock = (by_id[p.call_id].units if p.call_id in by_id else None) or p.units
            have = f"They confirmed {stock} unit{'s' if stock != 1 else ''}{when}."
            if len(g.pickups) > 1 and stock != p.units:
                have += f" Collect {p.units} there."
            parts.append(f"{lead}{p.name} in {p.area}{eta}. {have}{who}")
            if p.hold:
                parts.append(p.hold + ".")
            if p.fee:
                parts.append(f"Processing fee about {p.fee} rupees per unit.")
            if p.replacement_donor_required:
                parts.append("They need a replacement donor. Take one with you.")
            parts.append(f"Their phone number is {spell_number(p.phone)}.")
        if g.units_covered < req.units:
            parts.append(f"Only {g.units_covered} of {req.units} units are confirmed so far. Keep asking donor groups for the rest.")
        parts.append("Bring the doctor's signed requisition form, the patient's blood sample, and a photo ID.")
        parts.append("Availability can change, so please confirm at the counter when you arrive.")
        return " ".join(parts)
    # failed
    leads = [c for c in search.calls if c.availability == "unconfirmed"]
    parts = [f"I am sorry. No blood bank clearly confirmed {group} {comp} right now."]
    if leads:
        names = ", ".join(f"{c.facility.name} at {spell_number(c.phone.number)}" for c in leads[:2])
        parts.append(f"Possible leads, not confirmed, that you should call yourself: {names}.")
    parts.append("Please also ask the hospital blood bank and blood donor groups, and ask the treating doctor "
                 "whether another compatible group could be used.")
    return " ".join(parts)


def spell_number(number: str) -> str:
    """+919876543210 -> '98765 43210' spoken digit by digit (country code dropped for Indian numbers)."""
    digits = _DIGITS.sub("", number)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    return ", ".join(" ".join(digits[i:i + 5]) for i in range(0, len(digits), 5))


# --------------------------------------------------------------------------------------- the flow
class PhoneIntake:
    """Turns finished intake calls into searches, then rings the family back."""

    def __init__(self, settings: Settings, orch: Orchestrator, store: Store, provider) -> None:
        self.s = settings
        self.orch = orch
        self.store = store
        self.provider = provider
        self.seen: set[str] = set()  # intake execution ids already handled (webhooks repeat)

    def is_intake(self, payload: dict[str, Any]) -> bool:
        if (payload.get("status") or "").lower() != "completed":
            return False
        agent = str(payload.get("agent_id") or "")
        if self.s.bolna_intake_agent_id:
            return agent == self.s.bolna_intake_agent_id
        tel = payload.get("telephony_data") or {}
        return (tel.get("call_type") or "").lower() == "inbound"

    def accept(self, payload: dict[str, Any]) -> Search | None:
        """Create the search for a finished intake call; None if this is not one or was seen before."""
        if not self.is_intake(payload):
            return None
        exec_id = str(payload.get("id") or payload.get("execution_id") or "")
        if exec_id and exec_id in self.seen:
            return None
        if exec_id:
            self.seen.add(exec_id)
        try:
            intake = intake_request(payload)
        except IntakeError as exc:
            log.warning("intake call %s rejected: %s", exec_id, exc)
            search = self.store.add(Search(request=BloodRequest(), status="failed", source="phone",
                                           caller=caller_of(payload), intake_execution_id=exec_id or None))
            search.reason = f"Could not understand the request on the call ({exc}). Please call again."
            search.note(f"Phone intake rejected: {exc}")
            search.callback_status = "skipped"
            self.store.publish(search)
            return search
        search = self.store.add(Search(request=intake.request, source="phone", caller=intake.caller,
                                       intake_execution_id=intake.execution_id))
        search.note(f"Phone request from {_mask(intake.caller)}: {intake.request.units} unit(s) of "
                    f"{intake.request.blood_group} near {intake.request.area}")
        for n in intake.notes:
            search.note(f"Intake: {n}")
        search.warnings.extend(f"Intake: {n}" for n in intake.notes)
        self.store.publish(search)
        return search

    async def run(self, search_id: str) -> None:
        """Search, then call back. Runs as a background task."""
        search = self.store.get(search_id)
        await self.orch.run(search_id)
        await self.call_back(search)

    async def call_back(self, search: Search) -> None:
        if not search.caller:
            search.callback_status = "skipped"
            return
        text = build_gopack(search).message
        search.sms_status = await send_sms(self.s, search.caller, text)
        search.note(f"SMS to {_mask(search.caller)}: {search.sms_status}")
        self.store.publish(search)

        if self.orch.live and not self.s.bolna_callback_agent_id:
            search.callback_status = "skipped (BOLNA_CALLBACK_AGENT_ID not set)"
            search.note("Callback skipped: no callback agent configured")
            self.store.publish(search)
            return
        if self.s.callback_delay_s:
            await asyncio.sleep(self.s.callback_delay_s)
        family = Facility(id="family", name="Family", area=search.request.area, lat=0, lon=0,
                          phones=[Phone(number=search.caller, label="family")], sim={"persona": "family_callback"})
        user_data = {
            "plan_spoken": spoken_plan(search),
            "blood_group_spoken": GROUP_SPOKEN[search.request.blood_group],
            "outcome": "found" if search.status == "done" and search.chosen_call_id else "not_found",
            "language": search.request.language,
        }
        search.callback_status = "waiting for line" if self.orch.live else "queued"
        search.note(f"Calling {_mask(search.caller)} back with the plan")
        self.store.publish(search)
        async with self.orch._lines:  # a callback is a paid line too
            try:
                exec_id = await self.provider.start(family, family.phones[0], user_data,
                                                    agent_id=self.s.bolna_callback_agent_id or None)
            except Exception as exc:  # noqa: BLE001
                search.callback_status = f"failed: {exc}"
                search.note(f"Callback failed: {exc}")
                self.store.publish(search)
                return
            search.callback_execution_id, search.callback_status = exec_id, "queued"
            self.store.publish(search)
            deadline = time.monotonic() + self.s.call_timeout_s
            interval = self.s.poll_interval_s if self.orch.live else self.s.sim_poll_s
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                try:
                    st = await self.provider.status(exec_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("callback status poll failed: %s", exc)
                    continue
                if st.status != search.callback_status:
                    search.callback_status = st.status
                    self.store.publish(search)
                if st.status in TERMINAL_STATUSES or st.status == "voicemail":
                    search.note(f"Callback {st.status}")
                    self.store.publish(search)
                    return
            search.callback_status = "timeout"
            self.store.publish(search)


def _mask(n: str | None) -> str:
    from .schemas import mask_number

    return mask_number(n) or "unknown"
