// Package domain holds the types shared across the fetcher service and, where noted,
// across the process boundary with the Python services. Anything in this file that is
// serialized to Redis or Postgres is a contract: change it and you must change the
// Python side in the same commit.
package domain

import (
	"encoding/json"
	"errors"
	"fmt"
)

// ---------------------------------------------------------------------------
// Queue payloads. Contract with jme/queue.py.
// ---------------------------------------------------------------------------

// FetchJob is the payload XADDed to the jme:fetch stream by the Python ingestor.
type FetchJob struct {
	PostingID    int64  `json:"posting_id"`
	CanonicalKey string `json:"canonical_key"`
	URL          string `json:"url"`
	Company      string `json:"company"`
	Title        string `json:"title"`
	// Attempt is incremented by the queue harness on redelivery, not by producers.
	Attempt int `json:"attempt,omitempty"`
}

func (j FetchJob) Validate() error {
	if j.PostingID <= 0 {
		return errors.New("fetch job: posting_id must be positive")
	}
	if j.URL == "" {
		return errors.New("fetch job: url is required")
	}
	return nil
}

// EnrichJob is the payload XADDed to the jme:enrich stream by the Go fetcher and
// consumed by the Python enricher.
type EnrichJob struct {
	PostingID int64  `json:"posting_id"`
	JDSHA256  string `json:"jd_sha256"`
	Adapter   string `json:"adapter"`
	CharCount int    `json:"char_count"`
}

// Marshal encodes a payload into the single "payload" field used in every stream entry.
func Marshal(v any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("marshal payload: %w", err)
	}
	return map[string]any{"payload": string(b)}, nil
}

// Unmarshal decodes a stream entry's "payload" field.
func Unmarshal(values map[string]any, out any) error {
	raw, ok := values["payload"]
	if !ok {
		return errors.New("stream entry has no payload field")
	}
	s, ok := raw.(string)
	if !ok {
		return fmt.Errorf("payload field is %T, want string", raw)
	}
	return json.Unmarshal([]byte(s), out)
}

// ---------------------------------------------------------------------------
// Job description
// ---------------------------------------------------------------------------

// JobDescription is what an ATS adapter returns.
type JobDescription struct {
	RawText  string `json:"raw_text"`
	Title    string `json:"title"`
	Location string `json:"location"`
	Company  string `json:"company"`
	Adapter  string `json:"adapter"`
	// SourceURL is the URL actually fetched, which may differ from the input after
	// redirects or after an adapter rewrites to a JSON API endpoint.
	SourceURL string `json:"source_url"`
}

// ---------------------------------------------------------------------------
// Fetch status. Mirrors the fetch_status enum in Postgres (jme/models.py).
// ---------------------------------------------------------------------------

type FetchStatus string

const (
	StatusPending        FetchStatus = "pending"
	StatusOK             FetchStatus = "ok"
	StatusNotFound       FetchStatus = "not_found"
	StatusRateLimited    FetchStatus = "rate_limited"
	StatusTransientError FetchStatus = "transient_error"
	StatusPermanentError FetchStatus = "permanent_error"
	StatusRobotsDenied   FetchStatus = "robots_denied"
	StatusUnsupported    FetchStatus = "unsupported"
)

// ---------------------------------------------------------------------------
// Typed errors. Adapters and the HTTP client return these so callers can decide
// retry policy without string matching.
// ---------------------------------------------------------------------------

var (
	// ErrNotFound: the posting is gone. Do not retry.
	ErrNotFound = errors.New("not found")
	// ErrRateLimited: back off and retry later.
	ErrRateLimited = errors.New("rate limited")
	// ErrTransient: network blip, 5xx, timeout. Retry.
	ErrTransient = errors.New("transient")
	// ErrPermanent: malformed response, unsupported shape, 4xx that is not 404/429.
	ErrPermanent = errors.New("permanent")
	// ErrRobotsDenied: robots.txt disallows this path.
	ErrRobotsDenied = errors.New("robots denied")
	// ErrCircuitOpen: the host circuit breaker is open; no request was made.
	ErrCircuitOpen = errors.New("circuit open")
)

// FetchError wraps a sentinel with context. Use errors.Is against the sentinels.
type FetchError struct {
	Kind       error
	StatusCode int
	Adapter    string
	URL        string
	Err        error
}

func (e *FetchError) Error() string {
	return fmt.Sprintf("%s: adapter=%s url=%s status=%d: %v", e.Kind, e.Adapter, e.URL, e.StatusCode, e.Err)
}

func (e *FetchError) Unwrap() error { return e.Kind }

// StatusFor maps an error onto the fetch_status column value.
func StatusFor(err error) FetchStatus {
	switch {
	case err == nil:
		return StatusOK
	case errors.Is(err, ErrNotFound):
		return StatusNotFound
	case errors.Is(err, ErrRateLimited):
		return StatusRateLimited
	case errors.Is(err, ErrRobotsDenied):
		return StatusRobotsDenied
	case errors.Is(err, ErrPermanent):
		return StatusPermanentError
	default:
		return StatusTransientError
	}
}

// Retryable reports whether a failed fetch is worth another attempt.
func Retryable(err error) bool {
	switch {
	case err == nil:
		return false
	case errors.Is(err, ErrNotFound), errors.Is(err, ErrPermanent), errors.Is(err, ErrRobotsDenied):
		return false
	default:
		return true
	}
}
