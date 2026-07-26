package worker

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/queue"
	"github.com/JamesConway98/RunBox/runner/internal/store"
)

// popTimeout bounds each blocking pop so that shutdown is responsive. An
// indefinite BRPOP cannot be interrupted by context cancellation, which would
// mean shutdown hangs until work happens to arrive.
const popTimeout = 2 * time.Second

// Pool is a fixed set of workers pulling from Redis, with Postgres as the
// fallback source.
//
// Fixed rather than elastic on purpose: the binding constraint is host memory
// and Docker's ability to create containers, neither of which gets better by
// spawning more goroutines. A bounded pool with a queue in front is the honest
// shape of this workload.
type Pool struct {
	size     int
	queue    *queue.Queue
	store    *store.Store
	executor *Executor
	log      *slog.Logger

	// inflight tracks running executions so shutdown can wait for them and
	// cancellation can reach them.
	mu       sync.Mutex
	inflight map[string]context.CancelFunc
}

func NewPool(
	size int, q *queue.Queue, st *store.Store, ex *Executor, log *slog.Logger,
) *Pool {
	return &Pool{
		size:     size,
		queue:    q,
		store:    st,
		executor: ex,
		log:      log,
		inflight: make(map[string]context.CancelFunc),
	}
}

// Run blocks until ctx is cancelled, then waits for in-flight runs to finish.
//
// The wait is deliberate. Killing a container mid-run on deploy would leave a
// run stuck in `running` for the reaper to find, when the graceful path costs
// only the remaining seconds of work.
func (p *Pool) Run(ctx context.Context) {
	var wg sync.WaitGroup
	for i := 0; i < p.size; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			p.worker(ctx, id)
		}(i)
	}

	<-ctx.Done()
	p.log.Info("shutdown signalled, draining", "inflight", p.Inflight())
	wg.Wait()
	p.log.Info("all workers drained")
}

// Inflight reports how many runs are currently executing.
func (p *Pool) Inflight() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.inflight)
}

// Cancel stops a specific in-flight run. Returns false if this process is not
// the one executing it.
func (p *Pool) Cancel(runID string) bool {
	p.mu.Lock()
	cancel, ok := p.inflight[runID]
	p.mu.Unlock()
	if ok {
		p.log.Info("cancelling run", "run_id", runID)
		cancel()
	}
	return ok
}

func (p *Pool) worker(ctx context.Context, id int) {
	log := p.log.With("worker", id)
	log.Debug("worker started")

	for {
		if ctx.Err() != nil {
			log.Debug("worker stopping")
			return
		}

		run, err := p.next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			log.Error("could not fetch work", "err", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
			}
			continue
		}
		if run == nil {
			continue // nothing waiting; loop back and block again
		}

		p.execute(ctx, run, log)
	}
}

// next returns the next run to execute, or nil if there is nothing waiting.
//
// Redis first for latency, Postgres as the fallback. The fallback is not
// redundancy theatre: it is what makes a lost queue entry recoverable, and it
// is the only reason the Redis list is acceptable as a queue at all.
func (p *Pool) next(ctx context.Context) (*store.Run, error) {
	runID, err := p.queue.Pop(ctx, popTimeout)
	switch {
	case err == nil:
		// The queue only carries an id. Claiming the row is what actually
		// grants ownership, and the status guard is what makes a duplicate
		// delivery harmless.
		claimed, err := p.store.MarkRunning(ctx, runID)
		if err != nil {
			return nil, err
		}
		if !claimed {
			p.log.Debug("queue entry already claimed", "run_id", runID)
			return nil, nil
		}
		return p.store.Get(ctx, runID)

	case errors.Is(err, queue.ErrEmpty):
		// Nothing in Redis. Sweep Postgres for anything whose queue entry was
		// lost, then go back to blocking.
		run, err := p.store.Claim(ctx)
		if errors.Is(err, store.ErrNoWork) {
			return nil, nil
		}
		return run, err

	default:
		return nil, err
	}
}

func (p *Pool) execute(ctx context.Context, run *store.Run, log *slog.Logger) {
	// Each run gets a cancellable child of the pool context, registered so that
	// Cancel can reach it by id.
	runCtx, cancel := context.WithCancel(ctx)

	p.mu.Lock()
	p.inflight[run.ID] = cancel
	p.mu.Unlock()

	defer func() {
		p.mu.Lock()
		delete(p.inflight, run.ID)
		p.mu.Unlock()
		cancel()
	}()

	if err := p.executor.Execute(runCtx, run); err != nil {
		log.Error("execute failed", "run_id", run.ID, "err", err)
	}
}
