package worker

import (
	"context"
	"log/slog"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/queue"
)

// CancelWatcher polls the cancel set and stops matching in-flight runs.
//
// Polling rather than pub/sub, which looks like the lazier choice and is
// actually the correct one. A cancel published while this process is
// reconnecting is simply gone; a cancel sitting in a set is still there on the
// next tick. Cancellation is the one operation where an extra second of latency
// costs nothing and a lost message costs a container running to its timeout.
type CancelWatcher struct {
	queue    *queue.Queue
	pool     *Pool
	interval time.Duration
	log      *slog.Logger
}

func NewCancelWatcher(q *queue.Queue, pool *Pool, log *slog.Logger) *CancelWatcher {
	return &CancelWatcher{queue: q, pool: pool, interval: time.Second, log: log}
}

func (w *CancelWatcher) Run(ctx context.Context) {
	ticker := time.NewTicker(w.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			w.sweep(ctx)
		}
	}
}

func (w *CancelWatcher) sweep(ctx context.Context) {
	w.pool.mu.Lock()
	ids := make([]string, 0, len(w.pool.inflight))
	for id := range w.pool.inflight {
		ids = append(ids, id)
	}
	w.pool.mu.Unlock()

	// Only ask about runs this process is actually executing. Scanning the
	// whole set would mean every runner in a fleet doing work proportional to
	// every other runner's cancellations.
	for _, id := range ids {
		requested, err := w.queue.CancelRequested(ctx, id)
		if err != nil {
			if ctx.Err() == nil {
				w.log.Warn("cancel check failed", "run_id", id, "err", err)
			}
			return
		}
		if !requested {
			continue
		}
		if w.pool.Cancel(id) {
			if err := w.queue.ClearCancel(ctx, id); err != nil {
				w.log.Warn("could not clear cancel flag", "run_id", id, "err", err)
			}
		}
	}
}
