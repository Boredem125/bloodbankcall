from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config import get_settings
from .decision import build_engine
from .facilities import AREAS, load_facilities
from .gopack import GoPack, build_gopack
from .intake import PhoneIntake
from .orchestrator import Orchestrator
from .providers import BolnaProvider, build_provider
from .schemas import BLOOD_GROUPS, COMPONENTS, BloodRequest, Search, mask_number
from .store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.settings = s
    app.state.provider = build_provider(s)
    app.state.engine = build_engine(s)
    app.state.orch = Orchestrator(s, app.state.provider, app.state.engine, store, load_facilities(s.facilities_file))
    app.state.intake = PhoneIntake(s, app.state.orch, store, app.state.provider)
    app.state.tasks = set()
    yield


app = FastAPI(title="BloodBankCall", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    s = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "live": s.live_mode,
            "chain": [e.name for e in request.app.state.engine.chain],
            "groups": BLOOD_GROUPS,
            "components": COMPONENTS,
            "areas": list(AREAS),
            "inbound_number": s.bolna_inbound_number,
        },
    )


def _spawn(request: Request, coro) -> None:
    task = asyncio.create_task(coro)
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)


def _summary(s: Search) -> dict[str, Any]:
    return {
        "id": s.id, "created_at": s.created_at, "status": s.status, "source": s.source,
        "caller": mask_number(s.caller), "blood_group": s.request.blood_group, "units": s.request.units,
        "area": s.request.area, "callback_status": s.callback_status, "sms_status": s.sms_status,
        "chosen": next((c.facility.name for c in s.calls if c.id == s.chosen_call_id), None),
    }


@app.get("/searches")
async def list_searches(limit: int = 10) -> list[dict[str, Any]]:
    """Most recent searches first, for the dashboard's incoming-calls panel. Caller numbers are masked."""
    recent = sorted(store.searches.values(), key=lambda s: s.created_at, reverse=True)
    return [_summary(s) for s in recent[: max(1, min(limit, 50))]]


@app.post("/searches")
async def create_search(req: BloodRequest, request: Request) -> dict[str, str]:
    from .guard import client_key, get_guard

    guard = get_guard(request.app.state)
    refused = guard.admit(client_key(request, request.app.state.settings.trust_forwarded_for))
    if refused:
        raise HTTPException(429, refused)
    search = store.add(Search(request=req))
    task = asyncio.create_task(request.app.state.orch.run(search.id))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)
    task.add_done_callback(lambda _t: guard.release())
    return {"id": search.id}


@app.get("/searches/{search_id}")
async def get_search(search_id: str) -> Search:
    search = store.get(search_id)
    if search is None:
        raise HTTPException(404, "unknown search")
    return search


@app.get("/searches/{search_id}/gopack")
async def get_gopack(search_id: str) -> GoPack:
    """Family handoff for a finished search: where to go, what to bring, a shareable message."""
    search = store.get(search_id)
    if search is None:
        raise HTTPException(404, "unknown search")
    if search.status not in ("done", "failed"):
        raise HTTPException(409, f"search is still {search.status}")
    return build_gopack(search)


@app.get("/searches/{search_id}/stream")
async def stream(search_id: str, request: Request):
    search = store.get(search_id)
    if search is None:
        raise HTTPException(404, "unknown search")
    q = store.subscribe(search_id)

    async def events():
        try:
            yield f"data: {search.model_dump_json()}\n\n"
            while not await request.is_disconnected():
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {snap}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.unsubscribe(search_id, q)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/webhooks/bolna")
async def bolna_webhook(payload: dict[str, Any], request: Request, token: str = "") -> dict[str, Any]:
    """Bolna's webhook for every agent (set as webhook_url; needs a public URL).

    Blood bank calls: cached so the orchestrator polls less. Intake calls (the family phoned us):
    a finished call becomes a phone search, and the family is called back when it ends."""
    s = request.app.state.settings
    if s.webhook_token and token != s.webhook_token:
        raise HTTPException(403, "bad webhook token")
    provider = request.app.state.provider
    if isinstance(provider, BolnaProvider):
        provider.ingest_webhook(payload)
    record = payload["data"] if not payload.get("status") and isinstance(payload.get("data"), dict) else payload
    intake: PhoneIntake = request.app.state.intake
    search = intake.accept(record)
    if search is None:
        return {"ok": True}
    if search.status != "failed":
        _spawn(request, intake.run(search.id))
    return {"ok": True, "search_id": search.id}


class SimulatedIntake(BaseModel):
    """A pretend finished intake call, for demos without an inbound number (POST /intake/simulate)."""

    caller: str = "+919999900001"
    blood_group: str = "O-"
    component: str = "prbc"
    units: int = 2
    area: str = "HSR Layout"
    hospital: str = ""
    language: str = "en"
    transcript: str = ""  # if given, extractions are left empty and the transcript is parsed instead


@app.post("/intake/simulate")
async def simulate_intake(body: SimulatedIntake, request: Request) -> dict[str, Any]:
    if request.app.state.settings.live_mode:
        raise HTTPException(403, "simulated intake is disabled in LIVE_MODE (it would place a real callback)")
    exec_id = f"sim-intake-{uuid.uuid4().hex[:10]}"
    if body.transcript:
        extracted: dict[str, Any] = {}
    else:
        extracted = {"Blood request": {
            "blood_group": {"objective": body.blood_group}, "component": {"objective": body.component},
            "units": {"subjective": str(body.units)}, "area": {"subjective": body.area},
            "hospital": {"subjective": body.hospital}, "language": {"objective": body.language},
            "callback_number": {"subjective": ""},
        }}
    payload = {
        "id": exec_id, "status": "completed", "agent_id": request.app.state.settings.bolna_intake_agent_id,
        "user_number": body.caller, "transcript": body.transcript, "extracted_data": extracted,
        "telephony_data": {"call_type": "inbound", "from_number": body.caller},
    }
    intake: PhoneIntake = request.app.state.intake
    search = intake.accept(payload)
    if search is None:
        raise HTTPException(409, "duplicate intake")
    if search.status != "failed":
        _spawn(request, intake.run(search.id))
    return {"id": search.id, "status": search.status, "reason": search.reason}
