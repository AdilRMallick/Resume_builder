package adapters

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// blockingServer never responds. The handler parks until the client goes away
// (or the test ends), which is the only honest way to test that cancellation
// propagates all the way into an in-flight request.
func blockingServer(t *testing.T) (base string, doer Doer, started <-chan struct{}) {
	t.Helper()
	ch := make(chan struct{})
	var once sync.Once
	done := make(chan struct{})

	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(ch) })
		select {
		case <-r.Context().Done(): // client disconnected
		case <-done: // test finished
		}
	})
	// Registered after serve's own cleanup so it runs first (cleanups are LIFO).
	// srv.Close waits for in-flight handlers, which would deadlock against a
	// handler still parked on done.
	t.Cleanup(func() { close(done) })

	return srv.URL, doer, ch
}

// TestCancelledContextAbortsInFlightFetch is the whole reason adapters take a
// context: a worker shutting down, or a job whose visibility timeout has
// expired, must not leave a request hanging on a socket.
func TestCancelledContextAbortsInFlightFetch(t *testing.T) {
	for _, a := range adaptersUnderTest() {
		t.Run(a.name, func(t *testing.T) {
			base, doer, started := blockingServer(t)
			target := a.url
			if target == "" {
				target = base + "/careers/1"
			}

			ctx, cancel := context.WithCancel(context.Background())
			adapter := a.make(base)

			type result struct {
				err error
			}
			res := make(chan result, 1)
			go func() {
				_, err := adapter.Fetch(ctx, doer, target)
				res <- result{err}
			}()

			// Only cancel once the server confirms the request is in flight;
			// otherwise this would pass even if the adapter ignored ctx and
			// simply failed to connect.
			select {
			case <-started:
			case <-time.After(5 * time.Second):
				cancel()
				t.Fatal("server never received the request")
			}
			cancel()

			select {
			case r := <-res:
				if r.err == nil {
					t.Fatal("Fetch returned nil error after cancellation")
				}
				if !errors.Is(r.err, context.Canceled) {
					t.Errorf("want errors.Is(err, context.Canceled); got %v", r.err)
				}
				// Cancellation is a transient outcome: the posting itself is
				// fine, the attempt was abandoned, so the job may be retried.
				if !errors.Is(r.err, domain.ErrTransient) {
					t.Errorf("want errors.Is(err, domain.ErrTransient); got %v", r.err)
				}
				if !domain.Retryable(r.err) {
					t.Error("a cancelled fetch must stay retryable")
				}
				var fe *domain.FetchError
				if !errors.As(r.err, &fe) {
					t.Errorf("error is not a *domain.FetchError: %v", r.err)
				} else if fe.Adapter != a.name {
					t.Errorf("FetchError.Adapter = %q, want %q", fe.Adapter, a.name)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("Fetch did not return within 5s of cancellation")
			}
		})
	}
}

// TestContextDeadlineAbortsInFlightFetch covers the timeout half of the same
// contract; the fetcher sets a per-job deadline from config.FetchTimeout.
func TestContextDeadlineAbortsInFlightFetch(t *testing.T) {
	base, doer, started := blockingServer(t)

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	res := make(chan error, 1)
	go func() {
		_, err := NewGreenhouse(WithBaseURL(base)).Fetch(ctx, doer, "https://boards.greenhouse.io/acmerobotics/jobs/4012345")
		res <- err
	}()

	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("server never received the request")
	}

	select {
	case err := <-res:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("want errors.Is(err, context.DeadlineExceeded); got %v", err)
		}
		if !errors.Is(err, domain.ErrTransient) {
			t.Errorf("want errors.Is(err, domain.ErrTransient); got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Fetch did not return after the deadline expired")
	}
}
