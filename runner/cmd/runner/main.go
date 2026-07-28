// Command runner executes queued runs inside sandboxed containers.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/JamesConway98/RunBox/runner/internal/config"
	"github.com/JamesConway98/RunBox/runner/internal/queue"
	"github.com/JamesConway98/RunBox/runner/internal/sandbox"
	"github.com/JamesConway98/RunBox/runner/internal/store"
	"github.com/JamesConway98/RunBox/runner/internal/worker"
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

	q, err := queue.Open(ctx, cfg.RedisURL)
	if err != nil {
		return err
	}
	defer q.Close()

	sbx, err := sandbox.New(log)
	if err != nil {
		return err
	}
	defer sbx.Close()

	// Fail at boot rather than on the first run.
	if err := sbx.Ping(ctx); err != nil {
		return err
	}

	executor := worker.NewExecutor(cfg, st, sbx, q, q, log)
	pool := worker.NewPool(cfg.Workers, q, st, executor, log)
	canceller := worker.NewCancelWatcher(q, pool, log)
	reaper := worker.NewReaper(st, cfg.MaxTimeout, log)

	if depth, err := q.Depth(ctx); err == nil && depth > 0 {
		log.Info("queue has waiting work at startup", "depth", depth)
	}

	log.Info("runner ready",
		"workers", cfg.Workers, "image", cfg.AgentImage, "max_timeout", cfg.MaxTimeout)

	var wg sync.WaitGroup
	for _, task := range []func(context.Context){canceller.Run, reaper.Run} {
		wg.Add(1)
		go func(run func(context.Context)) {
			defer wg.Done()
			run(ctx)
		}(task)
	}

	// Blocks until the context is cancelled, then drains in-flight runs.
	pool.Run(ctx)
	wg.Wait()

	log.Info("runner stopped")
	return nil
}

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	if err := lvl.UnmarshalText([]byte(level)); err != nil {
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl}))
}
