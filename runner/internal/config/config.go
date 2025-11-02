// Package config loads runner configuration from the environment.
//
// No config file. The runner is deployed as a container next to a Docker
// socket, and a container's configuration surface is its environment.
package config

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	DatabaseURL string
	RedisURL    string

	// AgentImage is pinned by digest in production. A tag is accepted for
	// local development but logged as a warning, because "which code actually
	// ran" is the first question asked about any agent trace.
	AgentImage string

	Workers        int
	PollInterval   time.Duration
	DefaultTimeout time.Duration
	MaxTimeout     time.Duration

	MemoryLimitMB int64
	CPULimit      float64
	PidsLimit     int64

	AnthropicAPIKey  string
	AnthropicBaseURL string
	AnthropicVersion string

	// Optional. When unset the OpenAI upstream is simply not mounted, and a run
	// against a gpt-* model fails with a clear error rather than a confusing
	// 401 from somebody else's API.
	OpenAIAPIKey  string
	OpenAIBaseURL string

	EgressAllowlist []string

	// SocketBaseDir holds one directory per in-flight run, each containing the
	// unix sockets bind-mounted into that run's container.
	SocketBaseDir string

	LogLevel string
}

func Load() (*Config, error) {
	c := &Config{
		DatabaseURL:      os.Getenv("DATABASE_URL"),
		RedisURL:         os.Getenv("REDIS_URL"),
		AgentImage:       envString("RUNBOX_AGENT_IMAGE", "runbox/agent:dev"),
		Workers:          envInt("RUNBOX_WORKERS", runtime.GOMAXPROCS(0)),
		PollInterval:     envDuration("RUNBOX_POLL_INTERVAL", 500*time.Millisecond),
		DefaultTimeout:   envDuration("RUNBOX_DEFAULT_TIMEOUT", 120*time.Second),
		MaxTimeout:       envDuration("RUNBOX_MAX_TIMEOUT", 600*time.Second),
		MemoryLimitMB:    int64(envInt("RUNBOX_MEMORY_MB", 512)),
		CPULimit:         envFloat("RUNBOX_CPUS", 1.0),
		PidsLimit:        int64(envInt("RUNBOX_PIDS_LIMIT", 128)),
		AnthropicAPIKey:  os.Getenv("ANTHROPIC_API_KEY"),
		AnthropicBaseURL: envString("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
		AnthropicVersion: envString("ANTHROPIC_VERSION", "2023-06-01"),
		OpenAIAPIKey:     os.Getenv("OPENAI_API_KEY"),
		OpenAIBaseURL:    envString("OPENAI_BASE_URL", "https://api.openai.com"),
		SocketBaseDir:    envString("RUNBOX_SOCKET_DIR", "/tmp/runbox-sockets"),
		EgressAllowlist: envList("RUNBOX_EGRESS_ALLOWLIST", []string{
			"api.github.com",
			"raw.githubusercontent.com",
			"en.wikipedia.org",
		}),
		LogLevel: envString("RUNBOX_LOG_LEVEL", "info"),
	}

	if c.DatabaseURL == "" {
		return nil, fmt.Errorf("DATABASE_URL is required")
	}
	if c.RedisURL == "" {
		return nil, fmt.Errorf("REDIS_URL is required")
	}
	if c.Workers < 1 {
		return nil, fmt.Errorf("RUNBOX_WORKERS must be at least 1, got %d", c.Workers)
	}
	if c.AnthropicAPIKey == "" {
		return nil, fmt.Errorf("ANTHROPIC_API_KEY is required")
	}
	return c, nil
}

// PinnedImage reports whether the agent image is referenced by digest.
func (c *Config) PinnedImage() bool {
	return strings.Contains(c.AgentImage, "@sha256:")
}

func envString(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key)))
	if err != nil {
		return def
	}
	return v
}

func envFloat(key string, def float64) float64 {
	v, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(key)), 64)
	if err != nil {
		return def
	}
	return v
}

func envDuration(key string, def time.Duration) time.Duration {
	v, err := time.ParseDuration(strings.TrimSpace(os.Getenv(key)))
	if err != nil {
		return def
	}
	return v
}

func envList(key string, def []string) []string {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
