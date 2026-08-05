package httpx

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
	"github.com/redis/go-redis/v9"
)

// TestRedisBreakerLifecycle walks the whole state machine against a real Redis:
// closed -> open on the Nth consecutive failure -> rejected without a network
// call -> exactly one half-open trial after the cooldown -> closed on success.
func TestRedisBreakerLifecycle(t *testing.T) {
	rdb, prefix := testRedis(t)
	b := NewRedisBreaker(rdb, 3, 150*time.Millisecond)
	b.Prefix = prefix

	ctx := context.Background()
	host := "lifecycle.example"

	// Two failures are not enough.
	for i := 0; i < 2; i++ {
		st, err := b.Allow(ctx, host)
		if err != nil || st != StateClosed {
			t.Fatalf("allow %d = (%v, %v), want closed", i, st, err)
		}
		if err := b.Report(ctx, host, st, false); err != nil {
			t.Fatalf("report: %v", err)
		}
	}
	if st, _ := b.Allow(ctx, host); st != StateClosed {
		t.Fatalf("state = %v after 2 of 3 failures, want closed", st)
	}
	// A success in the middle clears the run.
	_ = b.Report(ctx, host, StateClosed, true)
	for i := 0; i < 2; i++ {
		st, _ := b.Allow(ctx, host)
		_ = b.Report(ctx, host, st, false)
	}
	if st, _ := b.Allow(ctx, host); st != StateClosed {
		t.Fatalf("state = %v; a success should have reset the consecutive failure count", st)
	}

	// Third consecutive failure opens it.
	st, _ := b.Allow(ctx, host)
	_ = b.Report(ctx, host, st, false)
	if st, _ := b.Allow(ctx, host); st != StateOpen {
		t.Fatalf("state = %v after 3 consecutive failures, want open", st)
	}

	// Still open before the cooldown elapses.
	if st, _ := b.Allow(ctx, host); st != StateOpen {
		t.Fatal("breaker closed before the cooldown elapsed")
	}

	time.Sleep(200 * time.Millisecond)

	// Exactly one caller gets the trial, even with the whole fleet asking at once.
	var trials, opens atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			st, err := b.Allow(ctx, host)
			if err != nil {
				t.Errorf("allow: %v", err)
				return
			}
			switch st {
			case StateTrial:
				trials.Add(1)
			case StateOpen:
				opens.Add(1)
			default:
				t.Errorf("unexpected state %v while half-open", st)
			}
		}()
	}
	wg.Wait()
	if trials.Load() != 1 {
		t.Fatalf("%d callers got the half-open trial, want exactly 1", trials.Load())
	}
	if opens.Load() != 11 {
		t.Fatalf("%d callers were rejected, want 11", opens.Load())
	}

	// A successful trial closes the circuit for everyone.
	if err := b.Report(ctx, host, StateTrial, true); err != nil {
		t.Fatalf("report: %v", err)
	}
	if st, _ := b.Allow(ctx, host); st != StateClosed {
		t.Fatalf("state = %v after a successful trial, want closed", st)
	}
}

func TestRedisBreakerFailedTrialReopensImmediately(t *testing.T) {
	rdb, prefix := testRedis(t)
	b := NewRedisBreaker(rdb, 2, 100*time.Millisecond)
	b.Prefix = prefix

	ctx := context.Background()
	host := "reopen.example"

	for i := 0; i < 2; i++ {
		st, _ := b.Allow(ctx, host)
		_ = b.Report(ctx, host, st, false)
	}
	if st, _ := b.Allow(ctx, host); st != StateOpen {
		t.Fatal("breaker did not open")
	}

	time.Sleep(150 * time.Millisecond)
	st, _ := b.Allow(ctx, host)
	if st != StateTrial {
		t.Fatalf("state = %v after cooldown, want trial", st)
	}
	// The trial fails: back to open without waiting for another N failures.
	_ = b.Report(ctx, host, st, false)
	if st, _ := b.Allow(ctx, host); st != StateOpen {
		t.Fatalf("state = %v after a failed trial, want open", st)
	}
}

func TestRedisBreakerIsSharedAcrossClients(t *testing.T) {
	rdb, prefix := testRedis(t)
	ctx := context.Background()
	host := "shared.example"

	// Two breakers over two connections, standing in for two worker processes.
	a := NewRedisBreaker(rdb, 3, time.Minute)
	a.Prefix = prefix
	other := redis.NewClient(rdb.Options())
	t.Cleanup(func() { _ = other.Close() })
	b := NewRedisBreaker(other, 3, time.Minute)
	b.Prefix = prefix

	// Worker A trips the host on its own.
	for i := 0; i < 3; i++ {
		st, _ := a.Allow(ctx, host)
		_ = a.Report(ctx, host, st, false)
	}
	if st, _ := b.Allow(ctx, host); st != StateOpen {
		t.Fatalf("worker B sees %v; breaker state is not shared", st)
	}
	if err := a.Reset(ctx, host); err != nil {
		t.Fatalf("reset: %v", err)
	}
	if st, _ := b.Allow(ctx, host); st != StateClosed {
		t.Fatal("reset did not clear the shared state")
	}
}

// TestClientCircuitOpensAndStopsCallingHost is the end-to-end acceptance case
// against a real Redis: a dead host trips the breaker, and once open the handler
// is never invoked again.
func TestClientCircuitOpensAndStopsCallingHost(t *testing.T) {
	rdb, prefix := testRedis(t)

	var hits atomic.Int32
	var healthy atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if healthy.Load() {
			w.Write([]byte("ok"))
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	br := NewRedisBreaker(rdb, 2, 300*time.Millisecond)
	br.Prefix = prefix

	cfg := testConfig()
	cfg.CircuitThreshold = 2
	cfg.CircuitCooldown = 300 * time.Millisecond
	m := newRecordingMetrics()
	c, err := New(cfg, nil,
		WithLimiter(&stubLimiter{}),
		WithBreaker(br),
		WithRetryPolicy(fastPolicy()),
		WithMetrics(m),
	)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx := context.Background()

	// First Do: attempt 1 fails (1 of 2), attempt 2 fails (2 of 2, opens), attempt
	// 3 is refused by the now-open breaker.
	_, err = c.Do(ctx, mustGet(t, srv.URL+"/jobs/1"))
	if !errors.Is(err, domain.ErrCircuitOpen) {
		t.Fatalf("first Do error = %v, want ErrCircuitOpen once the threshold is crossed", err)
	}
	tripped := hits.Load()
	if tripped != 2 {
		t.Fatalf("handler hits before the circuit opened = %d, want 2 (the threshold)", tripped)
	}

	// Everything after that costs the host nothing.
	for i := 0; i < 5; i++ {
		_, err := c.Do(ctx, mustGet(t, srv.URL+"/jobs/2"))
		if !errors.Is(err, domain.ErrCircuitOpen) {
			t.Fatalf("Do %d error = %v, want ErrCircuitOpen", i, err)
		}
	}
	if hits.Load() != tripped {
		t.Fatalf("handler was invoked %d more times while the circuit was open", hits.Load()-tripped)
	}
	if m.count(OutcomeCircuitOpen) != 6 {
		t.Errorf("circuit_open metric count = %d, want 6", m.count(OutcomeCircuitOpen))
	}

	// After the cooldown the host recovers and one trial closes the circuit.
	healthy.Store(true)
	time.Sleep(350 * time.Millisecond)

	resp, err := c.Do(ctx, mustGet(t, srv.URL+"/jobs/3"))
	if err != nil {
		t.Fatalf("half-open trial: %v", err)
	}
	resp.Body.Close()
	if hits.Load() != tripped+1 {
		t.Fatalf("handler hits = %d, want %d (exactly one trial)", hits.Load(), tripped+1)
	}

	resp, err = c.Do(ctx, mustGet(t, srv.URL+"/jobs/4"))
	if err != nil {
		t.Fatalf("after recovery: %v", err)
	}
	resp.Body.Close()
}
