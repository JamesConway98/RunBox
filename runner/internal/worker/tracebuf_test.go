package worker

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/trace"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeStore records batches so tests can assert on how work was grouped, which
// is the entire point of this component.
type fakeStore struct {
	mu      sync.Mutex
	batches [][]trace.Event
	err     error
	delay   time.Duration
}

func (f *fakeStore) AppendEvents(
	_ context.Context, _, _ string, events []trace.Event,
) error {
	if f.delay > 0 {
		time.Sleep(f.delay)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	// Copy: the buffer reuses its backing array, and a test that held the
	// original slice would be asserting on whatever landed there later.
	batch := make([]trace.Event, len(events))
	copy(batch, events)
	f.batches = append(f.batches, batch)
	return nil
}

func (f *fakeStore) snapshot() [][]trace.Event {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([][]trace.Event, len(f.batches))
	copy(out, f.batches)
	return out
}

func (f *fakeStore) total() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, b := range f.batches {
		n += len(b)
	}
	return n
}

type countingPublisher struct {
	mu sync.Mutex
	n  int
}

func (p *countingPublisher) Publish(context.Context, string, trace.Event) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.n++
	return nil
}

func (p *countingPublisher) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.n
}

func event(seq int, t trace.Type) trace.Event {
	payload, _ := json.Marshal(map[string]any{"type": string(t), "seq": seq})
	return trace.Event{Seq: seq, Type: t, TS: int64(seq), Payload: payload}
}

func TestBatchesRatherThanWritingPerEvent(t *testing.T) {
	// The whole reason this exists: 250 events must not be 250 round trips.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	for i := 1; i <= 250; i++ {
		if err := buf.Add(ctx, event(i, trace.TypeToken)); err != nil {
			t.Fatalf("Add: %v", err)
		}
	}
	if err := buf.Close(ctx); err != nil {
		t.Fatalf("Close: %v", err)
	}

	batches := store.snapshot()
	if store.total() != 250 {
		t.Fatalf("wrote %d events, want 250", store.total())
	}
	// 250 events at a flush size of 100 is three writes, not 250.
	if len(batches) > 5 {
		t.Errorf("took %d round trips for 250 events; batching is not working", len(batches))
	}
	t.Logf("250 events in %d round trips", len(batches))
}

func TestPreservesOrderAcrossBatches(t *testing.T) {
	// A viewer must never see seq 7 before seq 6.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	for i := 1; i <= 300; i++ {
		buf.Add(ctx, event(i, trace.TypeToken)) //nolint:errcheck
	}
	buf.Close(ctx) //nolint:errcheck

	want := 1
	for _, batch := range store.snapshot() {
		for _, e := range batch {
			if e.Seq != want {
				t.Fatalf("out of order: got seq %d, want %d", e.Seq, want)
			}
			want++
		}
	}
	if want != 301 {
		t.Errorf("saw %d events, want 300", want-1)
	}
}

func TestFinalEventFlushesImmediately(t *testing.T) {
	// The executor reads run state straight after the final event and would
	// otherwise race the ticker.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	buf.Add(ctx, event(1, trace.TypeToken)) //nolint:errcheck
	buf.Add(ctx, event(2, trace.TypeFinal)) //nolint:errcheck

	// No Close, no sleep — the final event alone must have forced the write.
	if store.total() != 2 {
		t.Fatalf("final event did not flush: %d events written", store.total())
	}
}

func TestTimerFlushesDuringQuietStretches(t *testing.T) {
	// A run can sit inside one tool call for a long time. Tokens emitted just
	// before it must not wait for the call to return.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())
	defer buf.Close(context.Background()) //nolint:errcheck

	buf.Add(context.Background(), event(1, trace.TypeToken)) //nolint:errcheck

	deadline := time.Now().Add(2 * time.Second)
	for store.total() == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if store.total() != 1 {
		t.Fatal("timer did not flush a partial batch")
	}
}

func TestCloseIsIdempotent(t *testing.T) {
	// Close runs explicitly before the run resolves and again from a defer on
	// the error paths. A second close of the stop channel would panic.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	buf.Add(ctx, event(1, trace.TypeToken)) //nolint:errcheck

	if err := buf.Close(ctx); err != nil {
		t.Fatalf("first Close: %v", err)
	}
	if err := buf.Close(ctx); err != nil {
		t.Fatalf("second Close: %v", err)
	}
	if store.total() != 1 {
		t.Errorf("wrote %d events, want 1", store.total())
	}
}

func TestWriteFailureSurfacesToTheCaller(t *testing.T) {
	// Losing the durable trace is worth failing the run for — it is the product.
	store := &fakeStore{err: errors.New("connection reset")}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	// Enough to force a flush and record the failure.
	var seen error
	for i := 1; i <= flushSize+1 && seen == nil; i++ {
		seen = buf.Add(ctx, event(i, trace.TypeToken))
	}
	if seen == nil {
		seen = buf.Close(ctx)
	}
	if seen == nil {
		t.Fatal("a failing store did not surface an error")
	}
}

func TestPublishFailureDoesNotFailTheRun(t *testing.T) {
	// A dropped publish costs a live viewer nothing: the events are durable and
	// the client replays from its cursor on reconnect.
	store := &fakeStore{}
	buf := newTraceBuffer("run-1", "tenant-1", store, failingPublisher{}, quietLogger())

	ctx := context.Background()
	buf.Add(ctx, event(1, trace.TypeToken)) //nolint:errcheck

	if err := buf.Close(ctx); err != nil {
		t.Fatalf("publish failure should not fail the run: %v", err)
	}
	if store.total() != 1 {
		t.Errorf("event was not persisted despite publish failing")
	}
}

type failingPublisher struct{}

func (failingPublisher) Publish(context.Context, string, trace.Event) error {
	return errors.New("redis is down")
}

func TestEveryEventIsPublishedExactlyOnce(t *testing.T) {
	store := &fakeStore{}
	pub := &countingPublisher{}
	buf := newTraceBuffer("run-1", "tenant-1", store, pub, quietLogger())

	ctx := context.Background()
	for i := 1; i <= 150; i++ {
		buf.Add(ctx, event(i, trace.TypeToken)) //nolint:errcheck
	}
	buf.Close(ctx) //nolint:errcheck

	if pub.count() != 150 {
		t.Errorf("published %d times, want 150", pub.count())
	}
}

func TestConcurrentAddAndFlushDoNotRace(t *testing.T) {
	// Producers append from the scanner loop while the ticker flushes. Run with
	// -race, which CI does.
	store := &fakeStore{delay: time.Millisecond}
	buf := newTraceBuffer("run-1", "tenant-1", store, &countingPublisher{}, quietLogger())

	ctx := context.Background()
	var wg sync.WaitGroup
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func(base int) {
			defer wg.Done()
			for i := 1; i <= 50; i++ {
				buf.Add(ctx, event(base*100+i, trace.TypeToken)) //nolint:errcheck
			}
		}(w)
	}
	wg.Wait()
	buf.Close(ctx) //nolint:errcheck

	if store.total() != 200 {
		t.Errorf("wrote %d events, want 200", store.total())
	}
}
