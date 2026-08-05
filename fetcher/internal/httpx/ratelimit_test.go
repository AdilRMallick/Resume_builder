package httpx

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// ---------------------------------------------------------------------------
// Redis test harness. Everything here talks to the real jme-redis container
// (docker compose, localhost:6380). If it is not reachable the tests skip so the
// suite still passes on a plane.
// ---------------------------------------------------------------------------

// testRedis dials Redis or skips the test. Each caller gets its own key prefix
// and a cleanup that deletes everything under it.
func testRedis(t *testing.T) (*redis.Client, string) {
	t.Helper()

	rawURL := "redis://localhost:6380/0"
	if v := os.Getenv("JME_REDIS_URL"); v != "" {
		rawURL = v
	}
	opt, err := redis.ParseURL(rawURL)
	if err != nil {
		t.Skipf("bad redis url %q: %v", rawURL, err)
	}
	rdb := redis.NewClient(opt)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := rdb.Ping(ctx).Err(); err != nil {
		_ = rdb.Close()
		t.Skipf("redis unreachable at %s (%v); skipping the integration test", rawURL, err)
	}

	prefix := fmt.Sprintf("jme:httpxtest:%s:%d", sanitize(t.Name()), time.Now().UnixNano())
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer ccancel()
		iter := rdb.Scan(cctx, 0, prefix+"*", 100).Iterator()
		var keys []string
		for iter.Next(cctx) {
			keys = append(keys, iter.Val())
		}
		if len(keys) > 0 {
			_ = rdb.Del(cctx, keys...).Err()
		}
		_ = rdb.Close()
	})
	return rdb, prefix
}

func sanitize(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			out = append(out, r)
		default:
			out = append(out, '-')
		}
	}
	return string(out)
}

// TestRedisLimiterSharedAcrossProcesses is the acceptance case for the rate
// limiter. Two Client instances with independent Redis connections stand in for
// two worker processes. They hammer one host concurrently, and the wall time for
// the whole batch must be at least what the shared bucket allows: proof that the
// limit is enforced in Redis, not per process.
func TestRedisLimiterSharedAcrossProcesses(t *testing.T) {
	rdbA, prefix := testRedis(t)
	rdbB := redis.NewClient(rdbA.Options())
	t.Cleanup(func() { _ = rdbB.Close() })

	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Write([]byte("ok"))
	}))
	defer srv.Close()

	// The shipping default is 0.5/s (one request per two seconds). That is the
	// right production number and the wrong test number, so the shape is
	// identical and the clock is turned up: burst 1, no bursting, one token at a
	// time.
	const (
		perSec    = 5.0
		burst     = 1
		perWorker = 4
		workers   = 2
		total     = perWorker * workers
	)
	rate := Rate{PerSec: perSec, Burst: burst}

	newWorker := func(rdb *redis.Client) *Client {
		lim := NewRedisLimiter(rdb)
		lim.Prefix = prefix
		lim.MaxWait = 30 * time.Second

		cfg := testConfig()
		cfg.HostRatePerSec = perSec
		cfg.HostBurst = burst
		c, err := New(cfg, nil,
			WithLimiter(lim),
			WithBreaker(&stubBreaker{}),
			WithRetryPolicy(fastPolicy()),
		)
		if err != nil {
			t.Fatalf("New: %v", err)
		}
		return c
	}
	clients := []*Client{newWorker(rdbA), newWorker(rdbB)}

	start := time.Now()
	var wg sync.WaitGroup
	errs := make(chan error, total)
	for _, c := range clients {
		for i := 0; i < perWorker; i++ {
			wg.Add(1)
			go func(c *Client, i int) {
				defer wg.Done()
				resp, err := c.Do(context.Background(), mustGet(t, fmt.Sprintf("%s/jobs/%d", srv.URL, i)))
				if err != nil {
					errs <- err
					return
				}
				resp.Body.Close()
			}(c, i)
		}
	}
	wg.Wait()
	elapsed := time.Since(start)
	close(errs)
	for err := range errs {
		t.Fatalf("worker request failed: %v", err)
	}

	want := ExpectedMinDuration(total, rate)
	// 5% slack for timer granularity; the point is that the floor exists at all.
	floor := want - want/20
	if elapsed < floor {
		t.Fatalf("%d requests across %d clients took %v, want >= %v (the shared bucket was not enforced)",
			total, workers, elapsed, want)
	}
	if int(hits.Load()) != total {
		t.Fatalf("handler saw %d requests, want %d", hits.Load(), total)
	}
	t.Logf("%d requests across %d client instances at %.1f/s: elapsed %v, theoretical minimum %v, asserted floor %v",
		total, workers, perSec, elapsed.Round(time.Millisecond), want, floor)

	// A single process alone would also have to respect the same floor.
	u, _ := url.Parse(srv.URL)
	key := prefix + ":rl:" + hostKey(u)
	if n, err := rdbA.Exists(context.Background(), key).Result(); err != nil || n != 1 {
		t.Fatalf("bucket key %q missing after the run (exists=%d err=%v)", key, n, err)
	}
	if ttl, err := rdbA.PTTL(context.Background(), key).Result(); err != nil || ttl <= 0 {
		t.Fatalf("bucket key %q has no TTL (ttl=%v err=%v); it would leak", key, ttl, err)
	}
}

// TestRedisLimiterBurstThenRefill pins the arithmetic: a fresh bucket hands out
// exactly Burst tokens immediately and then meters.
func TestRedisLimiterBurstThenRefill(t *testing.T) {
	rdb, prefix := testRedis(t)
	lim := NewRedisLimiter(rdb)
	lim.Prefix = prefix

	rate := Rate{PerSec: 10, Burst: 3}
	ctx := context.Background()

	start := time.Now()
	for i := 0; i < 3; i++ {
		if err := lim.Wait(ctx, "burst.example", rate); err != nil {
			t.Fatalf("wait %d: %v", i, err)
		}
	}
	if d := time.Since(start); d > 200*time.Millisecond {
		t.Fatalf("the initial burst of 3 blocked for %v, want ~0", d)
	}

	start = time.Now()
	if err := lim.Wait(ctx, "burst.example", rate); err != nil {
		t.Fatalf("wait 4: %v", err)
	}
	if d := time.Since(start); d < 80*time.Millisecond {
		t.Fatalf("the 4th request waited %v, want ~100ms at 10/s", d)
	}
}

// TestRedisLimiterContextCancelAbortsWait is the acceptance case for
// cancellation: a worker parked behind the bucket must unpark immediately when
// its job is cancelled, and it must hand the reservation back.
func TestRedisLimiterContextCancelAbortsWait(t *testing.T) {
	rdb, prefix := testRedis(t)
	lim := NewRedisLimiter(rdb)
	lim.Prefix = prefix

	rate := Rate{PerSec: 0.5, Burst: 1} // the shipping default: one per two seconds
	host := "cancel.example"

	// Drain the burst so the next caller has to wait ~2s.
	if err := lim.Wait(context.Background(), host, rate); err != nil {
		t.Fatalf("priming wait: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	err := lim.Wait(ctx, host, rate)
	elapsed := time.Since(start)

	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Wait error = %v, want context.Canceled", err)
	}
	if elapsed > time.Second {
		t.Fatalf("Wait blocked for %v after cancellation; it should abort at once", elapsed)
	}

	// The cancelled caller gave its reservation back, so the bucket is no worse
	// off than before it asked.
	tokens, err := rdb.HGet(context.Background(), prefix+":rl:"+host, "tokens").Float64()
	if err != nil {
		t.Fatalf("read tokens: %v", err)
	}
	if tokens < -0.001 {
		t.Fatalf("bucket still owes %v tokens; the cancelled reservation leaked", -tokens)
	}
}

func TestRedisLimiterRefusesUnboundedWait(t *testing.T) {
	rdb, prefix := testRedis(t)
	lim := NewRedisLimiter(rdb)
	lim.Prefix = prefix
	lim.MaxWait = 200 * time.Millisecond

	rate := Rate{PerSec: 0.5, Burst: 1}
	host := "maxwait.example"
	ctx := context.Background()

	if err := lim.Wait(ctx, host, rate); err != nil {
		t.Fatalf("priming wait: %v", err)
	}
	start := time.Now()
	err := lim.Wait(ctx, host, rate)
	if !errors.Is(err, ErrRateWaitTooLong) {
		t.Fatalf("error = %v, want ErrRateWaitTooLong", err)
	}
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Fatalf("refusal took %v; it should be immediate", d)
	}

	// A refusal must not have consumed a token, or a busy host would starve.
	tokens, err := rdb.HGet(ctx, prefix+":rl:"+host, "tokens").Float64()
	if err != nil {
		t.Fatalf("read tokens: %v", err)
	}
	if tokens < -0.001 {
		t.Fatalf("refused reservation still took %v tokens", -tokens)
	}
}

func TestRedisLimiterZeroRateIsUnlimited(t *testing.T) {
	rdb, prefix := testRedis(t)
	lim := NewRedisLimiter(rdb)
	lim.Prefix = prefix

	start := time.Now()
	for i := 0; i < 50; i++ {
		if err := lim.Wait(context.Background(), "free.example", Rate{PerSec: 0}); err != nil {
			t.Fatalf("wait: %v", err)
		}
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("an unlimited host took %v for 50 acquisitions", d)
	}
	if n, _ := rdb.Exists(context.Background(), prefix+":rl:free.example").Result(); n != 0 {
		t.Error("a disabled limiter should not write to Redis at all")
	}
}

// TestRedisLimiterHostsAreIndependent guards the key schema: one slow host must
// not throttle another.
func TestRedisLimiterHostsAreIndependent(t *testing.T) {
	rdb, prefix := testRedis(t)
	lim := NewRedisLimiter(rdb)
	lim.Prefix = prefix

	rate := Rate{PerSec: 0.5, Burst: 1}
	ctx := context.Background()
	start := time.Now()
	for i := 0; i < 5; i++ {
		host := fmt.Sprintf("host-%d.example", i)
		if err := lim.Wait(ctx, host, rate); err != nil {
			t.Fatalf("wait on %s: %v", host, err)
		}
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("five distinct hosts took %v; they are sharing a bucket", d)
	}
}
