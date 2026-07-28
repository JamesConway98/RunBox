package worker

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/trace"
)

// Tuning. These are the two numbers that decide whether the runner is
// latency-bound or throughput-bound.
//
// flushInterval is the ceiling on how long an event can sit before a live
// viewer sees it. 50ms is below the threshold where a token stream stops
// looking continuous, and the browser client already coalesces to an animation
// frame (~16ms), so anything finer is thrown away on arrival.
//
// flushSize bounds memory and keeps a single insert reasonable. A fast model
// emits a few hundred tokens a second; 100 rows is well under a second of
// output.
const (
	flushInterval = 50 * time.Millisecond
	flushSize     = 100
)

// traceBuffer batches trace events before writing them.
//
// The original implementation wrote one row and published one message per
// event, synchronously. Against a local Postgres that is sub-millisecond and
// invisible. Against a managed database in another region it is ~115ms per
// event, and since every streamed token is an event, a 400-token run spent
// roughly 46 seconds doing nothing but round trips.
//
// That was a real defect rather than a tuning choice: the design assumed
// co-location and nothing tested otherwise. Batching removes the assumption —
// 400 events become ~8 round trips, which is fine whether the database is on
// localhost or across an ocean.
//
// Ordering is preserved. Events are appended in seq order and flushed in
// slices, so a viewer never sees seq 7 before seq 6.
type traceBuffer struct {
	runID    string
	tenantID string
	store    traceStore
	pub      Publisher
	log      *slog.Logger

	mu      sync.Mutex
	pending []trace.Event
	// failed records the first write error. The scanner loop checks it so a
	// database that has gone away fails the run rather than silently dropping
	// the trace, which is the product.
	failed error

	stop chan struct{}
	done chan struct{}
	// Close is called explicitly before the run resolves and again from a defer
	// covering the error paths. Without this, the second close(b.stop) panics.
	closeOnce sync.Once
}

// traceStore is the slice of the store the buffer needs, named separately so
// this file can be tested without a database.
type traceStore interface {
	AppendEvents(ctx context.Context, runID, tenantID string, events []trace.Event) error
}

func newTraceBuffer(
	runID, tenantID string, st traceStore, pub Publisher, log *slog.Logger,
) *traceBuffer {
	b := &traceBuffer{
		runID:    runID,
		tenantID: tenantID,
		store:    st,
		pub:      pub,
		log:      log,
		pending:  make([]trace.Event, 0, flushSize),
		stop:     make(chan struct{}),
		done:     make(chan struct{}),
	}
	go b.tick()
	return b
}

// tick flushes on a timer, so a quiet stretch does not strand buffered events.
//
// A run can spend forty seconds inside one tool call. Without a timer, the
// tokens emitted just before that call would sit in memory until the call
// returned, and the viewer would watch a stalled screen for no reason.
func (b *traceBuffer) tick() {
	defer close(b.done)
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()

	for {
		select {
		case <-b.stop:
			return
		case <-ticker.C:
			// Background flushes use their own context: the run's context may
			// already be cancelled, and an event produced before a timeout still
			// belongs in the trace.
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			b.flush(ctx)
			cancel()
		}
	}
}

// Add queues an event, flushing early when the buffer is full or the event is
// terminal.
//
// Returns the first write error seen so far, so the caller can abandon a run
// whose trace is not landing.
func (b *traceBuffer) Add(ctx context.Context, event trace.Event) error {
	b.mu.Lock()
	if err := b.failed; err != nil {
		b.mu.Unlock()
		return err
	}
	b.pending = append(b.pending, event)
	full := len(b.pending) >= flushSize
	b.mu.Unlock()

	// The final event is flushed immediately rather than waiting out the timer.
	// The executor reads run state straight after and would otherwise race the
	// ticker.
	if full || event.Type == trace.TypeFinal {
		b.flush(ctx)
	}

	b.mu.Lock()
	defer b.mu.Unlock()
	return b.failed
}

// Close stops the ticker and flushes whatever is left.
//
// Always called, including on the error paths, or the last few events of a
// failed run are lost precisely when they are most worth having.
func (b *traceBuffer) Close(ctx context.Context) error {
	b.closeOnce.Do(func() {
		close(b.stop)
		<-b.done
		b.flush(ctx)
	})

	b.mu.Lock()
	defer b.mu.Unlock()
	return b.failed
}

func (b *traceBuffer) flush(ctx context.Context) {
	b.mu.Lock()
	if len(b.pending) == 0 || b.failed != nil {
		b.mu.Unlock()
		return
	}
	// Hand the slice off and start a fresh one, so producers are not blocked on
	// the network round trip below.
	batch := b.pending
	b.pending = make([]trace.Event, 0, flushSize)
	b.mu.Unlock()

	if err := b.store.AppendEvents(ctx, b.runID, b.tenantID, batch); err != nil {
		b.mu.Lock()
		if b.failed == nil {
			b.failed = err
		}
		b.mu.Unlock()
		b.log.Error("could not persist trace batch",
			"run_id", b.runID, "events", len(batch), "err", err)
		return
	}

	// Publishing is best effort and deliberately not part of the error path. A
	// dropped publish costs a live viewer nothing — the events are durable, and
	// the client's next reconnect replays them from its cursor.
	for _, event := range batch {
		if err := b.pub.Publish(ctx, b.runID, event); err != nil {
			b.log.Debug("publish failed", "seq", event.Seq, "err", err)
		}
	}
}
