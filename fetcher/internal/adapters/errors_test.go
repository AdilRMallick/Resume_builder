package adapters

import (
	"errors"
	"net/http"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// adapterUnderTest pairs a constructor with a URL that adapter accepts, so the
// error tables below can run every adapter through the same matrix. A new
// adapter is added to the fetcher by adding one line here and one to
// NewRegistry.
type adapterUnderTest struct {
	name string
	make func(base string) Adapter
	url  string
	// malformed is a body that parses as neither the adapter's expected shape
	// nor anything usable.
	malformed string
}

func adaptersUnderTest() []adapterUnderTest {
	return []adapterUnderTest{
		{
			name:      "greenhouse",
			make:      func(base string) Adapter { return NewGreenhouse(WithBaseURL(base)) },
			url:       "https://boards.greenhouse.io/acmerobotics/jobs/4012345",
			malformed: `{"title": "Software Engineer", "content": `,
		},
		{
			name:      "lever",
			make:      func(base string) Adapter { return NewLever(WithBaseURL(base)) },
			url:       "https://jobs.lever.co/northwind/" + leverUUID,
			malformed: `<!doctype html><html><body>not json</body></html>`,
		},
		{
			name:      "ashby",
			make:      func(base string) Adapter { return NewAshby(WithBaseURL(base), WithAltBaseURL(base)) },
			url:       "https://jobs.ashbyhq.com/halcyon/" + ashbyListedID,
			malformed: `{"apiVersion":"1","jobs":{"not":"an array"}}`,
		},
		{
			name:      "smartrecruiters",
			make:      func(base string) Adapter { return NewSmartRecruiters(WithBaseURL(base)) },
			url:       "https://jobs.smartrecruiters.com/NorthwindLabs/744000123456789-software-engineer",
			malformed: `{"name":"Software Engineer","jobAd":{"sections":`,
		},
		{
			name:      "fallback",
			make:      func(string) Adapter { return NewFallback() },
			url:       "", // filled in per test with the server URL
			malformed: `<html><body><div>too short to be a job description</div></body></html>`,
		},
	}
}

// TestStatusMapping is the contract the queue harness depends on: retry policy
// is decided from these sentinels, never from an error string.
func TestStatusMapping(t *testing.T) {
	statuses := []struct {
		name   string
		status int
		want   error
		// retryable mirrors domain.Retryable so a change to either side of the
		// mapping shows up here.
		retryable bool
		wantStat  domain.FetchStatus
	}{
		{"404 not found", http.StatusNotFound, domain.ErrNotFound, false, domain.StatusNotFound},
		{"410 gone", http.StatusGone, domain.ErrNotFound, false, domain.StatusNotFound},
		{"429 rate limited", http.StatusTooManyRequests, domain.ErrRateLimited, true, domain.StatusRateLimited},
		{"500 server error", http.StatusInternalServerError, domain.ErrTransient, true, domain.StatusTransientError},
		{"502 bad gateway", http.StatusBadGateway, domain.ErrTransient, true, domain.StatusTransientError},
		{"503 unavailable", http.StatusServiceUnavailable, domain.ErrTransient, true, domain.StatusTransientError},
		{"400 bad request", http.StatusBadRequest, domain.ErrPermanent, false, domain.StatusPermanentError},
		{"401 unauthorized", http.StatusUnauthorized, domain.ErrPermanent, false, domain.StatusPermanentError},
		{"403 forbidden", http.StatusForbidden, domain.ErrPermanent, false, domain.StatusPermanentError},
	}

	for _, a := range adaptersUnderTest() {
		for _, s := range statuses {
			t.Run(a.name+"/"+s.name, func(t *testing.T) {
				srv, doer := serveStatus(t, s.status)
				target := a.url
				if target == "" {
					target = srv.URL + "/careers/1"
				}

				_, err := a.make(srv.URL).Fetch(ctxT(t), doer, target)
				assertFetchError(t, err, s.want, a.name)

				if got := domain.StatusFor(err); got != s.wantStat {
					t.Errorf("domain.StatusFor = %q, want %q", got, s.wantStat)
				}
				if got := domain.Retryable(err); got != s.retryable {
					t.Errorf("domain.Retryable = %v, want %v", got, s.retryable)
				}

				var fe *domain.FetchError
				if errors.As(err, &fe) && fe.StatusCode != s.status && fe.StatusCode != 0 {
					t.Errorf("FetchError.StatusCode = %d, want %d", fe.StatusCode, s.status)
				}
			})
		}
	}
}

// TestMalformedBodyIsPermanent: a response the adapter cannot parse will not
// parse on retry either, so it must never be requeued.
func TestMalformedBodyIsPermanent(t *testing.T) {
	for _, a := range adaptersUnderTest() {
		t.Run(a.name, func(t *testing.T) {
			srv, doer := serveBody(t, "application/json", a.malformed)
			target := a.url
			if target == "" {
				target = srv.URL + "/careers/1"
			}

			_, err := a.make(srv.URL).Fetch(ctxT(t), doer, target)
			assertFetchError(t, err, domain.ErrPermanent, a.name)

			if domain.Retryable(err) {
				t.Error("malformed body must not be retryable")
			}
			if got := domain.StatusFor(err); got != domain.StatusPermanentError {
				t.Errorf("domain.StatusFor = %q, want %q", got, domain.StatusPermanentError)
			}
		})
	}
}

// TestEmptyResponseIsPermanent: a 200 with nothing usable in it is a failure,
// not a job description. Storing the empty string would count as coverage.
func TestEmptyResponseIsPermanent(t *testing.T) {
	for _, a := range adaptersUnderTest() {
		if a.name == "ashby" {
			// Ashby's empty board is a legitimate not-found, covered by
			// TestAshbyMissingEverywhereIsNotFound.
			continue
		}
		t.Run(a.name, func(t *testing.T) {
			srv, doer := serveBody(t, "application/json", "{}")
			target := a.url
			if target == "" {
				target = srv.URL + "/careers/1"
			}
			_, err := a.make(srv.URL).Fetch(ctxT(t), doer, target)
			assertFetchError(t, err, domain.ErrPermanent, a.name)
		})
	}
}

// TestTransportFailureIsTransient: a connection that never completes is a
// network problem, and network problems are worth another attempt.
func TestTransportFailureIsTransient(t *testing.T) {
	for _, a := range adaptersUnderTest() {
		t.Run(a.name, func(t *testing.T) {
			// Start a server, take its URL, then close it: the port is now
			// refusing connections.
			srv, doer := serveStatus(t, http.StatusOK)
			base := srv.URL
			srv.Close()

			target := a.url
			if target == "" {
				target = base + "/careers/1"
			}
			_, err := a.make(base).Fetch(ctxT(t), doer, target)
			assertFetchError(t, err, domain.ErrTransient, a.name)
			if !domain.Retryable(err) {
				t.Error("a refused connection must be retryable")
			}
		})
	}
}
