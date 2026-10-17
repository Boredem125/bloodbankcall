"""Errand pipeline: classify -> dial in parallel -> decide per call -> rank -> pick -> book."""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .decision import FallbackEngine
from .decision.base import choice, noul, score
from .decision.questions import (
    RELIABILITY_LEVELS,
    call_questions,
    call_state,
    final_questions,
    final_state,
    intake_questions,
    intake_state,
)
from .parsing import parse_price, parse_slot
from .providers import CallProvider
from .ranking import rank
from .schemas import TERMINAL_STATUSES, CallRecord, Decision, Errand, Vendor
from .store import Store
from .vendors import vendors_for

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        provider: CallProvider,
        engine: FallbackEngine,
        store: Store,
        vendors: list[Vendor],
    ) -> None:
        self.s = settings
        self.provider = provider
        self.engine = engine
        self.store = store
        self.vendors = vendors
        # Live calls beyond the account's concurrency wait here instead of being rejected by Bolna.
        self._lines = asyncio.Semaphore(max(settings.live_concurrency, 1))

    @property
    def live(self) -> bool:
        return self.provider.name != "sim"

    def _emit(self, errand: Errand) -> None:
        self.store.publish(errand)

    async def _decide(self, errand: Errand, stage: str, state, questions, subject: str | None = None):
        answers, backend, ms = await self.engine.decide(state, questions)
        errand.decisions.append(
            Decision(stage=stage, backend=backend, latency_ms=round(ms, 1), answers=answers, subject=subject)
        )
        return answers

    # ------------------------------------------------------------------ main
    async def run(self, errand_id: str) -> None:
        errand = self.store.get(errand_id)
        try:
            await self._run(errand)
        except Exception as exc:  # noqa: BLE001 - surface any failure on the dashboard
            log.exception("errand %s failed", errand_id)
            errand.status = "failed"
            errand.reason = f"Something went wrong: {exc}"
            self._emit(errand)

    async def _run(self, errand: Errand) -> None:
        req = errand.request

        answers = await self._decide(errand, "intake", intake_state(req), intake_questions())
        errand.category, conf = choice(answers, "category")
        errand.note(f"Classified as {errand.category} (p={conf:.2f})")
        queue = vendors_for(self.vendors, errand.category)
        if not queue:
            errand.status, errand.reason = "failed", f"No businesses listed for {errand.category}."
            self._emit(errand)
            return

        cap = self.s.max_live_calls_per_errand if self.live else len(queue)
        batch = min(self.s.initial_calls, cap)
        errand.status = "calling"
        errand.note(f"Calling {min(batch, len(queue))} businesses in parallel")
        self._emit(errand)

        dialed = 0
        while True:
            next_vendors = queue[dialed: dialed + batch]
            dialed += len(next_vendors)
            await asyncio.gather(*(self._quote_call(errand, v) for v in next_vendors))
            viable = rank(errand.calls, req.priority)
            self._emit(errand)
            room = min(cap, len(queue)) - dialed
            if len(viable) >= self.s.min_viable_quotes or room <= 0:
                break
            batch = min(room, self.s.min_viable_quotes - len(viable) + 1)
            errand.note(f"Only {len(viable)} usable quote(s); calling {batch} more")
            self._emit(errand)

        if not viable:
            errand.status = "failed"
            errand.reason = "No business gave a usable quote. Try a wider area or a different time."
            self._emit(errand)
            return

        await self._pick(errand, viable)

    # ------------------------------------------------------------- per call
    async def _quote_call(self, errand: Errand, vendor: Vendor) -> None:
        call = CallRecord(vendor=vendor)
        errand.calls.append(call)
        await self._place_and_track(errand, call, self._user_data(errand, vendor))
        if call.status != "completed":
            call.outcome = call.status
            self._emit(errand)
            return

        call.price = call.price or parse_price(call.transcript)
        call.slot = call.slot or parse_slot(call.transcript)
        answers = await self._decide(
            errand, "call", call_state(errand.request, call), call_questions(), subject=call.id
        )
        outcome, _ = choice(answers, "outcome")
        call.outcome = outcome
        call.signals = {
            "got_quote": noul(answers, "got_quote"),
            "within_budget": noul(answers, "within_budget"),
            "hidden_charges": noul(answers, "hidden_charges"),
            "slot_fits": noul(answers, "slot_fits"),
            "reliability": score(answers, "reliability", len(RELIABILITY_LEVELS)),
            "quoted": float((answers["outcome"].get("probabilities") or {}).get("quoted", 1.0 if outcome == "quoted" else 0.0)),
        }
        errand.note(f"{vendor.name}: {outcome}" + (f", Rs {call.price}" if call.price else ""))
        self._emit(errand)

    async def _place_and_track(self, errand: Errand, call: CallRecord, user_data: dict) -> None:
        if not self.live:
            return await self._dial(errand, call, user_data)
        call.status = "waiting for line"
        self._emit(errand)
        async with self._lines:
            await self._dial(errand, call, user_data)

    async def _dial(self, errand: Errand, call: CallRecord, user_data: dict) -> None:
        try:
            call.execution_id = await self.provider.start(call.vendor, user_data)
        except Exception as exc:  # noqa: BLE001
            call.status, call.error = "failed", str(exc)
            self._emit(errand)
            return
        call.status = "queued"
        self._emit(errand)
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
            if st.status in TERMINAL_STATUSES:
                call.cost_cents, call.duration_s = st.cost_cents, st.duration_s
                call.recording_url, call.error = st.recording_url, st.error
                if st.extracted:
                    call.price = parse_price(st.transcript, st.extracted)
                    call.slot = parse_slot(st.transcript, st.extracted)
                self._emit(errand)
                return
            if changed:
                self._emit(errand)
        call.status, call.error = "timeout", "No final status before timeout"
        self._emit(errand)

    def _user_data(self, errand: Errand, vendor: Vendor, **extra) -> dict:
        req = errand.request
        return {
            "business_name": vendor.name,
            "task": req.task,
            "area": req.area,
            "time_window": req.time_window,
            "budget": str(req.budget),
            "customer_name": self.s.customer_name,
            "language": req.language,
            "mode": "quote",
            **extra,
        }

    # ------------------------------------------------------------ pick/book
    async def _pick(self, errand: Errand, viable: list[CallRecord]) -> None:
        req = errand.request
        errand.status = "deciding"
        self._emit(errand)
        shortlist = viable[:5]
        answers = await self._decide(errand, "final", final_state(req, shortlist), final_questions(req, shortlist))
        picked, conf = choice(answers, "best_vendor")
        chosen = next((c for c in shortlist if c.id == picked), shortlist[0])
        errand.chosen_call_id = chosen.id
        errand.choice_confidence = round(conf, 3)
        errand.auto_book_probability = round(noul(answers, "safe_to_auto_book"), 3)
        errand.reason = explain(chosen, errand.calls, req.priority)

        auto = (
            req.auto_book_limit > 0
            and chosen.price is not None
            and chosen.price <= req.auto_book_limit
            and conf >= self.s.auto_book_confidence
            and errand.auto_book_probability >= self.s.auto_book_confidence
        )
        if auto:
            errand.note(f"Auto-booking {chosen.vendor.name} (confidence {conf:.2f})")
            await self._book(errand, chosen)
        else:
            errand.status = "awaiting_approval"
            errand.note(f"Recommending {chosen.vendor.name} (confidence {conf:.2f}); waiting for approval")
            self._emit(errand)

    async def approve(self, errand_id: str, call_id: str | None = None) -> Errand:
        errand = self.store.get(errand_id)
        if errand is None or errand.status != "awaiting_approval":
            raise ValueError("errand is not waiting for approval")
        target = errand.call(call_id or errand.chosen_call_id)
        if not target.viable:
            raise ValueError("that business did not give a usable quote")
        errand.chosen_call_id = target.id
        asyncio.create_task(self._book(errand, target))
        return errand

    async def _book(self, errand: Errand, chosen: CallRecord) -> None:
        errand.status = "booking"
        self._emit(errand)
        if self.live and not self.s.booking_call:
            errand.status = "booked"
            errand.note(f"Marked {chosen.vendor.name} as booked (BOOKING_CALL is off, no call placed)")
            self._emit(errand)
            return
        call = CallRecord(vendor=chosen.vendor, purpose="book", price=chosen.price, slot=chosen.slot)
        errand.calls.append(call)
        await self._place_and_track(
            errand, call,
            self._user_data(errand, chosen.vendor, mode="book", price=str(chosen.price), slot=chosen.slot or ""),
        )
        errand.status = "booked" if call.status == "completed" else "failed"
        errand.note(
            f"Booked {chosen.vendor.name}" if errand.status == "booked"
            else f"Booking call ended with {call.status}"
        )
        self._emit(errand)


def explain(chosen: CallRecord, calls: list[CallRecord], priority: str) -> str:
    """Plain-language reason shown to the user."""
    quotes = [c for c in calls if c.purpose == "quote"]
    viable = [c for c in quotes if c.viable]
    parts = [f"{chosen.vendor.name}: Rs {chosen.price}"]
    if chosen.slot:
        parts.append(chosen.slot)
    parts.append(f"rated {chosen.vendor.rating}")
    if chosen.signals.get("hidden_charges", 1) < 0.5:
        parts.append("no extra charges")
    head = ", ".join(parts) + "."
    beat = len(viable) - 1
    lines = [head, f"Best for '{priority}' among {len(viable)} usable quotes from {len(quotes)} calls." if beat else ""]
    skipped = []
    for c in quotes:
        if c is chosen:
            continue
        if c.signals.get("hidden_charges", 0) >= 0.5:
            skipped.append(f"{c.vendor.name} (hidden charges)")
        elif c.outcome == "no_slot" or (c.viable and c.signals.get("slot_fits", 1) < 0.5):
            skipped.append(f"{c.vendor.name} (no slot in time)")
        elif c.viable and c.signals.get("within_budget", 1) < 0.5:
            skipped.append(f"{c.vendor.name} (over budget)")
        elif c.viable and c.signals.get("reliability", 1) < 0.45:
            skipped.append(f"{c.vendor.name} (vague answers)")
    if skipped:
        lines.append("Passed on " + "; ".join(skipped) + ".")
    return " ".join(x for x in lines if x)
