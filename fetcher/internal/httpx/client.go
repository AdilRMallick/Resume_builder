// Package httpx is the polite HTTP client every ATS adapter fetches through.
//
// It exists because the fetcher runs as several worker processes against a handful of
// ATS hosts. Per-process rate limiting would multiply the request rate by the worker
// count, so the token bucket, the circuit breaker, and the robots.txt cache all keep
// their state in Redis. Two workers on two machines share one budget per host.
//
// The public surface is deliberately tiny:
//
//	c, err := httpx.New(cfg, rdb)
//	resp, err := c.Do(ctx, req)
//
// which satisfies the Doer interface the adapters package depends on.
package httpx

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/config"
	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
	"github.com/redis/go-redis/v9"
)

// Doer is the contract the adapters package consumes. Client satisfies it.
type Doer interface {
	Do(ctx context.Context, req *http.Request) (*http.Response, error)
}

var _ Doer = (*Client)(nil)

// Metrics is the injectable telemetry hook. Status is the HTTP status code as a
// string for completed requests, or one of the outcome labels below for requests
// that never produced a response. Seconds is the wall time of the attempt,
// excluding time spent blocked on the rate limiter.
type Metrics interface {
	ObserveFetch(host, status string, seconds float64)
}

// Outcome labels passed to Metrics.ObserveFetch when there is no HTTP status.
const (
	OutcomeError        = "error"          // transport failure
	OutcomeCircuitOpen  = "circuit_open"   // rejected by the breaker, no network call
	OutcomeRobotsDenied = "robots_denied"  // rejected by robots.txt, no network call
	OutcomeRateLimitOut = "rate_limit_out" // gave up waiting for a token
)

// NopMetrics is the default: measure nothing.
type NopMetrics struct{}

func (NopMetrics) ObserveFetch(string, string, float64) {}

// Rate is a token bucket configuration for one host.
type Rate struct {
	// PerSec is the sustained refill rate in tokens per second. 0.5 means one
	// request every two seconds. Zero or negative disables limiting.
	PerSec float64
	// Burst is the bucket capacity. 1 means no bursting at all.
	Burst int
}

// Client is a rate limited, circuit broken, robots respecting HTTP client.
// Safe for concurrent use.
type Client struct {
	cfg     config.Config
	http    *http.Client
	limiter Limiter
	breaker Breaker
	robots  *RobotsAgent
	metrics Metrics
	policy  RetryPolicy

	userAgent     string
	defaultRate   Rate
	hostRates     map[string]Rate
	respectRobots bool

	now  func() time.Time
	rand func() float64
}

// Option customises a Client.
type Option func(*Client)

// WithMetrics installs a telemetry hook. Default is NopMetrics.
func WithMetrics(m Metrics) Option {
	return func(c *Client) {
		if m != nil {
			c.metrics = m
		}
	}
}

// WithTransport swaps the http.RoundTripper, mostly for tests.
func WithTransport(rt http.RoundTripper) Option {
	return func(c *Client) {
		if rt != nil {
			c.http.Transport = rt
		}
	}
}

// WithHTTPClient replaces the underlying *http.Client wholesale. The client's own
// Timeout is left alone; per-attempt deadlines come from config.FetchTimeout.
func WithHTTPClient(h *http.Client) Option {
	return func(c *Client) {
		if h != nil {
			c.http = h
		}
	}
}

// WithHostRate overrides the token bucket for a single host. The key is the URL
// host, including the port when the URL carries one ("boards.greenhouse.io",
// "127.0.0.1:8080").
func WithHostRate(host string, r Rate) Option {
	return func(c *Client) {
		c.hostRates[strings.ToLower(host)] = r
	}
}

// WithRetryPolicy replaces the backoff policy.
func WithRetryPolicy(p RetryPolicy) Option {
	return func(c *Client) { c.policy = p }
}

// WithLimiter injects a Limiter, replacing the Redis one.
func WithLimiter(l Limiter) Option {
	return func(c *Client) {
		if l != nil {
			c.limiter = l
		}
	}
}

// WithBreaker injects a Breaker, replacing the Redis one.
func WithBreaker(b Breaker) Option {
	return func(c *Client) {
		if b != nil {
			c.breaker = b
		}
	}
}

// WithRobots injects a RobotsAgent, replacing the Redis backed one.
func WithRobots(r *RobotsAgent) Option {
	return func(c *Client) {
		if r != nil {
			c.robots = r
		}
	}
}

// WithClock overrides time.Now, for deterministic tests.
func WithClock(now func() time.Time) Option {
	return func(c *Client) {
		if now != nil {
			c.now = now
		}
	}
}

// WithRand overrides the jitter source. Must return a value in [0, 1).
func WithRand(f func() float64) Option {
	return func(c *Client) {
		if f != nil {
			c.rand = f
		}
	}
}

// New builds a Client. rdb may be nil only if a Limiter, a Breaker and a
// RobotsAgent are all supplied through options.
func New(cfg config.Config, rdb redis.UniversalClient, opts ...Option) (*Client, error) {
	c := &Client{
		cfg:           cfg,
		http:          &http.Client{},
		metrics:       NopMetrics{},
		policy:        DefaultRetryPolicy(),
		userAgent:     cfg.UserAgent,
		defaultRate:   Rate{PerSec: cfg.HostRatePerSec, Burst: cfg.HostBurst},
		hostRates:     map[string]Rate{},
		respectRobots: cfg.RespectRobots,
		now:           time.Now,
		rand:          rand.Float64,
	}
	if c.userAgent == "" {
		c.userAgent = "job-match-engine/0.1"
	}
	if c.defaultRate.Burst <= 0 {
		c.defaultRate.Burst = 1
	}
	// Never follow redirects blindly into another host's rate budget; one hop is
	// fine, the adapters mostly hit JSON APIs that do not redirect.
	c.http.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 5 {
			return errors.New("stopped after 5 redirects")
		}
		req.Header.Set("User-Agent", c.userAgent)
		return nil
	}

	for _, o := range opts {
		o(c)
	}

	if c.limiter == nil {
		if rdb == nil {
			return nil, errors.New("httpx: nil redis client and no Limiter supplied")
		}
		c.limiter = NewRedisLimiter(rdb)
	}
	if c.breaker == nil {
		if rdb == nil {
			return nil, errors.New("httpx: nil redis client and no Breaker supplied")
		}
		c.breaker = NewRedisBreaker(rdb, cfg.CircuitThreshold, cfg.CircuitCooldown)
	}
	if c.robots == nil && c.respectRobots {
		if rdb == nil {
			return nil, errors.New("httpx: nil redis client and no RobotsAgent supplied")
		}
		c.robots = NewRobotsAgent(NewRedisCache(rdb), c.userAgent)
	}
	if c.robots != nil && c.robots.Fetch == nil {
		// robots.txt goes out over the same rate budget and the same user agent,
		// but skips the breaker and, obviously, the robots check itself.
		c.robots.Fetch = c.fetchRobots
	}
	return c, nil
}

// UserAgent returns the honest identifier sent on every request.
func (c *Client) UserAgent() string { return c.userAgent }

// Do performs req, blocking as long as necessary to stay inside the host's token
// bucket. Errors are always *domain.FetchError wrapping one of the domain
// sentinels, except for context cancellation which is returned as-is so callers
// can tell "we gave up" from "the host misbehaved".
//
// On success the caller owns the response body and must close it.
func (c *Client) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	if req == nil || req.URL == nil {
		return nil, c.errf(domain.ErrPermanent, 0, "", errors.New("nil request"))
	}
	host := hostKey(req.URL)
	if host == "" {
		return nil, c.errf(domain.ErrPermanent, 0, req.URL.String(), errors.New("request URL has no host"))
	}
	rawURL := req.URL.String()

	rate := c.rateFor(host)

	// robots.txt first: a disallowed path must cost the target host nothing.
	if c.respectRobots && c.robots != nil {
		allowed, crawlDelay, err := c.robots.Check(ctx, req.URL)
		if err != nil && ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if err == nil && !allowed {
			c.metrics.ObserveFetch(host, OutcomeRobotsDenied, 0)
			return nil, c.errf(domain.ErrRobotsDenied, 0, rawURL,
				fmt.Errorf("robots.txt disallows %s for %s", req.URL.Path, c.userAgent))
		}
		// A Crawl-delay stricter than our configured rate wins. Politeness only
		// ever ratchets down.
		if crawlDelay > 0 {
			if perSec := 1 / crawlDelay.Seconds(); perSec < rate.PerSec || rate.PerSec <= 0 {
				rate = Rate{PerSec: perSec, Burst: 1}
			}
		}
	}

	// Buffer the body once so retries can replay it. Adapters send GETs, but a
	// silently unretryable POST would be a nasty surprise later.
	if req.Body != nil && req.GetBody == nil {
		body, err := io.ReadAll(req.Body)
		req.Body.Close()
		if err != nil {
			return nil, c.errf(domain.ErrPermanent, 0, rawURL, fmt.Errorf("read request body: %w", err))
		}
		req.Body = io.NopCloser(strings.NewReader(string(body)))
		req.GetBody = func() (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader(string(body))), nil
		}
	}

	var (
		lastErr   error
		totalWait time.Duration
	)
	for attempt := 0; attempt < c.policy.MaxAttempts; attempt++ {
		state, err := c.breaker.Allow(ctx, host)
		if err != nil {
			return nil, c.errf(domain.ErrTransient, 0, rawURL, fmt.Errorf("circuit breaker: %w", err))
		}
		if state == StateOpen {
			c.metrics.ObserveFetch(host, OutcomeCircuitOpen, 0)
			return nil, c.errf(domain.ErrCircuitOpen, 0, rawURL,
				fmt.Errorf("host %s circuit is open", host))
		}

		if err := c.limiter.Wait(ctx, host, rate); err != nil {
			if ctx.Err() != nil {
				return nil, err
			}
			c.metrics.ObserveFetch(host, OutcomeRateLimitOut, 0)
			return nil, c.errf(domain.ErrRateLimited, 0, rawURL, err)
		}

		resp, elapsed, doErr := c.attempt(ctx, req)

		var status string
		if resp != nil {
			status = fmt.Sprintf("%d", resp.StatusCode)
		} else {
			status = OutcomeError
		}
		c.metrics.ObserveFetch(host, status, elapsed.Seconds())

		outcome := classify(resp, doErr)
		_ = c.breaker.Report(ctx, host, state, outcome.healthy)

		if outcome.kind == nil {
			return resp, nil
		}

		lastErr = c.errf(outcome.kind, outcome.status, rawURL, outcome.err)

		if !outcome.retry || attempt == c.policy.MaxAttempts-1 {
			drain(resp)
			return nil, lastErr
		}

		wait, ok := c.policy.Delay(attempt, resp, c.rand)
		drain(resp)
		if !ok {
			return nil, lastErr
		}
		if totalWait+wait > c.policy.MaxTotalWait {
			return nil, lastErr
		}
		totalWait += wait
		if err := sleepCtx(ctx, wait); err != nil {
			return nil, err
		}
	}
	if lastErr == nil {
		lastErr = c.errf(domain.ErrTransient, 0, rawURL, errors.New("no attempts made"))
	}
	return nil, lastErr
}

// attempt performs exactly one HTTP round trip under the per-request timeout.
// The returned response body stays open; closing it also releases the timeout.
func (c *Client) attempt(ctx context.Context, req *http.Request) (*http.Response, time.Duration, error) {
	reqCtx := ctx
	cancel := context.CancelFunc(func() {})
	if c.cfg.FetchTimeout > 0 {
		reqCtx, cancel = context.WithTimeout(ctx, c.cfg.FetchTimeout)
	}

	r := req.Clone(reqCtx)
	if req.GetBody != nil {
		body, err := req.GetBody()
		if err != nil {
			cancel()
			return nil, 0, fmt.Errorf("rewind request body: %w", err)
		}
		r.Body = body
	}
	r.Header.Set("User-Agent", c.userAgent)

	start := c.now()
	resp, err := c.http.Do(r)
	elapsed := c.now().Sub(start)
	if err != nil {
		cancel()
		return nil, elapsed, err
	}
	// Tie the timeout's lifetime to the body so the caller can read it after we
	// return, and cancelling on Close does not leak the context.
	resp.Body = &cancelReader{ReadCloser: resp.Body, cancel: cancel}
	return resp, elapsed, nil
}

// fetchRobots is the RobotsAgent's network hook. It uses the same rate budget as
// everything else (a robots.txt request is still a request to that host) but
// bypasses the breaker and, obviously, the robots check itself.
func (c *Client) fetchRobots(ctx context.Context, robotsURL string) (string, int, error) {
	u, err := url.Parse(robotsURL)
	if err != nil {
		return "", 0, err
	}
	if err := c.limiter.Wait(ctx, hostKey(u), c.rateFor(hostKey(u))); err != nil {
		return "", 0, err
	}
	reqCtx := ctx
	cancel := context.CancelFunc(func() {})
	if c.cfg.FetchTimeout > 0 {
		reqCtx, cancel = context.WithTimeout(ctx, c.cfg.FetchTimeout)
	}
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, robotsURL, nil)
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("User-Agent", c.userAgent)

	start := c.now()
	resp, err := c.http.Do(req)
	if err != nil {
		c.metrics.ObserveFetch(hostKey(u), OutcomeError, c.now().Sub(start).Seconds())
		return "", 0, err
	}
	defer resp.Body.Close()
	c.metrics.ObserveFetch(hostKey(u), fmt.Sprintf("%d", resp.StatusCode), c.now().Sub(start).Seconds())

	// Cap the read: a hostile or broken robots.txt should not eat memory.
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxRobotsBytes))
	if err != nil {
		return "", resp.StatusCode, err
	}
	return string(body), resp.StatusCode, nil
}

func (c *Client) rateFor(host string) Rate {
	if r, ok := c.hostRates[host]; ok {
		if r.Burst <= 0 {
			r.Burst = 1
		}
		return r
	}
	return c.defaultRate
}

func (c *Client) errf(kind error, status int, rawURL string, err error) error {
	return &domain.FetchError{
		Kind:       kind,
		StatusCode: status,
		Adapter:    "httpx",
		URL:        rawURL,
		Err:        err,
	}
}

// ---------------------------------------------------------------------------
// Outcome classification. Every HTTP result maps onto exactly one existing
// domain sentinel; this package invents no error taxonomy of its own.
// ---------------------------------------------------------------------------

type outcome struct {
	kind    error // nil means success
	status  int
	err     error
	retry   bool // worth another attempt
	healthy bool // does this count as the host being up, for the breaker
}

func classify(resp *http.Response, err error) outcome {
	if err != nil {
		return outcome{
			kind:  domain.ErrTransient,
			err:   fmt.Errorf("transport: %w", err),
			retry: true,
		}
	}
	code := resp.StatusCode
	switch {
	case code >= 200 && code < 400:
		return outcome{healthy: true}
	case code == http.StatusNotFound || code == http.StatusGone:
		// The host answered correctly; the posting is simply gone.
		return outcome{kind: domain.ErrNotFound, status: code, err: errors.New(resp.Status), healthy: true}
	case code == http.StatusTooManyRequests:
		return outcome{kind: domain.ErrRateLimited, status: code, err: errors.New(resp.Status), retry: true}
	case code == http.StatusRequestTimeout || code == http.StatusTooEarly:
		return outcome{kind: domain.ErrTransient, status: code, err: errors.New(resp.Status), retry: true, healthy: true}
	case code >= 500:
		return outcome{kind: domain.ErrTransient, status: code, err: errors.New(resp.Status), retry: true}
	default:
		// 4xx that is not 404/410/429: auth walls, bad shapes. Retrying will not
		// help, and the host itself is healthy.
		return outcome{kind: domain.ErrPermanent, status: code, err: errors.New(resp.Status), healthy: true}
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// hostKey is the rate limit / breaker / robots partition key. It keeps the port
// so two test servers on 127.0.0.1 do not share a bucket.
func hostKey(u *url.URL) string { return strings.ToLower(u.Host) }

type cancelReader struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (c *cancelReader) Close() error {
	err := c.ReadCloser.Close()
	c.cancel()
	return err
}

// drain closes a response we are discarding, reading a little first so the
// keep-alive connection can be reused.
func drain(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
	_ = resp.Body.Close()
}
