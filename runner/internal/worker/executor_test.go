package worker

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

// A run that fails has to say why on the run itself, not only in its trace.
//
// The agent's final event carries a status and no reason, so the reason has to
// be carried forward from the error event that preceded it. Without that, every
// wrong-provider-key run shows up as "failed" with an empty error column.

func stateWithLines(t *testing.T, lines ...string) *runState {
	t.Helper()
	s := &runState{
		log: quietLogger(),
		buf: newTraceBuffer("run-1", "tenant-1", &fakeStore{}, &countingPublisher{}, quietLogger()),
	}
	ctx := context.Background()
	for _, line := range lines {
		if err := s.onLine(ctx, []byte(line)); err != nil {
			t.Fatalf("onLine(%s): %v", line, err)
		}
	}
	t.Cleanup(func() { _ = s.buf.Close(ctx) })
	return s
}

func TestFailedRunCarriesTheAgentsReason(t *testing.T) {
	s := stateWithLines(t,
		`{"type":"llm_call","ts":1,"model":"claude-haiku-4-5"}`,
		`{"type":"error","ts":2,"message":"provider returned 401: API key is invalid.","retryable":false}`,
		`{"type":"final","ts":3,"status":"failed","result":"","usage":{}}`,
	)

	c := s.resolve(context.Background(), nil, nil, 0, time.Now())

	if c.Status != "failed" {
		t.Fatalf("status = %q, want failed", c.Status)
	}
	if !strings.Contains(c.Error, "401") {
		t.Errorf("error = %q, want the provider's reason", c.Error)
	}
}

func TestSucceededRunCarriesNoError(t *testing.T) {
	// An error mid-run that the agent recovered from must not become the
	// verdict on a run that went on to succeed.
	s := stateWithLines(t,
		`{"type":"error","ts":1,"message":"tool http_get timed out","retryable":true}`,
		`{"type":"final","ts":2,"status":"succeeded","result":"42","usage":{}}`,
	)

	c := s.resolve(context.Background(), nil, nil, 0, time.Now())

	if c.Status != "succeeded" || c.Error != "" {
		t.Errorf("completion = %+v, want succeeded with no error", c)
	}
}

func TestFailedRunWithoutAnErrorEventStillExplainsItself(t *testing.T) {
	s := stateWithLines(t, `{"type":"final","ts":1,"status":"failed","result":"","usage":{}}`)

	c := s.resolve(context.Background(), nil, nil, 0, time.Now())

	if c.Error == "" {
		t.Error("a failed run must never reach the database with an empty error")
	}
}

func TestTimeoutKeepsItsOwnReason(t *testing.T) {
	// The run-level verdict outranks anything the agent said on its way out: a
	// timeout is a timeout even if a tool errored first.
	s := stateWithLines(t,
		`{"type":"error","ts":1,"message":"tool http_get timed out","retryable":true}`,
	)

	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()

	c := s.resolve(ctx, nil, nil, 0, time.Now())

	if c.Status != "timeout" || !strings.Contains(c.Error, "timeout") {
		t.Errorf("completion = %+v, want a timeout with its own reason", c)
	}
}

func TestRunnerDetectedFailureIsUnaffected(t *testing.T) {
	s := stateWithLines(t)

	c := s.resolve(context.Background(), errors.New("broken pipe"), nil, 0, time.Now())

	if !strings.Contains(c.Error, "broken pipe") {
		t.Errorf("error = %q, want the stream error", c.Error)
	}
}
