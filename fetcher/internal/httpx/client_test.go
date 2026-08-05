package httpx

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/config"
	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// ---------------------------------------------------------------------------
// Test doubles. The Redis-backed implementations are exercised in
// ratelimit_test.go and breaker_test.go; these keep the pure client-logic tests
// runnable with no server.
// ---------------------------------------------------------------------------

type stubLimiter struct {
	calls atomic.Int32
	// block, when non-nil, makes Wait park until it is closed or ctx is done.
	block chan struct{}
	err   error
}

func (s *stubLimiter) Wait(ctx context.Context, host string, r Rate) error {
	s.calls.Add(1)
	if s.err != nil {
		return s.err
	}
	if s.block != nil {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.block:
		}
	}
	return ctx.Err()
}

type stubBreaker struct {
	mu      sync.Mutex
	state   BreakerState
	allowed int
	reports []bool
}

func (s *stubBreaker) Allow(context.Context, string) (BreakerState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.allowed++
	if s.state == "" {
		return StateClosed, nil
	}
	return s.state, nil
}

func (s *stubBreaker) Report(_ context.Context, _ string, _ BreakerState, healthy bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reports = append(s.reports, healthy)
	return nil
}

func (s *stubBreaker) healthReports() []bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]bool(nil), s.reports...)
}

type memCache struct {
	mu   sync.Mutex
	m    map[string]string
	sets int
	gets int
}

func newMemCache() *memCache { return &memCache{m: map[string]string{}} }

func (c *memCache) Get(_ context.Context, key string) (string, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.gets++
	v, ok := c.m[key]
	return v, ok, nil
}

func (c *memCache) Set(_ context.Context, key, val string, _ time.Duration) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sets++
	c.m[key] = val
	return nil
}

type recordingMetrics struct {
	mu   sync.Mutex
	obs  []metricObs
	seen map[string]int
}

type metricObs struct {
	host    string
	status  string
	seconds float64
}

func newRecordingMetrics() *recordingMetrics {
	return &recordingMetrics{seen: map[string]int{}}
}

func (m *recordingMetrics) ObserveFetch(host, status string, seconds float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.obs = append(m.obs, metricObs{host, status, seconds})
	m.seen[status]++
}

func (m *recordingMetrics) count(status string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.seen[status]
}

func (m *recordingMetrics) all() []metricObs {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]metricObs(nil), m.obs...)
}

// testConfig is config.Load with the knobs tests care about pinned, so a stray
// JME_ variable in the developer's shell cannot change a test's meaning.
func testConfig() config.Config {
	c := config.Load()
	c.UserAgent = "job-match-engine-test/0.1 (+https://example.invalid/contact)"
	c.RespectRobots = false
	c.FetchTimeout = 5 * time.Second
	c.HostRatePerSec = 0
	c.HostBurst = 1
	return c
}

// fastPolicy retries quickly so the classification tests do not sleep for real.
func fastPolicy() RetryPolicy {
	return RetryPolicy{
		MaxAttempts:   3,
		Base:          time.Millisecond,
		Max:           5 * time.Millisecond,
		MaxTotalWait:  time.Second,
		MaxRetryAfter: 5 * time.Second,
	}
}

func newTestClient(t *testing.T, opts ...Option) *Client {
	t.Helper()
	base := []Option{
		WithLimiter(&stubLimiter{}),
		WithBreaker(&stubBreaker{}),
		WithRetryPolicy(fastPolicy()),
	}
	c, err := New(testConfig(), nil, append(base, opts...)...)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func mustGet(t *testing.T, rawURL string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, rawURL, nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	return req
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

func TestDoSendsHonestUserAgent(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("User-Agent")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := newTestClient(t)
	req := mustGet(t, srv.URL+"/jobs/1")
	// Even a caller that sets its own UA gets overwritten: the honest identifier
	// is not negotiable.
	req.Header.Set("User-Agent", "curl/8.0")

	resp, err := c.Do(context.Background(), req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if got != c.UserAgent() {
		t.Fatalf("User-Agent = %q, want %q", got, c.UserAgent())
	}
	if !strings.Contains(got, "+https://") {
		t.Errorf("user agent %q carries no contact URL", got)
	}
}

func TestDoMapsStatusOntoDomainSentinels(t *testing.T) {
	cases := []struct {
		name     string
		status   int
		want     error
		wantHits int // handler invocations, i.e. whether we retried
		healthy  bool
	}{
		{"ok", http.StatusOK, nil, 1, true},
		{"not found", http.StatusNotFound, domain.ErrNotFound, 1, true},
		{"gone", http.StatusGone, domain.ErrNotFound, 1, true},
		{"forbidden", http.StatusForbidden, domain.ErrPermanent, 1, true},
		{"teapot", http.StatusTeapot, domain.ErrPermanent, 1, true},
		{"rate limited", http.StatusTooManyRequests, domain.ErrRateLimited, 3, false},
		{"server error", http.StatusInternalServerError, domain.ErrTransient, 3, false},
		{"bad gateway", http.StatusBadGateway, domain.ErrTransient, 3, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var hits atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				hits.Add(1)
				w.WriteHeader(tc.status)
			}))
			defer srv.Close()

			br := &stubBreaker{}
			c := newTestClient(t, WithBreaker(br))
			resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
			if resp != nil {
				resp.Body.Close()
			}

			if tc.want == nil {
				if err != nil {
					t.Fatalf("Do: unexpected error %v", err)
				}
			} else {
				if !errors.Is(err, tc.want) {
					t.Fatalf("Do error = %v, want errors.Is(..., %v)", err, tc.want)
				}
				var fe *domain.FetchError
				if !errors.As(err, &fe) {
					t.Fatalf("error %v is not a *domain.FetchError", err)
				}
				if fe.StatusCode != tc.status {
					t.Errorf("FetchError.StatusCode = %d, want %d", fe.StatusCode, tc.status)
				}
			}
			if int(hits.Load()) != tc.wantHits {
				t.Errorf("handler hits = %d, want %d", hits.Load(), tc.wantHits)
			}
			// The breaker must see the host as healthy for honest 4xx answers and
			// unhealthy for 429/5xx.
			for _, h := range br.healthReports() {
				if h != tc.healthy {
					t.Errorf("breaker health report = %v, want %v", h, tc.healthy)
				}
			}
		})
	}
}

func TestDoRetriesRecoverableFailure(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ok"))
	}))
	defer srv.Close()

	c := newTestClient(t)
	resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if hits.Load() != 2 {
		t.Fatalf("handler hits = %d, want 2", hits.Load())
	}
}

// TestDoHonoursRetryAfter is the acceptance case: a 429 carrying Retry-After
// parks us for at least that long, and the retry succeeds.
func TestDoHonoursRetryAfter(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.Write([]byte("ok"))
	}))
	defer srv.Close()

	// Backoff base is 1ms here, so anything close to a second can only have come
	// from the header.
	c := newTestClient(t)

	start := time.Now()
	resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if elapsed < 950*time.Millisecond {
		t.Fatalf("elapsed %v, want >= ~1s from Retry-After", elapsed)
	}
	if elapsed > 5*time.Second {
		t.Fatalf("elapsed %v, wildly longer than the requested 1s", elapsed)
	}
	if hits.Load() != 2 {
		t.Fatalf("handler hits = %d, want 2", hits.Load())
	}
}

func TestDoGivesUpWhenRetryAfterExceedsCap(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()

	c := newTestClient(t)
	start := time.Now()
	_, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
	if !errors.Is(err, domain.ErrRateLimited) {
		t.Fatalf("error = %v, want ErrRateLimited", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("waited %v; a one hour Retry-After must not park a worker", elapsed)
	}
	if hits.Load() != 1 {
		t.Fatalf("handler hits = %d, want 1 (no retry after an over-cap Retry-After)", hits.Load())
	}
	if !domain.Retryable(err) {
		t.Error("ErrRateLimited should stay retryable at the queue level")
	}
	if got := domain.StatusFor(err); got != domain.StatusRateLimited {
		t.Errorf("StatusFor = %q, want %q", got, domain.StatusRateLimited)
	}
}

// TestCircuitOpenMakesNoNetworkCall is the acceptance case for the breaker: while
// open, Do must return domain.ErrCircuitOpen and the origin must never be touched.
func TestCircuitOpenMakesNoNetworkCall(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	lim := &stubLimiter{}
	m := newRecordingMetrics()
	c := newTestClient(t, WithBreaker(&stubBreaker{state: StateOpen}), WithLimiter(lim), WithMetrics(m))

	for i := 0; i < 3; i++ {
		_, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
		if !errors.Is(err, domain.ErrCircuitOpen) {
			t.Fatalf("Do error = %v, want ErrCircuitOpen", err)
		}
	}
	if hits.Load() != 0 {
		t.Fatalf("handler was invoked %d times while the circuit was open", hits.Load())
	}
	// Not even the rate limiter should be consulted: an open circuit costs
	// nothing, including tokens.
	if lim.calls.Load() != 0 {
		t.Fatalf("limiter consulted %d times while the circuit was open", lim.calls.Load())
	}
	if got := m.count(OutcomeCircuitOpen); got != 3 {
		t.Errorf("circuit_open metric count = %d, want 3", got)
	}
	if got := domain.StatusFor(&domain.FetchError{Kind: domain.ErrCircuitOpen}); got != domain.StatusTransientError {
		t.Errorf("StatusFor(ErrCircuitOpen) = %q, want transient", got)
	}
}

// TestContextCancelAbortsRateLimitWait: a job cancelled while queued behind the
// token bucket must abort immediately and never reach the network.
func TestContextCancelAbortsRateLimitWait(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	lim := &stubLimiter{block: make(chan struct{})} // never released
	c := newTestClient(t, WithLimiter(lim))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := c.Do(ctx, mustGet(t, srv.URL+"/jobs/1"))
		done <- err
	}()

	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Do error = %v, want context.Canceled", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Do did not return after the context was cancelled")
	}
	if hits.Load() != 0 {
		t.Fatalf("handler invoked %d times despite cancellation during the rate limit wait", hits.Load())
	}
}

func TestContextCancelAbortsBackoffSleep(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	slow := fastPolicy()
	slow.Base = 5 * time.Second
	slow.Max = 5 * time.Second
	slow.MaxTotalWait = time.Minute // the cap must not be what saves us here
	c := newTestClient(t, WithRetryPolicy(slow), WithRand(func() float64 { return 1 }))

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := c.Do(ctx, mustGet(t, srv.URL+"/jobs/1"))
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error = %v, want context.DeadlineExceeded", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("backoff sleep ignored cancellation for %v", elapsed)
	}
}

func TestMetricsObserveEveryAttempt(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	m := newRecordingMetrics()
	c := newTestClient(t, WithMetrics(m))
	_, _ = c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))

	obs := m.all()
	if len(obs) != 3 {
		t.Fatalf("observations = %d, want 3 (one per attempt)", len(obs))
	}
	u, _ := url.Parse(srv.URL)
	for _, o := range obs {
		if o.status != "500" {
			t.Errorf("status label = %q, want 500", o.status)
		}
		if o.host != hostKey(u) {
			t.Errorf("host label = %q, want %q", o.host, hostKey(u))
		}
		if o.seconds < 0 {
			t.Errorf("negative latency %v", o.seconds)
		}
	}
}

func TestTransportErrorIsTransient(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	addr := srv.URL
	srv.Close() // nothing is listening now

	m := newRecordingMetrics()
	c := newTestClient(t, WithMetrics(m))
	_, err := c.Do(context.Background(), mustGet(t, addr+"/jobs/1"))
	if !errors.Is(err, domain.ErrTransient) {
		t.Fatalf("error = %v, want ErrTransient", err)
	}
	if m.count(OutcomeError) == 0 {
		t.Error("no error-labelled metric recorded for a transport failure")
	}
}

func TestNewRequiresRedisOrInjectedDependencies(t *testing.T) {
	if _, err := New(testConfig(), nil); err == nil {
		t.Fatal("New with a nil redis client and no injected limiter should fail")
	}
}

func TestRateForHostOverride(t *testing.T) {
	c := newTestClient(t, WithHostRate("Boards.Greenhouse.IO", Rate{PerSec: 2, Burst: 3}))
	if got := c.rateFor("boards.greenhouse.io"); got.PerSec != 2 || got.Burst != 3 {
		t.Fatalf("rateFor override = %+v, want {2 3}", got)
	}
	if got := c.rateFor("jobs.lever.co"); got.PerSec != c.defaultRate.PerSec {
		t.Fatalf("rateFor default = %+v, want %+v", got, c.defaultRate)
	}
}

func TestExpectedMinDuration(t *testing.T) {
	// The shipping default: one request every two seconds, no bursting.
	def := Rate{PerSec: 0.5, Burst: 1}
	if got, want := ExpectedMinDuration(4, def), 6*time.Second; got != want {
		t.Errorf("ExpectedMinDuration(4, 0.5/s) = %v, want %v", got, want)
	}
	if got := ExpectedMinDuration(1, def); got != 0 {
		t.Errorf("first request should not wait, got %v", got)
	}
	if got := ExpectedMinDuration(10, Rate{PerSec: 0}); got != 0 {
		t.Errorf("disabled limiter should expect no wait, got %v", got)
	}
}
