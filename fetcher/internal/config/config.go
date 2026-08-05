// Package config loads the fetcher's settings from the environment. Same JME_ prefixed
// variables the Python services read, so one .env drives the whole system.
package config

import (
	"os"
	"strconv"
	"time"
)

type Config struct {
	DatabaseURL string
	RedisURL    string

	UserAgent      string
	HostRatePerSec float64
	HostBurst      int
	FetchTimeout   time.Duration
	RespectRobots  bool

	// queue tuning
	Workers           int
	BlockTimeout      time.Duration
	VisibilityTimeout time.Duration
	MaxAttempts       int
	ClaimInterval     time.Duration

	// circuit breaker
	CircuitThreshold int
	CircuitCooldown  time.Duration
}

func Load() Config {
	return Config{
		DatabaseURL: env("JME_DATABASE_URL_GO", pgURL()),
		RedisURL:    env("JME_REDIS_URL", "redis://localhost:6380/0"),

		UserAgent:      env("JME_USER_AGENT", "job-match-engine/0.1 (+https://github.com/adilmallick/job-match-engine)"),
		HostRatePerSec: envFloat("JME_HOST_RATE_PER_SEC", 0.5),
		HostBurst:      envInt("JME_HOST_BURST", 1),
		FetchTimeout:   time.Duration(envInt("JME_FETCH_TIMEOUT_SEC", 20)) * time.Second,
		RespectRobots:  envBool("JME_RESPECT_ROBOTS", true),

		Workers:           envInt("JME_FETCH_WORKERS", 4),
		BlockTimeout:      time.Duration(envInt("JME_QUEUE_BLOCK_MS", 5000)) * time.Millisecond,
		VisibilityTimeout: time.Duration(envInt("JME_QUEUE_VISIBILITY_SEC", 120)) * time.Second,
		MaxAttempts:       envInt("JME_QUEUE_MAX_ATTEMPTS", 3),
		ClaimInterval:     time.Duration(envInt("JME_QUEUE_CLAIM_INTERVAL_SEC", 30)) * time.Second,

		CircuitThreshold: envInt("JME_CIRCUIT_THRESHOLD", 5),
		CircuitCooldown:  time.Duration(envInt("JME_CIRCUIT_COOLDOWN_SEC", 300)) * time.Second,
	}
}

// pgURL translates the SQLAlchemy-flavoured JME_DATABASE_URL into something pgx accepts.
// postgresql+psycopg://... -> postgresql://...
func pgURL() string {
	raw := env("JME_DATABASE_URL", "postgresql://jme:jme@localhost:5433/jme")
	const prefix = "postgresql+psycopg://"
	if len(raw) > len(prefix) && raw[:len(prefix)] == prefix {
		return "postgresql://" + raw[len(prefix):]
	}
	return raw
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

func envBool(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

// Stream and consumer group names. Contract with jme/config.py.
const (
	StreamFetch      = "jme:fetch"
	StreamEnrich     = "jme:enrich"
	StreamFetchDead  = "jme:fetch:dead"
	StreamEnrichDead = "jme:enrich:dead"
	GroupFetch       = "fetchers"
	GroupEnrich      = "enrichers"
)
