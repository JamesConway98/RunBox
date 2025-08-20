package trace

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestParseAssignsSeqAndKeepsPayloadVerbatim(t *testing.T) {
	line := []byte(`{"type":"token","ts":1722600000420,"text":"Based on"}`)

	event, err := Parse(line, 7)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if event.Seq != 7 {
		t.Errorf("seq = %d, want 7", event.Seq)
	}
	if event.Type != TypeToken {
		t.Errorf("type = %q, want token", event.Type)
	}
	if event.TS != 1722600000420 {
		t.Errorf("ts = %d", event.TS)
	}

	// The payload is kept whole so a field added in the agent reaches the
	// dashboard without a change here.
	var decoded map[string]any
	if err := json.Unmarshal(event.Payload, &decoded); err != nil {
		t.Fatalf("payload is not json: %v", err)
	}
	if decoded["text"] != "Based on" {
		t.Errorf("payload lost the text field: %v", decoded)
	}
}

func TestParseRejectsMalformedLines(t *testing.T) {
	cases := map[string]string{
		"not json":     `this is not json`,
		"missing type": `{"ts":1,"text":"hello"}`,
		"empty object": `{}`,
		"truncated":    `{"type":"token","tex`,
	}

	for name, line := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse([]byte(line), 1); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestParseDoesNotAliasCallerBuffer(t *testing.T) {
	// bufio.Scanner reuses its buffer between lines. If Parse held a reference
	// rather than a copy, every event would end up showing the last line read.
	buf := []byte(`{"type":"token","ts":1,"text":"first"}`)

	event, err := Parse(buf, 1)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	copy(buf, []byte(`{"type":"error","ts":9,"text":"XXXXX"}`))

	if !strings.Contains(string(event.Payload), "first") {
		t.Errorf("payload aliased the caller's buffer: %s", event.Payload)
	}
}

func TestDecodeFinal(t *testing.T) {
	line := []byte(`{"type":"final","ts":1,"status":"succeeded","result":"42",` +
		`"usage":{"input_tokens":8123,"output_tokens":412,"tool_calls":2}}`)

	event, err := Parse(line, 22)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	final, err := event.DecodeFinal()
	if err != nil {
		t.Fatalf("DecodeFinal: %v", err)
	}

	if final.Status != "succeeded" || final.Result != "42" {
		t.Errorf("final = %+v", final)
	}
	if final.Usage.InputTokens != 8123 || final.Usage.ToolCalls != 2 {
		t.Errorf("usage = %+v", final.Usage)
	}
}

func TestDecodeFinalRejectsNonFinal(t *testing.T) {
	event, err := Parse([]byte(`{"type":"token","ts":1,"text":"x"}`), 1)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if _, err := event.DecodeFinal(); err == nil {
		t.Fatal("expected an error decoding a non-final event")
	}
}

func TestErrorEventIsWellFormed(t *testing.T) {
	event := ErrorEvent(3, 1722600000000, "malformed agent output")

	if event.Type != TypeError || event.Seq != 3 {
		t.Fatalf("event = %+v", event)
	}
	var decoded map[string]any
	if err := json.Unmarshal(event.Payload, &decoded); err != nil {
		t.Fatalf("payload is not json: %v", err)
	}
	if decoded["source"] != "runner" {
		t.Errorf("runner-generated errors should be attributed: %v", decoded)
	}
}
