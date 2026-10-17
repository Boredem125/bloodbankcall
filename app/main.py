from __future__ import annotations

import asyncio
import logging
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
from .orchestrator import Orchestrator
from .providers import BolnaProvider, build_provider
from .schemas import CATEGORIES, Errand, ErrandRequest
from .store import store
from .vendors import load_vendors

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.settings = s
    app.state.provider = build_provider(s)
    app.state.engine = build_engine(s)
    app.state.orch = Orchestrator(s, app.state.provider, app.state.engine, store, load_vendors(s.vendors_file))
    app.state.tasks = set()
    yield


app = FastAPI(title="Phonewala", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _orch(request: Request) -> Orchestrator:
    return request.app.state.orch


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    s = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "live": s.live_mode,
            "backend": request.app.state.engine.primary,
            "chain": [e.name for e in request.app.state.engine.chain],
            "categories": CATEGORIES,
        },
    )


@app.post("/errands")
async def create_errand(req: ErrandRequest, request: Request) -> dict[str, str]:
    errand = store.add(Errand(request=req))
    task = asyncio.create_task(_orch(request).run(errand.id))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)
    return {"id": errand.id}


@app.get("/errands/{errand_id}")
async def get_errand(errand_id: str) -> Errand:
    errand = store.get(errand_id)
    if errand is None:
        raise HTTPException(404, "unknown errand")
    return errand


@app.get("/errands/{errand_id}/stream")
async def stream(errand_id: str, request: Request):
    errand = store.get(errand_id)
    if errand is None:
        raise HTTPException(404, "unknown errand")
    q = store.subscribe(errand_id)

    async def events():
        try:
            yield f"data: {errand.model_dump_json()}\n\n"
            while not await request.is_disconnected():
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {snap}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.unsubscribe(errand_id, q)

    return StreamingResponse(events(), media_type="text/event-stream")


class Approve(BaseModel):
    call_id: str | None = None


@app.post("/errands/{errand_id}/approve")
async def approve(errand_id: str, body: Approve, request: Request) -> dict[str, str]:
    try:
        await _orch(request).approve(errand_id, body.call_id)
    except (ValueError, StopIteration) as exc:
        raise HTTPException(409, str(exc) or "invalid call id") from exc
    return {"status": "booking"}


@app.post("/webhooks/bolna")
async def bolna_webhook(payload: dict[str, Any], request: Request) -> dict[str, bool]:
    """Optional: set as the agent's webhook_url (needs a public tunnel). Polling works without it."""
    provider = request.app.state.provider
    if isinstance(provider, BolnaProvider):
        provider.ingest_webhook(payload)
    return {"ok": True}


class NegotiateIn(BaseModel):
    errand_id: str
    quoted_price: int


@app.post("/tools/negotiate")
async def negotiate(body: NegotiateIn) -> dict[str, Any]:
    """In-call tool (stretch): Bolna custom function posts the live quote, gets a counter-offer line back."""
    errand = store.get(body.errand_id)
    if errand is None:
        raise HTTPException(404, "unknown errand")
    best = min((c.price for c in errand.calls if c.viable and c.price), default=None)
    budget = errand.request.budget
    target = min(x for x in (best, budget) if x is not None)
    if body.quoted_price <= target:
        return {"action": "accept", "message": "That works. Please confirm the slot."}
    return {
        "action": "counter",
        "counter_price": target,
        "message": f"Another service quoted {target} rupees. Can you match {target}?",
    }
