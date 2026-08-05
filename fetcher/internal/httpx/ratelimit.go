package httpx

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/redis/go-redis/v9"
)

// DefaultKeyPrefix namespaces every key this package writes to Redis.
const DefaultKeyPrefix = "jme:httpx"

// Redis key schema (all single-key, so the Lua scripts are Redis Cluster safe):
//
//	<prefix>:rl:<host>      HASH {tokens, last_refill_ms}   token bucket
//	<prefix>:cb:<host>      HASH {state, failures, opened_ms, trial_ms}
//	<prefix>:robots:<host>  STRING raw robots.txt body, TTL'd
//
// <host> includes the port when the URL carries one.

// ErrRateWaitTooLong is returned by a Limiter when the queue ahead of this caller
// is so long that waiting would exceed MaxWait. The Client turns it into a
// domain.ErrRateLimited so the queue harness reschedules the job instead of
// pinning a worker.
var ErrRateWaitTooLong = errors.New("httpx: rate limit wait exceeds maximum")

// Limiter gates outbound requests per host.
type Limiter interface {
	// Wait blocks until this caller may issue one request to host, or until ctx
	// is done. Implementations must respect ctx.
	Wait(ctx context.Context, host string, r Rate) error
}

// tokenBucketScript is the whole rate limiter.
//
// Why Lua: a token bucket is read-modify-write over {tokens, last_refill_ms}.
// Doing that with GET/SET from Go races between processes, and doing it with
// WATCH/MULTI costs a round trip per retry under contention. A script runs
// inside the single Redis command loop, so refill-then-consume is atomic by
// construction against every other worker in the fleet. go-redis sends it as
// EVALSHA and only falls back to EVAL on NOSCRIPT, so the script body crosses
// the wire once per Redis restart.
//
// The consume step is a *reservation*: on a miss the balance is allowed to go
// negative and the caller is told how long to sleep to pay off exactly its own
// share of the debt. That gives arrival-order fairness and one sleep per
// acquisition, instead of every blocked worker waking together to re-contend for
// the same token.
//
//	KEYS[1] bucket hash
//	ARGV[1] refill rate, tokens per second (float)
//	ARGV[2] burst, bucket capacity (float)
//	ARGV[3] now, unix millis
//	ARGV[4] tokens requested (normally 1)
//	ARGV[5] max acceptable wait, millis
//	ARGV[6] key TTL, millis
//
// Returns the millis to sleep before proceeding (0 = go now), or -1 when the
// reservation was refused because it would have exceeded ARGV[5].
var tokenBucketScript = redis.NewScript(`
local rate    = tonumber(ARGV[1])
local burst   = tonumber(ARGV[2])
local now     = tonumber(ARGV[3])
local want    = tonumber(ARGV[4])
local maxwait = tonumber(ARGV[5])
local ttl     = tonumber(ARGV[6])

local h      = redis.call('HMGET', KEYS[1], 'tokens', 'last_refill_ms')
local tokens = tonumber(h[1])
local last   = tonumber(h[2])
if tokens == nil or last == nil then
  tokens = burst
  last   = now
end

-- refill
if now > last then
  tokens = math.min(burst, tokens + ((now - last) * rate) / 1000.0)
end
last = now

-- consume, possibly into debt
local remaining = tokens - want
local wait = 0
if remaining < 0 then
  wait = math.ceil((-remaining) * 1000.0 / rate)
  if wait > maxwait then
    -- refuse: persist the refill but not the reservation
    redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'last_refill_ms', tostring(last))
    redis.call('PEXPIRE', KEYS[1], ttl)
    return -1
  end
end

redis.call('HSET', KEYS[1], 'tokens', tostring(remaining), 'last_refill_ms', tostring(last))
redis.call('PEXPIRE', KEYS[1], ttl)
return wait
`)

// restoreScript hands a reservation back when the caller is cancelled before it
// consumed the slot. Best effort: it never pushes the balance above burst.
//
//	KEYS[1] bucket hash
//	ARGV[1] burst
//	ARGV[2] tokens to return
//	ARGV[3] key TTL, millis
var restoreScript = redis.NewScript(`
local burst  = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local ttl    = tonumber(ARGV[3])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
if tokens == nil then return 0 end
tokens = math.min(burst, tokens + amount)
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens))
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
`)

// RedisLimiter is a per-host token bucket whose state lives in Redis, so N worker
// processes share one budget per host.
type RedisLimiter struct {
	rdb    redis.Scripter
	Prefix string
	// MaxWait bounds how long a single Wait may block. Beyond it the limiter
	// refuses the reservation and returns ErrRateWaitTooLong.
	MaxWait time.Duration
	now     func() time.Time
}

// NewRedisLimiter builds a limiter over rdb.
func NewRedisLimiter(rdb redis.Scripter) *RedisLimiter {
	return &RedisLimiter{
		rdb:     rdb,
		Prefix:  DefaultKeyPrefix,
		MaxWait: 60 * time.Second,
		now:     time.Now,
	}
}

func (l *RedisLimiter) key(host string) string {
	return l.Prefix + ":rl:" + host
}

// Wait implements Limiter.
func (l *RedisLimiter) Wait(ctx context.Context, host string, r Rate) error {
	if r.PerSec <= 0 {
		return nil // limiting disabled for this host
	}
	burst := r.Burst
	if burst <= 0 {
		burst = 1
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	maxWait := l.MaxWait
	if maxWait <= 0 {
		maxWait = 60 * time.Second
	}
	// Keep the bucket alive well past a full refill so a quiet host does not
	// reset to a free burst on every run.
	ttl := time.Duration(float64(burst)/r.PerSec*float64(time.Second)) + maxWait + time.Minute

	key := l.key(host)
	now := l.now().UnixMilli()

	res, err := tokenBucketScript.Run(ctx, l.rdb, []string{key},
		r.PerSec, float64(burst), now, 1.0, maxWait.Milliseconds(), ttl.Milliseconds(),
	).Int64()
	if err != nil {
		return fmt.Errorf("token bucket: %w", err)
	}
	if res < 0 {
		return fmt.Errorf("%w: host %s", ErrRateWaitTooLong, host)
	}
	if res == 0 {
		return nil
	}

	wait := time.Duration(res) * time.Millisecond
	if err := sleepCtx(ctx, wait); err != nil {
		l.restore(key, burst, ttl)
		return err
	}
	return nil
}

// restore returns a reserved token after a cancelled wait. It deliberately uses a
// fresh context: the caller's is already dead, and leaking a reservation would
// slow the whole fleet down for one cancellation.
func (l *RedisLimiter) restore(key string, burst int, ttl time.Duration) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, _ = restoreScript.Run(ctx, l.rdb, []string{key}, float64(burst), 1.0, ttl.Milliseconds()).Result()
}

// ExpectedMinDuration reports the shortest wall time in which n requests can
// legally be issued against one host under r, assuming a full bucket at the
// start. Tests assert against it; it is also the honest answer to "how long will
// this run take".
func ExpectedMinDuration(n int, r Rate) time.Duration {
	if r.PerSec <= 0 || n <= 0 {
		return 0
	}
	burst := r.Burst
	if burst <= 0 {
		burst = 1
	}
	paid := n - burst
	if paid <= 0 {
		return 0
	}
	secs := float64(paid) / r.PerSec
	return time.Duration(math.Round(secs * float64(time.Second)))
}
