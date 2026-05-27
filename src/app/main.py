import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, DATABASE_URL, init_db
from app.core.models import RunLog
from app.api import applications, candidates, credentials, form_answers, jobs, unanswered

try:
    from app.harvester.routers import harvesters
    _HAS_HARVESTERS = True
except ImportError:
    harvesters = None  # type: ignore[assignment]
    _HAS_HARVESTERS = False

try:
    from app.email.routers import outlook
    _HAS_OUTLOOK = True
except ImportError:
    outlook = None  # type: ignore[assignment]
    _HAS_OUTLOOK = False

try:
    from app.email.routers import outlook_demo
    _HAS_OUTLOOK_DEMO = True
except ImportError:
    outlook_demo = None  # type: ignore[assignment]
    _HAS_OUTLOOK_DEMO = False

# Comma-separated list of modules to enable, e.g. "harvester,candidates"
# Defaults to "all" — every router is mounted.
_ENABLED = {
    m.strip().lower()
    for m in os.getenv("ENABLED_MODULES", "all").split(",")
    if m.strip()
}


def _cors_origins() -> list[str]:
    """Read allowed origins from CORS_ORIGINS env var (comma-separated).

    NOTE: browsers block credentials (cookies/auth headers) when the server
    responds with Access-Control-Allow-Origin: *.  Always list explicit
    origins here instead of using a wildcard if the frontend sends credentials.
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        # Fallback for local dev when env var is not set
        return [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
        ]
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(modules: set[str] | None = None) -> FastAPI:
    """Factory used by standalone entry points and tests.

    Pass a set of module names (e.g. ``{"harvester"}``) to mount only those
    routers, or ``None`` to honour the ``ENABLED_MODULES`` env var (default
    behaviour when the full monolith is started via ``run_backend.py``).
    """
    active = modules if modules is not None else _ENABLED

    def include(name: str) -> bool:
        return "all" in active or name in active

    @asynccontextmanager
    async def _lifespan(a: FastAPI):
        await init_db()
        if include("harvester") and _HAS_HARVESTERS:
            asyncio.create_task(harvesters.scheduler_loop())
        yield

    _app = FastAPI(title="Job Applier API", version="1.0.0", lifespan=_lifespan)

    _app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if include("candidates"):
        _app.include_router(candidates.router, prefix="/api/candidates", tags=["candidates"])
    if include("jobs"):
        _app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    if include("applications"):
        _app.include_router(applications.router, prefix="/api/applications", tags=["applications"])
    if include("unanswered"):
        _app.include_router(unanswered.router, prefix="/api/unanswered-questions", tags=["unanswered"])
    if include("credentials"):
        _app.include_router(credentials.router, prefix="/api/credentials", tags=["credentials"])
    if include("form-answers"):
        _app.include_router(form_answers.router, prefix="/api/form-answers", tags=["form-answers"])
    if include("harvester") and _HAS_HARVESTERS:
        _app.include_router(harvesters.router, prefix="/api/harvesters", tags=["harvesters"])
    if include("outlook") and _HAS_OUTLOOK:
        _app.include_router(outlook.router, prefix="/api/outlook", tags=["outlook"])
    if include("outlook-demo") and _HAS_OUTLOOK_DEMO:
        _app.include_router(outlook_demo.router, prefix="/api/outlook-demo", tags=["outlook-demo"])

    @_app.get("/health", tags=["meta"])
    async def health():
        active_list = sorted(active)
        return {"status": "ok", "modules": active_list}

    return _app


app = create_app()


@app.websocket("/ws/logs/{application_id}")
async def ws_logs(websocket: WebSocket, application_id: str):
    try:
        app_uuid = uuid.UUID(application_id)
    except ValueError:
        await websocket.close(code=4000)
        return

    await websocket.accept()

    # asyncpg needs the plain postgresql:// scheme, not postgresql+asyncpg://
    asyncpg_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_url)
    notify_queue: asyncio.Queue = asyncio.Queue()

    async def on_notify(_conn, _pid, _channel, payload: str) -> None:
        await notify_queue.put(payload)

    channel = f"app_logs_{application_id}"
    await conn.add_listener(channel, on_notify)

    try:
        # Replay historical logs so the client sees the full run on connect
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(RunLog)
                .where(RunLog.application_id == app_uuid)
                .order_by(RunLog.created_at)
            )
            for log in result.scalars():
                await websocket.send_json({
                    "level": log.log_level,
                    "message": log.message,
                    "created_at": log.created_at.isoformat(),
                })

        # Stream live notifications; send a heartbeat every 25 s to keep the
        # connection alive through proxies that have short idle timeouts.
        while True:
            try:
                payload = await asyncio.wait_for(notify_queue.get(), timeout=25)
                await websocket.send_text(payload)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await conn.remove_listener(channel, on_notify)
            await conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)