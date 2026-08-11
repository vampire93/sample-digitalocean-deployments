from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.errors import register_error_handlers
from app.api.routes import router
from app.core import db
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, request_id_ctx
from app.services.events import broker
from app.workers.runner import worker

configure_logging(settings.log_level, pretty=settings.env == "local")
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.connect()
    await db.run_migrations()
    await broker.start()
    if settings.worker_enabled:
        await worker.start()
    log.info("app.started", env=settings.env, worker_enabled=settings.worker_enabled)
    try:
        yield
    finally:
        if settings.worker_enabled:
            await worker.stop()
        await broker.stop()
        await db.disconnect()
        log.info("app.stopped")


app = FastAPI(
    title="Job Orchestrator",
    version="1.0.0",
    description="Async job orchestration with a live operations dashboard.",
    lifespan=lifespan,
)

# In production the SPA is served from the same origin (App Platform routes /api to
# this service and / to the static site), so CORS is a local-development affordance only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request_id_ctx.set(rid)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request.failed", method=request.method, path=request.url.path)
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    # SSE streams never "complete" in a useful sense; logging them at open is enough.
    log.info(
        "request.completed",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Request-ID"] = rid
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Liveness: deliberately dumb.

    If this touched the database, a brief DB blip would make the platform kill and
    restart otherwise-healthy containers, turning a small problem into an outage.
    """
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Readiness: may this instance receive traffic? That does depend on the DB."""
    try:
        await db.pool().fetchval("SELECT 1")
    except Exception as exc:
        log.warning("readiness.failed", error=str(exc))
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready", "sse_subscribers": broker.subscriber_count}
