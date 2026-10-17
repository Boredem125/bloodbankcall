"""Blood search pipeline.

call facilities in waves (24x7 + nearest first) -> per call: parse + typed decisions
-> safety gate -> rank confirmed -> pick + plan units -> plain-language result.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .cache import FreshnessCache, age_text
from .config import ROOT, Settings
from .decision import FallbackEngine
from .decision.base import choice, noul
from .decision.questions import call_questions, call_state, final_questions, final_state
from .facilities import PickupStats, call_order, eta_minutes, phone_order
from .guard import NumberLimiter
from .parsing import parse_fee, parse_staff, parse_units, vendor_groups
from .providers import CallProvider
from .ranking import gate, plan_units, rank, units_covered
from .schemas import (
    COMPONENTS,
    GROUP_CONFIRM,
    GROUP_SPOKEN,
    NOT_REACHED,
    TERMINAL_STATUSES,
    CallRecord,
    Decision,
    Facility,
    Phone,
    Search,
)
from .store import Store

log = logging.getLogger(__name__)
SIGNALS = [
    "has_requested", "confirmed_readback", "mentioned_other_group", "enough_units",
    "replacement_donor_required", "will_hold", "open_now",
]
PICKUP_MISS = {"no-answer", "busy", "voicemail"}  # the line rang and nobody (human) took it


class Orchestrator:
    def __init__(self, settings: Settings, provider: CallProvider, engine: FallbackEngine,
                 store: Store, facilities: list[Facility]) -> None:
        self.s = settings
        self.provider = provider
        self.engine = engine
        self.store = store
        self.facilities = facilities
        # Live calls beyond the account's concurrency wait here instead of being rejected by Bolna.
        self._lines = asyncio.Semaphore(max(settings.live_concurrency, 1))
        self._budget: dict[str, int] = {}  # paid calls left, per search
        # Memory across searches: fresh answers to reuse, per-number call counts, learned pickup rates.
        self.cache = FreshnessCache(settings.cache_ttl_min * 60)
        self.limiter = NumberLimiter(settings.max_calls_per_number_per_hour)
        path = Path(settings.pickup_stats_file) if settings.pickup_stats_file else None
        self.pickup = PickupStats(path if path is None or path.is_absolute() else ROOT / path)

    @property
    def live(self) -> bool:
        return self.provider.name != "sim"

    def _emit(self, search: Search) -> None:
        self.store.publish(search)

    async def _decide(self, search: Search, stage: str, state, questions, subject: str | None = None):
        answers, backend, ms = await self.engine.decide(state, questions)
        search.decisions.append(
            Decision(stage=stage, backend=backend, latency_ms=round(ms, 1), answers=answers, subject=subject)
        )
        return answers, backend

    # ------------------------------------------------------------------ main
    async def run(self, search_id: str) -> None:
        search = self.store.get(search_id)
        try:
            await self._run(search)
        except Exception as exc:  # noqa: BLE001 - surface any failure on the dashboard
            log.exception("search %s failed", search_id)
            search.status = "failed"
            search.reason = f"Something went wrong: {exc}. Call the blood banks directly."
            self._emit(search)

    async def _run(self, search: Search) -> None:
        req = search.request
        queue = call_order(self.facilities, req.area, self.pickup)
        self._budget[search.id] = self.s.max_live_calls_per_search if self.live else 10_000
        deadline = time.monotonic() + self.s.search_deadline_s
        wave = self.s.live_concurrency if self.live else self.s.wave_size
        search.note(
            f"Need {req.units} unit(s) of {req.blood_group} {COMPONENTS[req.component]} near {req.area}; "
            f"{len(queue)} blood banks on file"
        )
        self._emit(search)

        next_i = 0
        while next_i < len(queue) and self._budget[search.id] > 0:
            if time.monotonic() > deadline:
                search.note("Search time limit reached; deciding with what we have")
                break
            batch = queue[next_i: next_i + wave]
            next_i += len(batch)
            search.note(f"Calling {', '.join(f.name for f in batch)}")
            self._emit(search)
            await asyncio.gather(*(self._facility(search, f) for f in batch))
            ranked = rank(search.calls)
            covered = sum(c.units or 1 for c in ranked)
            if len(ranked) >= self.s.min_confirmed and covered >= req.units:
                break
            if next_i < len(queue):
                search.note(f"{len(ranked)} confirmed so far ({covered} unit(s)); calling more")
        await self._finish(search)

    # ------------------------------------------------------------ per facility
    async def _facility(self, search: Search, f: Facility) -> None:
        """Reuse a fresh answer if we have one; else try each number until someone answers
        (direct line first, unless another number picks up clearly more often at this hour)."""
        req = search.request
        hit = self.cache.get(f.id, req)
        if hit:
            call, age = hit
            call.eta_min = eta_minutes(req.area, f)
            search.calls.append(call)
            what = "confirmation" if call.availability == "confirmed" else "'not available'"
            search.note(f"{f.name}: reused {what} from {age_text(age)} (not called again)")
            self._emit(search)
            return
        phones = phone_order(f, self.pickup)
        if phones[0] is not f.phones[0]:
            search.note(f"{f.name}: trying {phones[0].label} first (picks up more often at this hour)")
        attempt = 0
        for i, phone in enumerate(phones):
            if self._budget[search.id] <= 0:
                return
            if not self.limiter.allow(phone.number):
                search.note(
                    f"{f.name}: skipped {phone.label} {phone.number}, already called "
                    f"{self.limiter.max_per_hour} times in the last hour"
                )
                self._emit(search)
                continue
            self._budget[search.id] -= 1
            self.limiter.record(phone.number)
            attempt += 1
            call = CallRecord(facility=f, phone=phone, attempt=attempt, eta_min=eta_minutes(req.area, f))
            search.calls.append(call)
            await self._place_and_track(search, call)
            self._learn_pickup(call)
            if call.status not in NOT_REACHED:
                await self._assess(search, call)
                return
            if i + 1 < len(phones):
                search.note(f"{f.name}: {call.status} on {phone.label}; trying {phones[i + 1].label}")
                self._emit(search)

    def _learn_pickup(self, call: CallRecord) -> None:
        # Our own failures (API error, timeout, cancel) say nothing about the blood bank's line.
        if call.status == "completed" or call.status in PICKUP_MISS:
            self.pickup.record(call.phone.number, answered=call.status == "completed")

    async def _assess(self, search: Search, call: CallRecord) -> None:
        req = search.request
        t = call.transcript
        call.units, call.fee, call.staff = parse_units(t), parse_fee(t), parse_staff(t)
        call.groups_heard = vendor_groups(t)
        answers, backend = await self._decide(search, "call", call_state(req, call), call_questions(req), call.id)
        call.outcome, _ = choice(answers, "outcome")
        call.signals = {k: noul(answers, k) for k in SIGNALS}
        trusted = not (self.live and backend == "mock")
        gate(call, req, trusted=trusted)
        if call.availability == "confirmed":
            call.confirmed_at = time.time()
        if trusted:
            self.cache.put(call, req)
        search.note(
            f"{call.facility.name}: {call.availability}"
            + (f", {call.units} unit(s)" if call.units else "")
            + (f" ({'; '.join(call.gate_reasons)})" if call.gate_reasons and call.availability == "unconfirmed" else "")
        )
        self._emit(search)

    async def _place_and_track(self, search: Search, call: CallRecord) -> None:
        if not self.live:
            return await self._dial(search, call)
        call.status = "waiting for line"
        self._emit(search)
        async with self._lines:
            await self._dial(search, call)

    def _user_data(self, search: Search, call: CallRecord) -> dict:
        req = search.request
        return {
            "facility_name": call.facility.name,
            "phone_label": call.phone.label,
            "blood_group_spoken": GROUP_SPOKEN[req.blood_group],
            "blood_group_confirm": GROUP_CONFIRM[req.blood_group],
            "component": COMPONENTS[req.component],
            "units": str(req.units),
            "hospital": req.hospital or "a hospital",
            "area": req.area,
            "language": req.language,
        }

    async def _dial(self, search: Search, call: CallRecord) -> None:
        try:
            call.execution_id = await self.provider.start(call.facility, call.phone, self._user_data(search, call))
        except Exception as exc:  # noqa: BLE001
            call.status, call.error = "failed", str(exc)
            self._emit(search)
            return
        call.status = "queued"
        self._emit(search)
        deadline = time.monotonic() + self.s.call_timeout_s
        interval = self.s.poll_interval_s if self.live else self.s.sim_poll_s
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                st = await self.provider.status(call.execution_id)
            except Exception as exc:  # noqa: BLE001 - transient API errors: keep polling
                log.warning("status poll failed for %s: %s", call.execution_id, exc)
                continue
            changed = st.status != call.status or st.transcript != call.transcript
            call.status, call.transcript = st.status, st.transcript
            if st.status in TERMINAL_STATUSES or st.status == "voicemail":
                call.cost_cents, call.duration_s = st.cost_cents, st.duration_s
                call.recording_url, call.error = st.recording_url, st.error
                self._emit(search)
                return
            if changed:
                self._emit(search)
        call.status, call.error = "timeout", "No final status before timeout"
        self._emit(search)

    # ------------------------------------------------------------------ finish
    async def _finish(self, search: Search) -> None:
        req = search.request
        search.status = "deciding"
        self._emit(search)
        ranked = rank(search.calls)
        unconfirmed = [c for c in search.calls if c.availability == "unconfirmed"]
        search.warnings = ["Availability can change. Confirm at the blood bank counter on arrival."]

        if not ranked:
            search.status = "failed"
            if unconfirmed:
                names = ", ".join(f"{c.facility.name} ({c.phone.number})" for c in unconfirmed)
                search.reason = f"No blood bank clearly confirmed {req.blood_group}. Possible leads, call to verify: {names}."
            else:
                search.reason = (
                    f"No blood bank confirmed {req.blood_group} {COMPONENTS[req.component]}. "
                    "Ask the hospital to contact donor groups or the state blood transfusion council."
                )
            search.note("No confirmed availability")
            self._emit(search)
            return

        shortlist = ranked[:5]
        answers, backend = await self._decide(search, "final", final_state(req, shortlist), final_questions(req, shortlist))
        picked, conf = choice(answers, "best_facility")
        chosen = next((c for c in shortlist if c.id == picked), shortlist[0])
        search.chosen_call_id, search.choice_confidence = chosen.id, round(conf, 3)
        search.plan = plan_units(ranked, chosen, req.units)
        search.reason = explain(chosen, search)
        if units_covered(search.plan) < req.units:
            search.warnings.append(
                f"Only {units_covered(search.plan)} of {req.units} unit(s) confirmed. Keep searching or ask donor groups."
            )
        if len(search.plan) > 1:
            search.warnings.append("Units are split across blood banks; send two people if possible.")
        if chosen.reused_at is not None:
            search.warnings.append(
                f"{chosen.facility.name} confirmed {age_text(self.cache.clock() - chosen.reused_at)} on an earlier search "
                "and was not called again."
            )
        if chosen.signals.get("replacement_donor_required", 0) >= 0.5:
            search.warnings.append(f"{chosen.facility.name} needs a replacement donor. Bring one along.")
        if unconfirmed:
            search.warnings.append(
                "Unconfirmed leads (call to verify): " + ", ".join(c.facility.name for c in unconfirmed)
            )
        search.status = "done"
        search.note(f"Recommend {chosen.facility.name} (confidence {conf:.2f})")
        self._emit(search)


def explain(chosen: CallRecord, search: Search) -> str:
    req = search.request
    when = time.strftime("%H:%M", time.localtime(chosen.confirmed_at or time.time()))
    who = f" by {chosen.staff}" if chosen.staff else ""
    parts = [
        f"{chosen.facility.name} confirmed {req.blood_group} {COMPONENTS[req.component]}"
        f"{f', {chosen.units} unit(s)' if chosen.units else ''} at {when}{who}.",
        f"About {chosen.eta_min} min away.",
    ]
    if chosen.signals.get("will_hold", 0) >= 0.5:
        parts.append("They agreed to keep it aside.")
    if chosen.fee:
        parts.append(f"Processing fee about Rs {chosen.fee} per unit.")
    others = [c for c in search.calls if c is not chosen and c.availability in ("not_available", "closed", "unconfirmed")]
    if others:
        parts.append("Ruled out: " + "; ".join(f"{c.facility.name} ({c.availability.replace('_', ' ')})" for c in others) + ".")
    return " ".join(parts)
