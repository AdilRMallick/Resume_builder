package httpx

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// BreakerState is what the breaker decided for one attempt.
type BreakerState string

const (
	// StateClosed: normal operation, go ahead.
	StateClosed BreakerState = "closed"
	// StateOpen: the host is being rested. The caller must NOT touch the network.
	StateOpen BreakerState = "open"
	// StateTrial: the cooldown elapsed and this caller, alone, is the half-open
	// probe. Its result decides whether the circuit closes or re-opens.
	StateTrial BreakerState = "trial"
)

// Breaker is a per-host circuit breaker.
type Breaker interface {
	// Allow reports whether a request to host may be made now.
	Allow(ctx context.Context, host string) (BreakerState, error)
	// Report feeds an attempt's outcome back. state is what Allow returned.
	Report(ctx context.Context, host string, state BreakerState, healthy bool) error
}

// allowScript decides and, where the decision is also a transition, performs the
// transition in the same atomic step. That matters for half-open: exactly one
// caller across the whole fleet may receive 'trial', because the open -> half
// flip and the read that produced it happen inside one script invocation.
//
//	KEYS[1] breaker hash
//	ARGV[1] now, unix millis
//	ARGV[2] cooldown millis
//	ARGV[3] trial timeout millis
//	ARGV[4] key TTL millis
var allowScript = redis.NewScript(`
local now      = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local trialTO  = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])

local h     = redis.call('HMGET', KEYS[1], 'state', 'opened_ms', 'trial_ms')
local state = h[1]

if state == false or state == nil or state == 'closed' then
  return 'closed'
end

if state == 'open' then
  local opened = tonumber(h[2]) or 0
  if (now - opened) >= cooldown then
    redis.call('HSET', KEYS[1], 'state', 'half', 'trial_ms', ARGV[1])
    redis.call('PEXPIRE', KEYS[1], ttl)
    return 'trial'
  end
  return 'open'
end

if state == 'half' then
  -- a probe is already in flight; hand out another one only if that probe
  -- looks abandoned (its worker died mid-request)
  local t = tonumber(h[3]) or 0
  if (now - t) >= trialTO then
    redis.call('HSET', KEYS[1], 'trial_ms', ARGV[1])
    redis.call('PEXPIRE', KEYS[1], ttl)
    return 'trial'
  end
  return 'open'
end

return 'closed'
`)

// successScript closes the circuit and clears the failure run.
//
//	KEYS[1] breaker hash
//	ARGV[1] key TTL millis
var successScript = redis.NewScript(`
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', '0')
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[1]))
return 'closed'
`)

// failureScript counts a consecutive failure and opens the circuit when the run
// reaches the threshold. A failed half-open probe re-opens immediately.
//
//	KEYS[1] breaker hash
//	ARGV[1] now, unix millis
//	ARGV[2] threshold
//	ARGV[3] key TTL millis
//	ARGV[4] "1" when the caller held the half-open trial
var failureScript = redis.NewScript(`
local now       = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local ttl       = tonumber(ARGV[3])
local wasTrial  = ARGV[4] == '1'

local state = redis.call('HGET', KEYS[1], 'state')

if wasTrial or state == 'half' then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_ms', ARGV[1], 'failures', ARGV[2])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 'open'
end

local n = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if n >= threshold then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_ms', ARGV[1])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 'open'
end
redis.call('HSET', KEYS[1], 'state', 'closed')
redis.call('PEXPIRE', KEYS[1], ttl)
return 'closed'
`)

// RedisBreaker keeps breaker state in Redis so a host tripped by one worker is
// rested by all of them.
type RedisBreaker struct {
	rdb    redis.Scripter
	Prefix string
	// Threshold is the number of consecutive failures that opens the circuit.
	Threshold int
	// Cooldown is how long a host rests before a half-open probe is allowed.
	Cooldown time.Duration
	// TrialTimeout bounds how long one half-open probe may block the others,
	// covering the worker-died-mid-probe case.
	TrialTimeout time.Duration

	now func() time.Time
}

// NewRedisBreaker builds a breaker. Zero or negative threshold/cooldown fall back
// to sane defaults rather than disabling the breaker by accident.
func NewRedisBreaker(rdb redis.Scripter, threshold int, cooldown time.Duration) *RedisBreaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = 5 * time.Minute
	}
	return &RedisBreaker{
		rdb:          rdb,
		Prefix:       DefaultKeyPrefix,
		Threshold:    threshold,
		Cooldown:     cooldown,
		TrialTimeout: 60 * time.Second,
		now:          time.Now,
	}
}

func (b *RedisBreaker) key(host string) string { return b.Prefix + ":cb:" + host }

func (b *RedisBreaker) ttl() time.Duration {
	ttl := 4 * b.Cooldown
	if ttl < time.Hour {
		ttl = time.Hour
	}
	return ttl
}

// Allow implements Breaker.
func (b *RedisBreaker) Allow(ctx context.Context, host string) (BreakerState, error) {
	trialTO := b.TrialTimeout
	if trialTO <= 0 {
		trialTO = 60 * time.Second
	}
	res, err := allowScript.Run(ctx, b.rdb, []string{b.key(host)},
		b.now().UnixMilli(), b.Cooldown.Milliseconds(), trialTO.Milliseconds(), b.ttl().Milliseconds(),
	).Text()
	if err != nil {
		return StateClosed, fmt.Errorf("breaker allow: %w", err)
	}
	switch res {
	case "open":
		return StateOpen, nil
	case "trial":
		return StateTrial, nil
	default:
		return StateClosed, nil
	}
}

// Report implements Breaker.
func (b *RedisBreaker) Report(ctx context.Context, host string, state BreakerState, healthy bool) error {
	key := b.key(host)
	if healthy {
		if err := successScript.Run(ctx, b.rdb, []string{key}, b.ttl().Milliseconds()).Err(); err != nil {
			return fmt.Errorf("breaker success: %w", err)
		}
		return nil
	}
	wasTrial := "0"
	if state == StateTrial {
		wasTrial = "1"
	}
	if err := failureScript.Run(ctx, b.rdb, []string{key},
		b.now().UnixMilli(), b.Threshold, b.ttl().Milliseconds(), wasTrial,
	).Err(); err != nil {
		return fmt.Errorf("breaker failure: %w", err)
	}
	return nil
}

// Reset clears a host's breaker state. Operational escape hatch, and what tests
// use between cases.
func (b *RedisBreaker) Reset(ctx context.Context, host string) error {
	c, ok := b.rdb.(redis.Cmdable)
	if !ok {
		return fmt.Errorf("breaker reset: %T is not redis.Cmdable", b.rdb)
	}
	return c.Del(ctx, b.key(host)).Err()
}
