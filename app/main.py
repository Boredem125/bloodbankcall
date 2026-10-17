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

from .config import get_settings
from .decision import build_engine
from .facilities import AREAS, load_facilities
from .gopack import GoPack, build_gopack
from .orchestrator import Orchestrator
from .providers import BolnaProvider, build_provider
from .schemas import BLOOD_GROUPS, COMPONENTS, BloodRequest, Search
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
        },
    )


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
async def bolna_webhook(payload: dict[str, Any], request: Request) -> dict[str, bool]:
    """Optional: set as the agent's webhook_url (needs a public tunnel). Polling works without it."""
    provider = request.app.state.provider
    if isinstance(provider, BolnaProvider):
        provider.ingest_webhook(payload)
    return {"ok": True}
