package adapters

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// Shared test scaffolding. Every test in this package runs against an
// httptest.Server serving fixtures from testdata/; nothing here touches the
// network, which is the point: adapter tests that need Greenhouse to be up are
// not tests.

// fixture reads a recorded response body from testdata/.
func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	return b
}

// serve starts a test server and returns it with a Doer pointed at it. The
// Doer is a plain *http.Client; in production this slot is filled by the
// rate-limited client from internal/httpx, which satisfies the same interface.
func serve(t *testing.T, h http.HandlerFunc) (*httptest.Server, Doer) {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return srv, &http.Client{Timeout: 5 * time.Second}
}

// serveJSON replies with the named fixture for any request, recording the
// request URI it saw so tests can assert the endpoint an adapter built.
func serveJSON(t *testing.T, name string, gotURI *string) (*httptest.Server, Doer) {
	t.Helper()
	body := fixture(t, name)
	return serve(t, func(w http.ResponseWriter, r *http.Request) {
		if gotURI != nil {
			*gotURI = r.RequestURI
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
}

// serveStatus replies with a bare status code for any request.
func serveStatus(t *testing.T, code int) (*httptest.Server, Doer) {
	t.Helper()
	return serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(code)
		_, _ = w.Write([]byte("upstream says no"))
	})
}

// serveBody replies with a fixed body and content type.
func serveBody(t *testing.T, contentType, body string) (*httptest.Server, Doer) {
	t.Helper()
	return serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentType)
		_, _ = w.Write([]byte(body))
	})
}

func ctxT(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// assertContains fails with a readable diff-ish message rather than dumping the
// whole job description on every miss.
func assertContains(t *testing.T, got string, want []string) {
	t.Helper()
	for _, w := range want {
		if !strings.Contains(got, w) {
			t.Errorf("raw text missing %q\n--- got ---\n%s\n-----------", w, got)
		}
	}
}

func assertNotContains(t *testing.T, got string, unwanted []string) {
	t.Helper()
	for _, w := range unwanted {
		if strings.Contains(got, w) {
			t.Errorf("raw text should not contain %q\n--- got ---\n%s\n-----------", w, got)
		}
	}
}

// assertFetchError checks the domain sentinel and that the error carries the
// structured *domain.FetchError the queue layer reads.
func assertFetchError(t *testing.T, err error, want error, wantAdapter string) {
	t.Helper()
	if err == nil {
		t.Fatalf("want error %v, got nil", want)
	}
	if !errors.Is(err, want) {
		t.Fatalf("want errors.Is(err, %v); got %v", want, err)
	}
	var fe *domain.FetchError
	if !errors.As(err, &fe) {
		t.Fatalf("error is not a *domain.FetchError: %v", err)
	}
	if fe.Adapter != wantAdapter {
		t.Errorf("FetchError.Adapter = %q, want %q", fe.Adapter, wantAdapter)
	}
}
