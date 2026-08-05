// Package adapters turns a job posting URL into readable job description text.
//
// Every applicant tracking system (ATS) gets one Adapter. Adapters that have a
// public JSON API use it; scraping HTML for those is both ruder and more brittle
// than asking the documented endpoint. Anything unrecognised falls through to
// FallbackAdapter, which fetches the page and runs a readability-style main
// content extraction.
//
// Adapters do not own an HTTP client. They are handed a Doer, which the fetcher
// service satisfies with the rate-limited, robots-respecting client from
// internal/httpx. That keeps this package free of rate limiting, retry, and
// circuit breaker concerns, and keeps the tests free of live network.
package adapters

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// Doer is the sliver of *http.Client that adapters need. internal/httpx provides
// the real implementation (per-host token bucket, backoff, circuit breaker); the
// tests here provide http.DefaultClient pointed at an httptest.Server.
type Doer interface {
	Do(req *http.Request) (*http.Response, error)
}

// Adapter resolves one family of posting URLs to a JobDescription.
type Adapter interface {
	// Name is the stable identifier recorded in posting_jd.adapter and used for
	// the per-adapter coverage metric. Never change one without a migration.
	Name() string

	// Detect reports whether this adapter handles the URL. Host based, cheap,
	// no network.
	Detect(u *url.URL) bool

	// Fetch resolves the posting. It must honour ctx for both timeout and
	// cancellation, and must return errors wrapped in *domain.FetchError.
	Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error)
}

// maxBodyBytes caps how much of a response we will read. Job descriptions are
// kilobytes; anything past this is a misrouted download or a hostile response.
const maxBodyBytes = 8 << 20 // 8 MiB

// ---------------------------------------------------------------------------
// Construction options
// ---------------------------------------------------------------------------

// settings carries per-adapter construction knobs shared by the API adapters.
type settings struct {
	baseURL string
	// altBaseURL is the origin of a second endpoint. Only Ashby uses it: its
	// posting API and its job board GraphQL endpoint live on different hosts.
	altBaseURL string
}

// Option customises an adapter at construction time.
type Option func(*settings)

// WithBaseURL overrides the upstream API origin. Production never sets this; it
// exists so tests can point an adapter at an httptest.Server serving recorded
// fixtures. The value is an origin such as "http://127.0.0.1:1234" and must not
// carry a trailing slash.
func WithBaseURL(base string) Option {
	return func(s *settings) { s.baseURL = strings.TrimRight(base, "/") }
}

// WithAltBaseURL overrides the secondary endpoint origin (Ashby's GraphQL host).
// Tests only, same contract as WithBaseURL.
func WithAltBaseURL(base string) Option {
	return func(s *settings) { s.altBaseURL = strings.TrimRight(base, "/") }
}

func newSettings(defaultBase, defaultAlt string, opts []Option) settings {
	s := settings{baseURL: defaultBase, altBaseURL: defaultAlt}
	for _, opt := range opts {
		opt(&s)
	}
	return s
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

// ctxAwareError keeps a *domain.FetchError and a second cause both reachable
// through errors.Is / errors.As. It exists so that a fetch killed by context
// cancellation satisfies errors.Is(err, context.Canceled) for the caller's retry
// logic while still satisfying errors.Is(err, domain.ErrTransient) for the
// fetch_status mapping. domain.FetchError only unwraps to its Kind, so without
// this the context cause would be invisible to errors.Is.
type ctxAwareError struct {
	fe  *domain.FetchError
	aux error
}

func (e *ctxAwareError) Error() string   { return e.fe.Error() }
func (e *ctxAwareError) Unwrap() []error { return []error{e.fe, e.aux} }

// fetchErr builds the standard adapter error.
func fetchErr(kind error, name, rawURL string, status int, cause error) error {
	return &domain.FetchError{Kind: kind, StatusCode: status, Adapter: name, URL: rawURL, Err: cause}
}

// permanentf is shorthand for a malformed or unusable response body.
func permanentf(name, rawURL string, format string, args ...any) error {
	return fetchErr(domain.ErrPermanent, name, rawURL, 0, fmt.Errorf(format, args...))
}

// transportErr classifies a failure from Doer.Do. Context cancellation and
// deadline expiry stay retryable but remain detectable as context errors.
func transportErr(ctx context.Context, name, rawURL string, cause error) error {
	fe := &domain.FetchError{Kind: domain.ErrTransient, Adapter: name, URL: rawURL, Err: cause}

	// Prefer the context's own error: an http.Client wraps it in *url.Error, and
	// on some paths reports a generic "connection reset" instead.
	switch {
	case errors.Is(cause, context.Canceled) || errors.Is(cause, context.DeadlineExceeded):
		return &ctxAwareError{fe: fe, aux: cause}
	case ctx.Err() != nil:
		return &ctxAwareError{fe: fe, aux: ctx.Err()}
	}
	return fe
}

// statusErr maps an HTTP status onto the domain taxonomy.
//
//	404, 410      -> ErrNotFound   (posting is gone, never retry)
//	429           -> ErrRateLimited (back off, retry later)
//	5xx           -> ErrTransient  (retry)
//	other non-2xx -> ErrPermanent  (auth walls, bad request, redirect loops)
func statusErr(name, rawURL string, status int) error {
	switch {
	case status == http.StatusNotFound, status == http.StatusGone:
		return fetchErr(domain.ErrNotFound, name, rawURL, status, errors.New("posting not found"))
	case status == http.StatusTooManyRequests:
		return fetchErr(domain.ErrRateLimited, name, rawURL, status, errors.New("upstream rate limited"))
	case status >= 500:
		return fetchErr(domain.ErrTransient, name, rawURL, status, errors.New("upstream server error"))
	default:
		return fetchErr(domain.ErrPermanent, name, rawURL, status, fmt.Errorf("unexpected status %d", status))
	}
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

// getBody issues a GET and returns the body of a 2xx response. Non-2xx statuses
// are mapped by statusErr. The body is drained and closed either way so the
// underlying connection can be reused.
func getBody(ctx context.Context, doer Doer, name, endpoint, accept string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, permanentf(name, endpoint, "build request: %w", err)
	}
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	return do(ctx, doer, name, endpoint, req)
}

// postJSONBody issues a POST with a JSON body. Ashby's job board GraphQL
// endpoint is the only caller.
func postJSONBody(ctx context.Context, doer Doer, name, endpoint string, payload any) ([]byte, error) {
	enc, err := json.Marshal(payload)
	if err != nil {
		return nil, permanentf(name, endpoint, "encode request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(enc))
	if err != nil {
		return nil, permanentf(name, endpoint, "build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	return do(ctx, doer, name, endpoint, req)
}

func do(ctx context.Context, doer Doer, name, endpoint string, req *http.Request) ([]byte, error) {
	resp, err := doer.Do(req)
	if err != nil {
		return nil, transportErr(ctx, name, endpoint, err)
	}
	defer func() {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBodyBytes))
		_ = resp.Body.Close()
	}()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, statusErr(name, endpoint, resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))
	if err != nil {
		// A read that dies mid-body is a transport failure, not a bad payload.
		return nil, transportErr(ctx, name, endpoint, fmt.Errorf("read body: %w", err))
	}
	return body, nil
}

// decodeJSON parses a response body, mapping any syntax or type error onto
// ErrPermanent: a malformed payload will be malformed again on retry.
func decodeJSON(name, endpoint string, body []byte, out any) error {
	if err := json.Unmarshal(body, out); err != nil {
		return permanentf(name, endpoint, "decode json: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Result assembly
// ---------------------------------------------------------------------------

// finish validates and normalises an adapter's result. An adapter that reports
// success with no text is worse than an honest failure: it poisons the coverage
// metric and produces a match with nothing to match on.
func finish(jd domain.JobDescription, name, rawURL string) (domain.JobDescription, error) {
	jd.Adapter = name
	jd.RawText = collapse(jd.RawText)
	jd.Title = collapseInline(jd.Title)
	jd.Location = collapseInline(jd.Location)
	jd.Company = collapseInline(jd.Company)

	if jd.RawText == "" {
		return domain.JobDescription{}, permanentf(name, rawURL, "no job description text in response")
	}
	return jd, nil
}
