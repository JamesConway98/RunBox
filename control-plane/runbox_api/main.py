from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .routers import runs

logger = logging.getLogger("runbox")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    db = Database(settings)
    await db.connect()
    app.state.db = db
    app.state.settings = settings
    logger.info("control plane ready")

    try:
        yield
    finally:
        await db.disconnect()


app = FastAPI(
    title="Runbox API",
    version="0.1.0",
    description=(
        "Sandboxed execution and observability for LLM agents. "
        "Authenticate with `Authorization: Bearer rb_live_...`."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    # The SSE cursor header has to be readable by the browser client or it
    # cannot resume from where it dropped.
    expose_headers=["Location", "X-Runbox-Last-Seq"],
)

app.include_router(runs.router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return validation failures in the same envelope as every other error.

    FastAPI's default 422 body has a different shape to our errors, which means
    a client needs two parsers. One shape is worth the handler.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "message": "The request body or query parameters are invalid.",
            "detail": {"issues": _summarise(exc.errors())},
        },
    )


def _summarise(errors: list[dict]) -> list[dict]:
    out = []
    for err in errors[:10]:
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        out.append({"field": location or "body", "problem": err.get("msg", "invalid")})
    return out


@app.get("/health", tags=["meta"], include_in_schema=False)
async def health(request: Request) -> dict:
    """Liveness plus a real database round trip.

    A health check that does not touch its dependencies reports healthy right
    up until someone tries to use the service.
    """
    db: Database = request.app.state.db
    try:
        async with db.acquire_admin() as conn:
            await conn.fetchval("select 1")
    except Exception as exc:  # noqa: BLE001
        logger.exception("health check failed")
        return JSONResponse(
            status_code=503, content={"status": "degraded", "database": str(exc)}
        )
    return {"status": "ok", "database": "ok"}
