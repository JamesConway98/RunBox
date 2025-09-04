package store

import "testing"

var sonnet = Pricing{
	Model:             "claude-sonnet-5",
	InputMicrosPer1k:  3000,
	OutputMicrosPer1k: 15000,
	ComputeMicrosPerS: 200,
}

func TestCostMicros(t *testing.T) {
	cases := []struct {
		name  string
		usage Usage
		want  int64
	}{
		{
			name:  "zero usage costs nothing",
			usage: Usage{},
			want:  0,
		},
		{
			name:  "exact thousands divide cleanly",
			usage: Usage{InputTokens: 1000, OutputTokens: 1000, ComputeMS: 1000},
			want:  3000 + 15000 + 200,
		},
		{
			// 8123 input  -> ceil(8123*3000/1000)  = 24369
			// 412 output  -> ceil(412*15000/1000)  = 6180
			// 4300 ms     -> ceil(4300*200/1000)   = 860
			name:  "partial thousands round up",
			usage: Usage{InputTokens: 8123, OutputTokens: 412, ComputeMS: 4300},
			want:  24369 + 6180 + 860,
		},
		{
			// A single token must never be free. Rounding down here would mean
			// systematically undercharging on every small run.
			name:  "one token still costs something",
			usage: Usage{InputTokens: 1},
			want:  3,
		},
		{
			name:  "sub-micro compute rounds up rather than vanishing",
			usage: Usage{ComputeMS: 1},
			want:  1,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := CostMicros(sonnet, tc.usage); got != tc.want {
				t.Errorf("CostMicros(%+v) = %d, want %d", tc.usage, got, tc.want)
			}
		})
	}
}

func TestCostMicrosWithoutPricingIsZeroNotNegative(t *testing.T) {
	// An unpriced model yields a zero Pricing. The row should still be written,
	// with a cost of zero rather than something nonsensical.
	if got := CostMicros(Pricing{}, Usage{InputTokens: 5000, OutputTokens: 900}); got != 0 {
		t.Errorf("unpriced cost = %d, want 0", got)
	}
}

func TestCostMicrosIsMonotonic(t *testing.T) {
	// More tokens must never cost less. Cheap to assert, and it would catch a
	// sign error or an overflow in the arithmetic above.
	previous := int64(-1)
	for tokens := 0; tokens < 50_000; tokens += 137 {
		cost := CostMicros(sonnet, Usage{InputTokens: tokens})
		if cost < previous {
			t.Fatalf("cost fell from %d to %d at %d tokens", previous, cost, tokens)
		}
		previous = cost
	}
}

func TestCeilDivHandlesNonPositiveInput(t *testing.T) {
	// Token counts should never be negative, but a provider returning junk
	// should produce zero rather than a negative charge.
	if got := ceilDiv(-500, 1000); got != 0 {
		t.Errorf("ceilDiv(-500, 1000) = %d, want 0", got)
	}
	if got := ceilDiv(0, 1000); got != 0 {
		t.Errorf("ceilDiv(0, 1000) = %d, want 0", got)
	}
}

func TestCostMicrosDoesNotOverflowAtLargeVolumes(t *testing.T) {
	// A million-token run on the most expensive model, well inside int64.
	huge := Usage{InputTokens: 1_000_000, OutputTokens: 1_000_000, ComputeMS: 600_000}
	opus := Pricing{InputMicrosPer1k: 15000, OutputMicrosPer1k: 75000, ComputeMicrosPerS: 200}

	if got := CostMicros(opus, huge); got <= 0 {
		t.Fatalf("cost overflowed or went negative: %d", got)
	}
}
