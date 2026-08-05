package httpx

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

const testUA = "job-match-engine-test/0.1 (+https://example.invalid/contact)"

func TestProductToken(t *testing.T) {
	cases := map[string]string{
		testUA:                     "job-match-engine-test",
		"job-match-engine/0.1":     "job-match-engine",
		"JobMatchEngine":           "jobmatchengine",
		"Mozilla/5.0 (compatible)": "mozilla",
		"":                         "",
	}
	for in, want := range cases {
		if got := productToken(in); got != want {
			t.Errorf("productToken(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestParseRobotsRules(t *testing.T) {
	body := `
# a comment
User-agent: *
Disallow: /private/
Disallow: /*.pdf$
Allow: /private/public-note
Crawl-delay: 3

User-agent: evil-bot
Disallow: /

User-agent: job-match
Disallow: /jobs/draft/
`
	cases := []struct {
		token   string
		path    string
		allowed bool
	}{
		// Our token matches the "job-match" group by prefix, so the wildcard
		// group's rules must not apply to us.
		{"job-match-engine-test", "/private/anything", true},
		{"job-match-engine-test", "/jobs/draft/1", false},
		{"job-match-engine-test", "/jobs/1", true},
		// A crawler that only matches "*".
		{"somebot", "/private/anything", false},
		{"somebot", "/private/public-note", true}, // longer Allow wins
		{"somebot", "/jobs/1", true},
		{"somebot", "/docs/handbook.pdf", false}, // $ anchored suffix rule
		{"somebot", "/docs/handbook.pdf?x=1", true},
		{"somebot", "/", true},
		// Explicitly named, blanket disallow.
		{"evil-bot", "/", false},
		{"evil-bot", "/jobs/1", false},
	}
	for _, tc := range cases {
		rules := parseRobots(body, tc.token)
		if got := rules.allowed(tc.path); got != tc.allowed {
			t.Errorf("token=%q path=%q allowed=%v, want %v", tc.token, tc.path, got, tc.allowed)
		}
	}

	if d := parseRobots(body, "somebot").crawlDelay; d != 3*time.Second {
		t.Errorf("crawlDelay = %v, want 3s", d)
	}
	if d := parseRobots(body, "job-match-engine-test").crawlDelay; d != 0 {
		t.Errorf("our group declares no crawl delay, got %v", d)
	}
}

func TestParseRobotsEdgeCases(t *testing.T) {
	// An empty Disallow means "allow everything".
	if !parseRobots("User-agent: *\nDisallow:", "bot").allowed("/anything") {
		t.Error("empty Disallow should allow everything")
	}
	// No robots.txt at all.
	if !parseRobots("", "bot").allowed("/anything") {
		t.Error("empty robots.txt should allow everything")
	}
	// Garbage lines must not panic or accidentally block.
	if !parseRobots("this is not robots syntax\n\n???", "bot").allowed("/x") {
		t.Error("unparseable robots.txt should not block")
	}
	// Consecutive user-agent lines share one group.
	rules := parseRobots("User-agent: a\nUser-agent: b\nDisallow: /x", "b")
	if rules.allowed("/x") {
		t.Error("grouped user-agent lines should share the group's rules")
	}
	// Rules before any User-agent line belong to nobody.
	if !parseRobots("Disallow: /x\nUser-agent: *\nAllow: /", "bot").allowed("/x") {
		t.Error("a rule outside a group should be ignored")
	}
	// A nil ruleset is permissive.
	var nilRules *robotsRules
	if !nilRules.allowed("/x") {
		t.Error("nil rules should allow")
	}
}

// TestRobotsDeniedMakesNoResourceRequest is the acceptance case: a Disallow rule
// blocks the fetch before any request to the resource, and the robots.txt itself
// is fetched exactly once and then served from cache.
func TestRobotsDeniedMakesNoResourceRequest(t *testing.T) {
	var robotsHits, resourceHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			robotsHits.Add(1)
			w.Write([]byte("User-agent: *\nDisallow: /jobs/\nAllow: /jobs/public/\n"))
			return
		}
		resourceHits.Add(1)
		w.Write([]byte("job description"))
	}))
	defer srv.Close()

	cache := newMemCache()
	cfg := testConfig()
	cfg.RespectRobots = true

	m := newRecordingMetrics()
	c, err := New(cfg, nil,
		WithLimiter(&stubLimiter{}),
		WithBreaker(&stubBreaker{}),
		WithRetryPolicy(fastPolicy()),
		WithRobots(NewRobotsAgent(cache, cfg.UserAgent)),
		WithMetrics(m),
	)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	// Disallowed twice: the second call must not re-fetch robots.txt.
	for i := 0; i < 2; i++ {
		_, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/123"))
		if !errors.Is(err, domain.ErrRobotsDenied) {
			t.Fatalf("call %d: error = %v, want ErrRobotsDenied", i, err)
		}
		var fe *domain.FetchError
		if !errors.As(err, &fe) {
			t.Fatalf("error %v is not a *domain.FetchError", err)
		}
		if domain.StatusFor(err) != domain.StatusRobotsDenied {
			t.Errorf("StatusFor = %q, want robots_denied", domain.StatusFor(err))
		}
		if domain.Retryable(err) {
			t.Error("a robots denial must not be retryable")
		}
	}

	if resourceHits.Load() != 0 {
		t.Fatalf("resource was fetched %d times despite a Disallow rule", resourceHits.Load())
	}
	if robotsHits.Load() != 1 {
		t.Fatalf("robots.txt fetched %d times, want exactly 1 (cached)", robotsHits.Load())
	}
	if m.count(OutcomeRobotsDenied) != 2 {
		t.Errorf("robots_denied metric count = %d, want 2", m.count(OutcomeRobotsDenied))
	}

	// An Allow-listed path under the same prefix still goes through.
	resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/public/1"))
	if err != nil {
		t.Fatalf("allowed path: %v", err)
	}
	resp.Body.Close()
	if resourceHits.Load() != 1 {
		t.Fatalf("resource hits = %d, want 1", resourceHits.Load())
	}
	if robotsHits.Load() != 1 {
		t.Fatalf("robots.txt fetched %d times, want 1", robotsHits.Load())
	}

	// A fresh agent over the same shared cache must not go back to the network:
	// this is the cross-process half of the cache.
	fresh := NewRobotsAgent(cache, cfg.UserAgent)
	c2, err := New(cfg, nil,
		WithLimiter(&stubLimiter{}), WithBreaker(&stubBreaker{}),
		WithRetryPolicy(fastPolicy()), WithRobots(fresh),
	)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if _, err := c2.Do(context.Background(), mustGet(t, srv.URL+"/jobs/999")); !errors.Is(err, domain.ErrRobotsDenied) {
		t.Fatalf("second client: error = %v, want ErrRobotsDenied", err)
	}
	if robotsHits.Load() != 1 {
		t.Fatalf("robots.txt fetched %d times; the shared cache did not serve the second client", robotsHits.Load())
	}
	if cache.sets != 1 {
		t.Errorf("cache writes = %d, want 1", cache.sets)
	}
}

func TestRobotsGateCanBeDisabled(t *testing.T) {
	var robotsHits, resourceHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			robotsHits.Add(1)
			w.Write([]byte("User-agent: *\nDisallow: /\n"))
			return
		}
		resourceHits.Add(1)
		w.Write([]byte("ok"))
	}))
	defer srv.Close()

	cfg := testConfig() // RespectRobots false
	c, err := New(cfg, nil, WithLimiter(&stubLimiter{}), WithBreaker(&stubBreaker{}), WithRetryPolicy(fastPolicy()))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()
	if robotsHits.Load() != 0 {
		t.Errorf("robots.txt fetched %d times with RespectRobots=false", robotsHits.Load())
	}
	if resourceHits.Load() != 1 {
		t.Errorf("resource hits = %d, want 1", resourceHits.Load())
	}
}

func TestRobotsMissingOrBrokenFailsOpen(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		wantCached bool
	}{
		{"404 no robots file", http.StatusNotFound, true},
		{"500 server error", http.StatusInternalServerError, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var resourceHits atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/robots.txt" {
					w.WriteHeader(tc.status)
					return
				}
				resourceHits.Add(1)
				w.Write([]byte("ok"))
			}))
			defer srv.Close()

			cache := newMemCache()
			cfg := testConfig()
			cfg.RespectRobots = true
			c, err := New(cfg, nil,
				WithLimiter(&stubLimiter{}), WithBreaker(&stubBreaker{}),
				WithRetryPolicy(fastPolicy()), WithRobots(NewRobotsAgent(cache, cfg.UserAgent)))
			if err != nil {
				t.Fatalf("New: %v", err)
			}
			resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
			if err != nil {
				t.Fatalf("Do: %v", err)
			}
			resp.Body.Close()
			if resourceHits.Load() != 1 {
				t.Errorf("resource hits = %d, want 1 (fail open)", resourceHits.Load())
			}
			if got := cache.sets > 0; got != tc.wantCached {
				t.Errorf("cache write = %v, want %v (a 5xx must not poison the shared cache)", got, tc.wantCached)
			}
		})
	}
}

func TestRobotsCrawlDelayTightensRate(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			w.Write([]byte("User-agent: *\nCrawl-delay: 10\n"))
			return
		}
		w.Write([]byte("ok"))
	}))
	defer srv.Close()

	rec := &rateRecorder{}
	cfg := testConfig()
	cfg.RespectRobots = true
	cfg.HostRatePerSec = 0.5 // one per 2s; robots asks for one per 10s
	c, err := New(cfg, nil,
		WithLimiter(rec), WithBreaker(&stubBreaker{}), WithRetryPolicy(fastPolicy()),
		WithRobots(NewRobotsAgent(newMemCache(), cfg.UserAgent)))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	resp, err := c.Do(context.Background(), mustGet(t, srv.URL+"/jobs/1"))
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	last := rec.last()
	if last.PerSec != 0.1 {
		t.Fatalf("effective rate = %v/s, want 0.1/s from Crawl-delay: 10", last.PerSec)
	}
}

type rateRecorder struct {
	stubLimiter
	mu    sync.Mutex
	rates []Rate
}

func (r *rateRecorder) Wait(ctx context.Context, host string, rate Rate) error {
	r.mu.Lock()
	r.rates = append(r.rates, rate)
	r.mu.Unlock()
	return r.stubLimiter.Wait(ctx, host, rate)
}

func (r *rateRecorder) last() Rate {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.rates) == 0 {
		return Rate{}
	}
	return r.rates[len(r.rates)-1]
}

func TestPathAndQuery(t *testing.T) {
	cases := map[string]string{
		"http://h/jobs/1":         "/jobs/1",
		"http://h":                "/",
		"http://h/jobs/1?x=2&y=3": "/jobs/1?x=2&y=3",
		"http://h/a%20b":          "/a%20b",
	}
	for in, want := range cases {
		u, err := url.Parse(in)
		if err != nil {
			t.Fatal(err)
		}
		if got := pathAndQuery(u); got != want {
			t.Errorf("pathAndQuery(%q) = %q, want %q", in, got, want)
		}
	}
}
