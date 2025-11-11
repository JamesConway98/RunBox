from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from .. import parsing, quotas
from ..auth import Principal, authenticate, get_db
from ..bus import Bus
from ..db import Database
from ..schemas import Page

logger = logging.getLogger("runbox.datasets")

router = APIRouter(prefix="/v1", tags=["datasets"])

# 8 MB. Ten thousand cases of ~500 characters is around 5 MB, so this leaves
# headroom without letting the endpoint become a file host.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


async def get_bus(request) -> Bus:
    return request.app.state.bus


class Dataset(BaseModel):
    id: str
    name: str
    description: str | None
    case_count: int
    created_at: datetime


class DatasetCase(BaseModel):
    id: int
    idx: int
    input: str
    expected: str | None
    metadata: dict[str, Any]


class CreateBatchRequest(BaseModel):
    dataset_id: str
    name: str = Field(min_length=1, max_length=200)
    # {{input}} is substituted per case. A template rather than a bare prompt so
    # the same dataset can be used for several different questions.
    prompt_template: str = Field(default="{{input}}", max_length=20_000)
    models: list[str] = Field(min_length=1, max_length=6)
    tools: list[str] = Field(default_factory=list, max_length=8)
    timeout_s: int = Field(default=120, ge=1, le=600)
    max_tokens: int = Field(default=8_000, ge=256, le=200_000)


class Batch(BaseModel):
    id: str
    name: str
    dataset_id: str
    models: list[str]
    status: str
    total_runs: int
    created_at: datetime
    finished_at: datetime | None = None


class BatchProgress(BaseModel):
    total: int
    completed: int
    failed: int
    in_flight: int
    cost_micros: int

    @property
    def done(self) -> int:
        return self.completed + self.failed


class BatchDetail(Batch):
    progress: BatchProgress


@router.post("/datasets", response_model=Dataset, status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> Dataset:
    """Upload a JSONL or CSV of test cases."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "file_too_large",
                "message": f"Files must be under {MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
            },
        )

    try:
        cases = parsing.parse(content, file.filename or "upload.jsonl")
    except parsing.ParseError as exc:
        # A 400 with the line number, not a 500 with a stack trace.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_dataset", "message": str(exc)},
        ) from exc

    dataset_name = name.strip() or (file.filename or "Untitled dataset")

    async with db.acquire(principal.tenant_id) as conn:
        row = await conn.fetchrow(
            """
            insert into datasets (tenant_id, name, description, case_count)
            values ($1, $2, nullif($3, ''), $4)
            returning id, name, description, case_count, created_at
            """,
            principal.tenant_id,
            dataset_name,
            description.strip(),
            len(cases),
        )

        # copy_records_to_table rather than 10,000 inserts. The difference at
        # this size is seconds, not milliseconds.
        await conn.copy_records_to_table(
            "dataset_cases",
            columns=["dataset_id", "tenant_id", "idx", "input", "expected", "metadata"],
            records=[
                (row["id"], principal.tenant_id, c.idx, c.input, c.expected, c.metadata)
                for c in cases
            ],
        )

    return Dataset(**{**dict(row), "id": str(row["id"])})


@router.get("/datasets", response_model=Page[Dataset])
async def list_datasets(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
) -> Page[Dataset]:
    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select id, name, description, case_count, created_at
            from datasets where tenant_id = $1
            order by created_at desc limit $2
            """,
            principal.tenant_id,
            limit,
        )
    return Page[Dataset](
        data=[Dataset(**{**dict(r), "id": str(r["id"])}) for r in rows],
        has_more=False,
        next_cursor=None,
    )


@router.get("/datasets/{dataset_id}/cases", response_model=Page[DatasetCase])
async def list_cases(
    dataset_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> Page[DatasetCase]:
    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select id, idx, input, expected, metadata
            from dataset_cases
            where dataset_id = $1 and tenant_id = $2 and idx >= $3
            order by idx limit $4
            """,
            dataset_id,
            principal.tenant_id,
            after,
            limit + 1,
        )

    has_more = len(rows) > limit
    rows = rows[:limit]
    return Page[DatasetCase](
        data=[DatasetCase(**dict(r)) for r in rows],
        has_more=has_more,
        next_cursor=str(rows[-1]["idx"] + 1) if has_more and rows else None,
    )


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    async with db.acquire(principal.tenant_id) as conn:
        result = await conn.execute(
            "delete from datasets where id = $1 and tenant_id = $2",
            dataset_id,
            principal.tenant_id,
        )
    if result.endswith("0"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Dataset does not exist."},
        )


@router.post("/batches", response_model=BatchDetail, status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    body: CreateBatchRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> BatchDetail:
    """Fan a dataset out across the selected models.

    The runs created here are ordinary runs — same table, same runner, same
    trace stream — carrying a foreign key back to the batch. Giving batches
    their own execution path would mean two engines that must not drift apart.
    """
    async with db.acquire(principal.tenant_id) as conn:
        await quotas.enforce(conn, principal.tenant_id)

        dataset = await conn.fetchrow(
            "select id, case_count from datasets where id = $1 and tenant_id = $2",
            body.dataset_id,
            principal.tenant_id,
        )
        if dataset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": "Dataset does not exist."},
            )

        known = await conn.fetch(
            "select model from model_pricing where model = any($1) and active", body.models
        )
        unknown = set(body.models) - {r["model"] for r in known}
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "unknown_model",
                    "message": f"Unknown models: {', '.join(sorted(unknown))}.",
                },
            )

        total = dataset["case_count"] * len(body.models)

        batch = await conn.fetchrow(
            """
            insert into batches
              (tenant_id, dataset_id, name, prompt_template, models, tools, total_runs)
            values ($1, $2, $3, $4, $5, $6, $7)
            returning id, name, dataset_id, models, status, total_runs, created_at, finished_at
            """,
            principal.tenant_id,
            body.dataset_id,
            body.name,
            body.prompt_template,
            body.models,
            body.tools,
            total,
        )

        cases = await conn.fetch(
            "select id, input from dataset_cases where dataset_id = $1 order by idx",
            body.dataset_id,
        )

        # One statement rather than case_count × model_count round trips.
        # unnest turns the arrays into rows server side.
        run_rows = [
            (
                principal.tenant_id,
                body.prompt_template.replace("{{input}}", case["input"]),
                model,
                body.tools,
                body.timeout_s,
                body.max_tokens,
                batch["id"],
                case["id"],
            )
            for case in cases
            for model in body.models
        ]

        created = await conn.fetch(
            """
            insert into runs
              (tenant_id, status, task, model, tools, timeout_s, max_tokens, batch_id, case_id)
            select
              u.tenant_id, 'queued', u.task, u.model, u.tools,
              u.timeout_s, u.max_tokens, u.batch_id, u.case_id
            from unnest($1::uuid[], $2::text[], $3::text[], $4::text[][],
                        $5::int[], $6::int[], $7::uuid[], $8::bigint[])
              as u(tenant_id, task, model, tools, timeout_s, max_tokens, batch_id, case_id)
            returning id
            """,
            *[list(column) for column in zip(*run_rows, strict=True)],
        )

    # Enqueue outside the transaction. Pushing ids for rows that have not
    # committed yet is a race the runner would have to defend against.
    for run in created:
        try:
            await bus.enqueue(str(run["id"]))
        except Exception:  # noqa: BLE001
            logger.exception("batch enqueue failed for run %s", run["id"])

    return BatchDetail(
        **{**dict(batch), "id": str(batch["id"]), "dataset_id": str(batch["dataset_id"])},
        progress=BatchProgress(
            total=len(created), completed=0, failed=0, in_flight=len(created), cost_micros=0
        ),
    )


@router.get("/batches", response_model=Page[Batch])
async def list_batches(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    limit: int = Query(default=25, ge=1, le=100),
) -> Page[Batch]:
    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select id, name, dataset_id, models, status, total_runs, created_at, finished_at
            from batches where tenant_id = $1
            order by created_at desc limit $2
            """,
            principal.tenant_id,
            limit,
        )
    return Page[Batch](
        data=[
            Batch(**{**dict(r), "id": str(r["id"]), "dataset_id": str(r["dataset_id"])})
            for r in rows
        ],
        has_more=False,
        next_cursor=None,
    )


@router.get("/batches/{batch_id}", response_model=BatchDetail)
async def get_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
) -> BatchDetail:
    async with db.acquire(principal.tenant_id) as conn:
        row = await conn.fetchrow(
            """
            select id, name, dataset_id, models, status, total_runs, created_at, finished_at
            from batches where id = $1 and tenant_id = $2
            """,
            batch_id,
            principal.tenant_id,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": "Batch does not exist."},
            )

        # Progress is derived, never stored. A counter column would need a write
        # from the runner on every completion and would drift the moment
        # anything was retried.
        progress = await conn.fetchrow("select * from runbox_batch_progress($1)", batch_id)

        # Mark a finished batch finished, once. The guard makes it idempotent
        # under concurrent readers.
        if row["status"] == "running" and progress["in_flight"] == 0 and progress["total"] > 0:
            await conn.execute(
                """
                update batches set status = 'completed', finished_at = now()
                where id = $1 and status = 'running'
                """,
                batch_id,
            )
            row = dict(row) | {"status": "completed"}

    data = dict(row)
    return BatchDetail(
        **{**data, "id": str(data["id"]), "dataset_id": str(data["dataset_id"])},
        progress=BatchProgress(**dict(progress)),
    )


@router.post("/batches/{batch_id}/cancel", response_model=BatchDetail)
async def cancel_batch(
    batch_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> BatchDetail:
    async with db.acquire(principal.tenant_id) as conn:
        owned = await conn.fetchval(
            "select 1 from batches where id = $1 and tenant_id = $2", batch_id, principal.tenant_id
        )
        if not owned:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": "Batch does not exist."},
            )

        # Queued runs can be cancelled outright; running ones need the runner to
        # notice. Doing the queued ones here means a cancelled batch stops
        # consuming quota immediately rather than after the queue drains.
        await conn.execute(
            """
            update runs set status = 'cancelled', finished_at = now(), duration_ms = 0
            where batch_id = $1 and status = 'queued'
            """,
            batch_id,
        )
        running = await conn.fetch(
            "select id from runs where batch_id = $1 and status = 'running'", batch_id
        )
        await conn.execute(
            "update batches set status = 'cancelled', finished_at = now() where id = $1",
            batch_id,
        )

    for run in running:
        await bus.request_cancel(str(run["id"]))

    return await get_batch(batch_id, principal, db)
