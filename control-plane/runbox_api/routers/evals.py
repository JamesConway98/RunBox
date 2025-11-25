from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import scoring
from ..auth import Principal, authenticate, get_db
from ..bus import Bus
from ..db import Database
from ..schemas import Page

logger = logging.getLogger("runbox.evals")

router = APIRouter(prefix="/v1/evals", tags=["evals"])


async def get_bus(request: Request) -> Bus:
    return request.app.state.bus


class ScoreBatchRequest(BaseModel):
    batch_id: str
    scorer: str
    config: dict[str, Any] = Field(default_factory=dict)
    # Only used by llm_judge. A cheap model is the right default: the judge runs
    # once per case per model, so it is the most-invoked model in the system.
    judge_model: str = Field(default="claude-haiku-4-5")


class EvalScore(BaseModel):
    run_id: str
    model: str
    scorer: str
    passed: bool
    score: float
    detail: str | None
    judge_run_id: str | None = None


class ScoreSummary(BaseModel):
    model: str
    scorer: str
    total: int
    passed: int
    pass_rate: float
    avg_score: float
    avg_latency_ms: float | None
    cost_micros: int


class ScoreBatchResponse(BaseModel):
    scored: int
    skipped: int
    judge_runs_queued: int
    summary: list[ScoreSummary]


@router.get("/scorers")
async def list_scorers() -> dict[str, Any]:
    """What can score, and what each one needs.

    Public: knowing the scorer names is not privileged, and a client that has
    to guess them will guess wrong.
    """
    return {
        "scorers": [
            {
                "name": "exact_match",
                "description": "Output equals the expected value after Unicode normalisation.",
                "config": {"strict": "bool — compare byte-for-byte instead"},
                "requires_expected": True,
            },
            {
                "name": "contains",
                "description": "Output contains the expected value, or a configured string.",
                "config": {"value": "str — override", "case_sensitive": "bool"},
                "requires_expected": False,
            },
            {
                "name": "regex",
                "description": "Output matches a pattern.",
                "config": {"pattern": "str — required", "case_sensitive": "bool"},
                "requires_expected": False,
            },
            {
                "name": "latency",
                "description": "Run finished inside a threshold. Scored on a ramp, not a cliff.",
                "config": {"threshold_ms": "int — required"},
                "requires_expected": False,
            },
            {
                "name": "llm_judge",
                "description": (
                    "A model grades the output against the reference. Implemented as a "
                    "Runbox run, so judging is traced, metered and cancellable."
                ),
                "config": {"judge_model": "str — defaults to claude-haiku-4-5"},
                "requires_expected": True,
            },
        ]
    }


@router.post("/score", response_model=ScoreBatchResponse)
async def score_batch(
    body: ScoreBatchRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> ScoreBatchResponse:
    """Score every succeeded run in a batch."""
    if body.scorer not in scoring.ALL_SCORERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_scorer",
                "message": f"Unknown scorer. Available: {', '.join(scoring.ALL_SCORERS)}.",
            },
        )

    async with db.acquire(principal.tenant_id) as conn:
        owned = await conn.fetchval(
            "select 1 from batches where id = $1 and tenant_id = $2",
            body.batch_id,
            principal.tenant_id,
        )
        if not owned:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": "Batch does not exist."},
            )

        # Only succeeded runs are scorable. A failed run has no output to grade,
        # and scoring it as a fail would conflate "the model got it wrong" with
        # "the container died" — two very different findings.
        runs = await conn.fetch(
            """
            select r.id, r.model, r.result, r.duration_ms,
                   c.input as case_input, c.expected as case_expected
            from runs r
            left join dataset_cases c on c.id = r.case_id
            where r.batch_id = $1 and r.tenant_id = $2 and r.status = 'succeeded'
            order by r.created_at
            """,
            body.batch_id,
            principal.tenant_id,
        )

        if body.scorer == scoring.LLM_JUDGE:
            queued = await _queue_judge_runs(conn, bus, principal, body, runs)
            summary = await _summarise(conn, body.batch_id)
            return ScoreBatchResponse(
                scored=0, skipped=len(runs) - queued, judge_runs_queued=queued, summary=summary
            )

        rows: list[tuple] = []
        skipped = 0
        for run in runs:
            config = dict(body.config)
            # The latency scorer needs a fact about the run, not about the case.
            # Threading it through config keeps every scorer a pure function of
            # its arguments.
            config.setdefault("duration_ms", run["duration_ms"])

            try:
                result = scoring.score(
                    body.scorer, run["result"] or "", run["case_expected"], config
                )
            except scoring.ScorerError as exc:
                # A misconfigured scorer is a 400 about the request, not a
                # partial result that silently scored some runs and not others.
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_scorer_config", "message": str(exc)},
                ) from exc

            if result.detail == "no expected value on this case":
                skipped += 1
                continue

            rows.append(
                (
                    principal.tenant_id,
                    run["id"],
                    body.batch_id,
                    body.scorer,
                    result.passed,
                    result.score,
                    result.detail,
                )
            )

        if rows:
            # Re-scoring is normal — a threshold gets tuned, a pattern gets
            # fixed — so the same (run, scorer) pair updates rather than
            # colliding.
            await conn.executemany(
                """
                insert into eval_scores
                  (tenant_id, run_id, batch_id, scorer, passed, score, detail)
                values ($1, $2, $3, $4, $5, $6, $7)
                on conflict (run_id, scorer) do update set
                  passed = excluded.passed,
                  score = excluded.score,
                  detail = excluded.detail,
                  created_at = now()
                """,
                rows,
            )

        summary = await _summarise(conn, body.batch_id)

    return ScoreBatchResponse(
        scored=len(rows), skipped=skipped, judge_runs_queued=0, summary=summary
    )


async def _queue_judge_runs(conn, bus: Bus, principal: Principal, body, runs) -> int:
    """Create one judge run per scored run.

    Judging with the platform itself is the point. A judge run is traced,
    metered, cancellable and rate-limited exactly like any other run, and there
    is no second LLM path to secure or to keep in sync.
    """
    queued = 0
    for run in runs:
        if run["case_expected"] is None:
            continue

        prompt = scoring.JUDGE_PROMPT.format(
            input=run["case_input"] or "",
            expected=run["case_expected"],
            output=run["result"] or "",
        )
        judge = await conn.fetchrow(
            """
            insert into runs (tenant_id, status, task, model, tools, timeout_s, max_tokens)
            values ($1, 'queued', $2, $3, '{}', 60, 512)
            returning id
            """,
            principal.tenant_id,
            prompt,
            body.judge_model,
        )

        # The score row is written now, pending, with a pointer to the judge.
        # The alternative — waiting for every judge to finish before recording
        # anything — would mean a request that hangs for minutes.
        await conn.execute(
            """
            insert into eval_scores
              (tenant_id, run_id, batch_id, scorer, passed, score, detail, judge_run_id)
            values ($1, $2, $3, 'llm_judge', false, 0, 'judging…', $4)
            on conflict (run_id, scorer) do update set
              passed = false, score = 0, detail = 'judging…',
              judge_run_id = excluded.judge_run_id, created_at = now()
            """,
            principal.tenant_id,
            run["id"],
            body.batch_id,
            judge["id"],
        )
        await bus.enqueue(str(judge["id"]))
        queued += 1

    return queued


@router.post("/collect/{batch_id}", response_model=ScoreBatchResponse)
async def collect_judgements(
    batch_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
) -> ScoreBatchResponse:
    """Read finished judge runs and turn their answers into scores.

    Separate from scoring because judging is asynchronous. The client polls this
    while the judge runs drain, which is the same shape as watching a batch.
    """
    async with db.acquire(principal.tenant_id) as conn:
        pending = await conn.fetch(
            """
            select s.run_id, j.result
            from eval_scores s
            join runs j on j.id = s.judge_run_id
            where s.batch_id = $1 and s.tenant_id = $2
              and s.scorer = 'llm_judge' and s.detail = 'judging…'
              and j.status = 'succeeded'
            """,
            batch_id,
            principal.tenant_id,
        )

        collected = 0
        for row in pending:
            verdict = scoring.parse_judge_verdict(row["result"] or "")
            await conn.execute(
                """
                update eval_scores set passed = $3, score = $4, detail = $5
                where run_id = $1 and scorer = 'llm_judge' and tenant_id = $2
                """,
                row["run_id"],
                principal.tenant_id,
                verdict.passed,
                verdict.score,
                verdict.detail,
            )
            collected += 1

        summary = await _summarise(conn, batch_id)

    return ScoreBatchResponse(
        scored=collected, skipped=0, judge_runs_queued=0, summary=summary
    )


@router.get("/scores", response_model=Page[EvalScore])
async def list_scores(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    batch_id: str = Query(...),
    scorer: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
) -> Page[EvalScore]:
    conditions = ["s.batch_id = $1", "s.tenant_id = $2"]
    params: list[Any] = [batch_id, principal.tenant_id]
    if scorer:
        params.append(scorer)
        conditions.append(f"s.scorer = ${len(params)}")
    params.append(limit)

    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(
            f"""
            select s.run_id, r.model, s.scorer, s.passed, s.score, s.detail, s.judge_run_id
            from eval_scores s
            join runs r on r.id = s.run_id
            where {" and ".join(conditions)}
            order by s.id
            limit ${len(params)}
            """,
            *params,
        )

    return Page[EvalScore](
        data=[
            EvalScore(
                **{
                    **dict(r),
                    "run_id": str(r["run_id"]),
                    "judge_run_id": str(r["judge_run_id"]) if r["judge_run_id"] else None,
                }
            )
            for r in rows
        ],
        has_more=False,
        next_cursor=None,
    )


async def _summarise(conn, batch_id: str) -> list[ScoreSummary]:
    rows = await conn.fetch("select * from runbox_batch_scores($1)", batch_id)
    return [
        ScoreSummary(
            model=r["model"],
            scorer=r["scorer"],
            total=r["total"],
            passed=r["passed"],
            pass_rate=round(r["passed"] / r["total"], 4) if r["total"] else 0.0,
            avg_score=round(r["avg_score"] or 0.0, 4),
            avg_latency_ms=r["avg_latency_ms"],
            cost_micros=r["cost_micros"],
        )
        for r in rows
    ]
