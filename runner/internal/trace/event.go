// Package trace defines the event vocabulary shared between the agent, the
// runner and the control plane.
//
// The agent emits these without a seq. The runner assigns one, because it is
// the only component that sees the whole ordered stream for a run.
package trace

import (
	"encoding/json"
	"fmt"
)

type Type string

const (
	TypeLLMCall    Type = "llm_call"
	TypeToken      Type = "token"
	TypeToolCall   Type = "tool_call"
	TypeToolResult Type = "tool_result"
	TypeUsage      Type = "usage"
	TypeError      Type = "error"
	TypeFinal      Type = "final"
)

// Event is one line of the agent's stdout, after the runner has stamped it.
type Event struct {
	Seq     int             `json:"seq"`
	Type    Type            `json:"type"`
	TS      int64           `json:"ts"`
	Payload json.RawMessage `json:"payload"`
}

// Usage is the token accounting carried on a final event.
type Usage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	ToolCalls    int `json:"tool_calls"`
}

// Final is the terminal event's payload.
type Final struct {
	Status string `json:"status"`
	Result string `json:"result"`
	Usage  Usage  `json:"usage"`
}

// Parse turns one line of agent stdout into an Event with the given seq.
//
// A malformed line is an error the caller records as an error event and moves
// past. It is never fatal: an agent that prints a stray line to stdout should
// not take down the run.
func Parse(line []byte, seq int) (Event, error) {
	var probe struct {
		Type Type  `json:"type"`
		TS   int64 `json:"ts"`
	}
	if err := json.Unmarshal(line, &probe); err != nil {
		return Event{}, fmt.Errorf("not json: %w", err)
	}
	if probe.Type == "" {
		return Event{}, fmt.Errorf("missing type")
	}

	// The payload is the whole object. Keeping it verbatim means a new event
	// field added in the agent flows through to the dashboard without a change
	// here — the runner is a pipe, not a schema authority.
	return Event{
		Seq:     seq,
		Type:    probe.Type,
		TS:      probe.TS,
		Payload: json.RawMessage(append([]byte(nil), line...)),
	}, nil
}

// DecodeUsage extracts cumulative usage from a usage event.
func (e Event) DecodeUsage() (Usage, error) {
	var u Usage
	if e.Type != TypeUsage {
		return u, fmt.Errorf("event %d is %s, not usage", e.Seq, e.Type)
	}
	if err := json.Unmarshal(e.Payload, &u); err != nil {
		return u, fmt.Errorf("decode usage: %w", err)
	}
	return u, nil
}

// DecodeFinal extracts the terminal payload from a final event.
func (e Event) DecodeFinal() (Final, error) {
	var f Final
	if e.Type != TypeFinal {
		return f, fmt.Errorf("event %d is %s, not final", e.Seq, e.Type)
	}
	if err := json.Unmarshal(e.Payload, &f); err != nil {
		return f, fmt.Errorf("decode final: %w", err)
	}
	return f, nil
}

// ErrorEvent builds a synthetic error event for problems the runner itself
// detects, so that every failure is visible in the same stream the user is
// already watching.
func ErrorEvent(seq int, ts int64, message string) Event {
	payload, _ := json.Marshal(map[string]any{
		"type":    string(TypeError),
		"ts":      ts,
		"message": message,
		"source":  "runner",
	})
	return Event{Seq: seq, Type: TypeError, TS: ts, Payload: payload}
}
