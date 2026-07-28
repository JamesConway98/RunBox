// Package store is the runner's view of Postgres.
//
// The runner writes trace events and run state; it never reads tenant data it
// was not handed. Claiming work uses SELECT ... FOR UPDATE SKIP LOCKED so that
// two runner processes can safely coexist even though the deployment only runs
// one today.
package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JamesConway98/RunBox/runner/internal/trace"
)

var ErrNoWork = errors.New("no queued runs")

type Run struct {
	ID           string
	TenantID     string
	Task         string
	Model        string
	Tools        []string
	SystemPrompt string
	Temperature  *float64
	TimeoutS     int
	MaxTokens    int
}

type Store struct {
	pool *pgxpool.Pool
}

func Open(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	// The runner's connection count is bounded by its worker pool plus a little
	// headroom for the reaper and health checks.
	cfg.MaxConns = 16
	cfg.MinConns = 2
	cfg.MaxConnLifetime = 30 * time.Minute
	cfg.MaxConnIdleTime = 5 * time.Minute

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) Pool() *pgxpool.Pool { return s.pool }

// Claim atomically takes the oldest queued run and marks it running.
//
// SKIP LOCKED is what makes this safe under concurrency: a worker that finds
// the head row already locked moves to the next one instead of blocking behind
// it.
func (s *Store) Claim(ctx context.Context) (*Run, error) {
	const q = `
		update runs set
			status = 'running',
			started_at = now()
		where id = (
			select id from runs
			where status = 'queued'
			order by created_at
			for update skip locked
			limit 1
		)
		returning id, tenant_id, task, model, tools,
		          coalesce(system_prompt, ''), temperature, timeout_s, max_tokens`

	var r Run
	var temp *float64
	err := s.pool.QueryRow(ctx, q).Scan(
		&r.ID, &r.TenantID, &r.Task, &r.Model, &r.Tools,
		&r.SystemPrompt, &temp, &r.TimeoutS, &r.MaxTokens,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNoWork
	}
	if err != nil {
		return nil, fmt.Errorf("claim: %w", err)
	}
	r.Temperature = temp
	return &r, nil
}

// Get loads a run by id, used when the queue hands over an id directly.
func (s *Store) Get(ctx context.Context, id string) (*Run, error) {
	const q = `
		select id, tenant_id, task, model, tools,
		       coalesce(system_prompt, ''), temperature, timeout_s, max_tokens
		from runs where id = $1`

	var r Run
	var temp *float64
	err := s.pool.QueryRow(ctx, q, id).Scan(
		&r.ID, &r.TenantID, &r.Task, &r.Model, &r.Tools,
		&r.SystemPrompt, &temp, &r.TimeoutS, &r.MaxTokens,
	)
	if err != nil {
		return nil, fmt.Errorf("get run %s: %w", id, err)
	}
	r.Temperature = temp
	return &r, nil
}

// MarkRunning transitions a claimed-by-id run, returning false if some other
// worker got there first. The status guard is the whole point.
func (s *Store) MarkRunning(ctx context.Context, id string) (bool, error) {
	const q = `
		update runs set status = 'running', started_at = now()
		where id = $1 and status = 'queued'`

	tag, err := s.pool.Exec(ctx, q, id)
	if err != nil {
		return false, fmt.Errorf("mark running: %w", err)
	}
	return tag.RowsAffected() == 1, nil
}

// AppendEvents writes a batch of trace events in one round trip.
//
// unnest turns the parallel arrays into rows server side, so a hundred events
// cost one statement rather than a hundred. That matters more than it looks:
// every streamed token is an event, and against a database in another region a
// per-event insert made a normal run spend most of its wall clock waiting on
// the network.
//
// Conflicts on (run_id, seq) are still ignored, so a retried batch cannot put a
// duplicate into a stream clients resume from by cursor.
func (s *Store) AppendEvents(
	ctx context.Context, runID, tenantID string, events []trace.Event,
) error {
	if len(events) == 0 {
		return nil
	}

	seqs := make([]int32, len(events))
	types := make([]string, len(events))
	payloads := make([][]byte, len(events))
	for i, e := range events {
		seqs[i] = int32(e.Seq)
		types[i] = string(e.Type)
		payloads[i] = e.Payload
	}

	const q = `
		insert into trace_events (run_id, tenant_id, seq, type, payload)
		select $1, $2, u.seq, u.type, u.payload
		from unnest($3::int[], $4::text[], $5::jsonb[]) as u(seq, type, payload)
		on conflict (run_id, seq) do nothing`

	if _, err := s.pool.Exec(ctx, q, runID, tenantID, seqs, types, payloads); err != nil {
		return fmt.Errorf("append %d events: %w", len(events), err)
	}
	return nil
}

type Completion struct {
	Status     string
	Result     string
	Error      string
	DurationMS int
}

// Finish writes the terminal state of a run.
func (s *Store) Finish(ctx context.Context, id string, c Completion) error {
	const q = `
		update runs set
			status = $2,
			result = nullif($3, ''),
			error = nullif($4, ''),
			finished_at = now(),
			duration_ms = $5
		where id = $1`

	if _, err := s.pool.Exec(ctx, q, id, c.Status, c.Result, c.Error, c.DurationMS); err != nil {
		return fmt.Errorf("finish run %s: %w", id, err)
	}
	return nil
}

// ReapStaleRuns marks runs abandoned by a dead runner as failed.
//
// Keyed on started_at rather than on a lease column, because with one runner
// process a generous deadline gets the same outcome as leasing for a fraction
// of the machinery. The predicate is deliberately conservative: only runs that
// actually started, and only those older than a grace period that comfortably
// exceeds the maximum permitted run timeout.
//
// Usage rows are written too. A run that consumed compute and was then orphaned
// still consumed it, and a metering system that forgets exactly when something
// went wrong is not one.
func (s *Store) ReapStaleRuns(ctx context.Context, grace time.Duration) (int, error) {
	const q = `
		with reaped as (
			update runs set
				status = 'failed',
				error = 'runner did not finish this run; reaped as orphaned',
				finished_at = now(),
				duration_ms = coalesce(
					extract(milliseconds from (now() - started_at))::integer,
					0
				)
			where status = 'running'
			  and started_at is not null
			  and started_at < now() - $1::interval
			returning id, tenant_id, model, started_at
		)
		insert into usage_records (run_id, tenant_id, model, compute_ms, cost_micros)
		select
			r.id,
			r.tenant_id,
			r.model,
			extract(milliseconds from (now() - r.started_at))::integer,
			0
		from reaped r
		on conflict (run_id) do nothing`

	tag, err := s.pool.Exec(ctx, q, grace)
	if err != nil {
		return 0, fmt.Errorf("reap stale runs: %w", err)
	}
	// RowsAffected counts usage inserts, which can be fewer than the runs
	// reaped when a usage row already existed. Close enough for a log line, and
	// the alternative is a second query for a number nobody acts on.
	return int(tag.RowsAffected()), nil
}
