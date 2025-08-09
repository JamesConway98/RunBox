// Command runner executes queued runs inside sandboxed containers.
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jamesconway/runbox/runner/internal/config"
	"github.com/jamesconway/runbox/runner/internal/sandbox"
	"github.com/jamesconway/runbox/runner/internal/store"
	"github.com/jamesconway/runbox/runner/internal/worker"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	log := newLogger(cfg.LogLevel)
	slog.SetDefault(log)

	if !cfg.PinnedImage() {
		log.Warn("agent image is not pinned by digest", "image", cfg.AgentImage)
	}

	// Signals first, so a Ctrl-C during startup still exits cleanly.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Close()

	sbx, err := sandbox.New(log)
	if err != nil {
		return err
	}
	defer sbx.Close()

	// Fail at boot rather than on the first run.
	if err := sbx.Ping(ctx); err != nil {
		return err
	}

	executor := worker.NewExecutor(cfg, st, sbx, nil, log)

	log.Info("runner ready",
		"workers", cfg.Workers, "image", cfg.AgentImage, "poll", cfg.PollInterval)

	// M1: a single worker polling Postgres. The worker pool and the Redis queue
	// land in M2; the executor above is already safe to call concurrently.
	if err := pollLoop(ctx, st, executor, cfg.PollInterval, log); err != nil {
		return err
	}

	log.Info("runner stopped")
	return nil
}

func pollLoop(
	ctx context.Context,
	st *store.Store,
	executor *worker.Executor,
	interval time.Duration,
	log *slog.Logger,
) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		run, err := st.Claim(ctx)
		switch {
		case errors.Is(err, store.ErrNoWork):
			select {
			case <-ctx.Done():
				return nil
			case <-ticker.C:
				continue
			}
		case err != nil:
			if ctx.Err() != nil {
				return nil
			}
			// A transient database error should not kill the process; back off
			// and try again.
			log.Error("claim failed", "err", err)
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(2 * time.Second):
				continue
			}
		}

		if err := executor.Execute(ctx, run); err != nil {
			log.Error("execute failed", "run_id", run.ID, "err", err)
		}
	}
}

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	if err := lvl.UnmarshalText([]byte(level)); err != nil {
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl}))
}
