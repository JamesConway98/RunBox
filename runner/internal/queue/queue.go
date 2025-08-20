// Package queue is the Redis side of the runner: taking work off the list and
// publishing trace events to live subscribers.
//
// A Redis list is not a real job queue — no dead-letter, no visibility timeout,
// no redelivery. That is a known limitation rather than an oversight. What
// makes it acceptable is that the queue is not the system of record: the runs
// table is. An id lost between BRPOP and the status transition is recovered by
// the reaper, which sweeps for runs left queued past their lease.
package queue

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/jamesconway/runbox/runner/internal/trace"
)

const (
	// QueueKey holds run ids awaiting a worker. LPUSH by the control plane,
	// BRPOP by the runner, so it is FIFO.
	QueueKey = "runbox:queue:runs"

	// ChannelPrefix + run id is the pub/sub channel for one run's events.
	ChannelPrefix = "runbox:run:"

	// CancelKey is a set of run ids the control plane wants stopped. The runner
	// checks it rather than being pushed to, because a cancel that arrives
	// while a worker is mid-container has to be observed, not delivered.
	CancelKey = "runbox:cancel"
)

var ErrEmpty = errors.New("queue is empty")

type Queue struct {
	rdb *redis.Client
}

func Open(ctx context.Context, url string) (*Queue, error) {
	opts, err := redis.ParseURL(url)
	if err != nil {
		return nil, fmt.Errorf("parse redis url: %w", err)
	}
	// Blocking pops hold a connection for the whole timeout, so the pool has to
	// be at least as large as the worker count plus the publisher's needs.
	opts.PoolSize = 32
	opts.MinIdleConns = 4

	rdb := redis.NewClient(opts)
	if err := rdb.Ping(ctx).Err(); err != nil {
		rdb.Close()
		return nil, fmt.Errorf("redis ping: %w", err)
	}
	return &Queue{rdb: rdb}, nil
}

func (q *Queue) Close() error { return q.rdb.Close() }

// Pop blocks for up to timeout waiting for a run id.
//
// The timeout matters: an indefinite BRPOP cannot be interrupted by context
// cancellation, so shutdown would hang until work happened to arrive. Polling
// with a short block keeps shutdown responsive at the cost of one round trip
// per interval.
func (q *Queue) Pop(ctx context.Context, timeout time.Duration) (string, error) {
	result, err := q.rdb.BRPop(ctx, timeout, QueueKey).Result()
	switch {
	case errors.Is(err, redis.Nil):
		return "", ErrEmpty
	case err != nil:
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("brpop: %w", err)
	case len(result) != 2:
		return "", fmt.Errorf("brpop returned %d elements", len(result))
	}
	return result[1], nil
}

// Push enqueues a run id. Used by tests and by the reaper when it requeues.
func (q *Queue) Push(ctx context.Context, runID string) error {
	return q.rdb.LPush(ctx, QueueKey, runID).Err()
}

// Depth reports how many runs are waiting, for logging and health.
func (q *Queue) Depth(ctx context.Context) (int64, error) {
	return q.rdb.LLen(ctx, QueueKey).Result()
}

// Publish fans one stamped event out to whoever is streaming this run.
//
// Best effort by design. A dropped publish costs a live subscriber nothing,
// because the event is already durable in Postgres and the client's next
// reconnect replays from its cursor. Failing the run over it would be
// backwards.
func (q *Queue) Publish(ctx context.Context, runID string, event trace.Event) error {
	body, err := json.Marshal(struct {
		Seq     int             `json:"seq"`
		Type    trace.Type      `json:"type"`
		TS      int64           `json:"ts"`
		Payload json.RawMessage `json:"payload"`
	}{event.Seq, event.Type, event.TS, event.Payload})
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}
	return q.rdb.Publish(ctx, ChannelPrefix+runID, body).Err()
}

// CancelRequested reports whether the control plane has asked for this run to
// stop. Checked by the worker between events, which is what makes cancellation
// cooperative rather than a kill.
func (q *Queue) CancelRequested(ctx context.Context, runID string) (bool, error) {
	n, err := q.rdb.SIsMember(ctx, CancelKey, runID).Result()
	if err != nil {
		return false, fmt.Errorf("sismember: %w", err)
	}
	return n, nil
}

// ClearCancel removes a run from the cancel set once it has been acted on, so
// the set does not grow without bound.
func (q *Queue) ClearCancel(ctx context.Context, runID string) error {
	return q.rdb.SRem(ctx, CancelKey, runID).Err()
}
