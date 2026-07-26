package worker

import (
	"context"
	"log/slog"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/store"
)

// Reaper finds runs that a dead runner left behind.
//
// The failure this exists for: a runner is killed mid-run — OOM, a deploy, a
// host reboot — and its runs sit in `running` forever. Nothing else in the
// system will ever touch them, because the worker that owned them is gone. To a
// user they are a spinner that never resolves, and to the metering system they
// are a run that consumed compute and was never billed.
//
// A lease would be the more precise mechanism: heartbeat a claim, reap anything
// whose lease expired. That is the right answer with several runners. With one
// runner process, a deadline generous enough to exceed the maximum run timeout
// gets the same outcome for a fraction of the machinery, and the trade is worth
// naming rather than hiding.
type Reaper struct {
	store    *store.Store
	interval time.Duration
	grace    time.Duration
	log      *slog.Logger
}

func NewReaper(st *store.Store, maxTimeout time.Duration, log *slog.Logger) *Reaper {
	// The grace period must comfortably exceed the longest a legitimate run can
	// take, or the reaper starts killing healthy work. Double the ceiling plus
	// five minutes is deliberately generous: reaping late costs a stale row for
	// a few minutes, reaping early destroys a run someone is watching.
	grace := 2*maxTimeout + 5*time.Minute

	return &Reaper{
		store:    st,
		interval: 60 * time.Second,
		grace:    grace,
		log:      log,
	}
}

// Run sweeps on startup and then on an interval.
//
// The startup sweep is the important one: the most likely reason runs are
// orphaned is that this process just restarted after dying.
func (r *Reaper) Run(ctx context.Context) {
	r.sweep(ctx)

	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.sweep(ctx)
		}
	}
}

func (r *Reaper) sweep(ctx context.Context) {
	reaped, err := r.store.ReapStaleRuns(ctx, r.grace)
	if err != nil {
		if ctx.Err() == nil {
			r.log.Error("reaper sweep failed", "err", err)
		}
		return
	}
	if reaped > 0 {
		// Loud on purpose. Every reaped run is a run that failed invisibly, and
		// a silent count would hide a recurring crash.
		r.log.Warn("reaped orphaned runs",
			"count", reaped, "older_than", r.grace)
	}
}
