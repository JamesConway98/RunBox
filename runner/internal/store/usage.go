package store

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// Pricing is one row of model_pricing, loaded at the moment a run is billed.
type Pricing struct {
	Model             string
	InputMicrosPer1k  int64
	OutputMicrosPer1k int64
	ComputeMicrosPerS int64
}

// Usage is what a run actually consumed.
type Usage struct {
	InputTokens  int
	OutputTokens int
	ToolCalls    int
	ComputeMS    int
}

// ErrNoPricing means we have no price for the model. The run is still recorded;
// only the cost is unknown.
var ErrNoPricing = errors.New("no pricing row for model")

func (s *Store) Pricing(ctx context.Context, model string) (Pricing, error) {
	const q = `
		select model, input_micros_per_1k, output_micros_per_1k, compute_micros_per_s
		from model_pricing where model = $1`

	var p Pricing
	err := s.pool.QueryRow(ctx, q, model).Scan(
		&p.Model, &p.InputMicrosPer1k, &p.OutputMicrosPer1k, &p.ComputeMicrosPerS,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Pricing{}, ErrNoPricing
	}
	if err != nil {
		return Pricing{}, fmt.Errorf("load pricing for %s: %w", model, err)
	}
	return p, nil
}

// CostMicros computes the cost of a run in integer micros.
//
// Integer arithmetic throughout, with division last and rounding up. Floats
// would introduce representation error into money, and rounding down would mean
// systematically undercharging by a fraction of a micro on every single run —
// which is small, consistent, and exactly the kind of thing that is discovered
// during an audit rather than during development.
func CostMicros(p Pricing, u Usage) int64 {
	return ceilDiv(int64(u.InputTokens)*p.InputMicrosPer1k, 1000) +
		ceilDiv(int64(u.OutputTokens)*p.OutputMicrosPer1k, 1000) +
		ceilDiv(int64(u.ComputeMS)*p.ComputeMicrosPerS, 1000)
}

func ceilDiv(numerator, denominator int64) int64 {
	if numerator <= 0 {
		return 0
	}
	return (numerator + denominator - 1) / denominator
}

// RecordUsage writes the usage row for a completed run.
//
// Cost is computed here, at write time, from the pricing row in force right
// now, and then stored. It is never recomputed on read. Prices change;
// historical costs must not.
//
// Called for every terminal state, including timeout and cancel. A run that
// burned 40,000 tokens before being cancelled consumed 40,000 tokens, and a
// metering system that quietly forgets that is not a metering system.
func (s *Store) RecordUsage(
	ctx context.Context, runID, tenantID, model string, u Usage,
) (int64, error) {
	pricing, err := s.Pricing(ctx, model)
	if err != nil && !errors.Is(err, ErrNoPricing) {
		return 0, err
	}

	// An unpriced model still gets a usage row, with zero cost. Losing the
	// token counts because we could not price them would be the worse trade.
	cost := CostMicros(pricing, u)

	const q = `
		insert into usage_records (
			run_id, tenant_id, model,
			input_tokens, output_tokens, tool_calls, compute_ms, cost_micros
		)
		values ($1, $2, $3, $4, $5, $6, $7, $8)
		on conflict (run_id) do nothing`

	_, err = s.pool.Exec(ctx, q,
		runID, tenantID, model,
		u.InputTokens, u.OutputTokens, u.ToolCalls, u.ComputeMS, cost,
	)
	if err != nil {
		return 0, fmt.Errorf("record usage for %s: %w", runID, err)
	}
	return cost, nil
}
