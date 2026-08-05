package httpx

import (
	"context"
	"errors"
	"math"
	"net/http"
	"testing"
	"time"
)

// TestBackoffFullJitterIsBounded is the acceptance case for jitter: whatever the
// random source does, the sleep is never negative and never exceeds the
// exponential ceiling.
func TestBackoffFullJitterIsBounded(t *testing.T) {
	p := RetryPolicy{
		MaxAttempts:   6,
		Base:          100 * time.Millisecond,
		Max:           4 * time.Second,
		MaxTotalWait:  time.Minute,
		MaxRetryAfter: time.Minute,
	}

	// Including sources that are broken on purpose.
	sources := map[string]func() float64{
		"zero":     func() float64 { return 0 },
		"half":     func() float64 { return 0.5 },
		"almost 1": func() float64 { return 0.999999 },
		"negative": func() float64 { return -3 },
		"over one": func() float64 { return 7.5 },
		"NaN":      func() float64 { return math.NaN() },
		"Inf":      func() float64 { return math.Inf(1) },
		"nil":      nil,
	}

	for name, src := range sources {
		t.Run(name, func(t *testing.T) {
			for attempt := -2; attempt < 40; attempt++ {
				d := p.Backoff(attempt, src)
				if d < 0 {
					t.Fatalf("attempt %d: negative sleep %v", attempt, d)
				}
				if d > p.Max {
					t.Fatalf("attempt %d: sleep %v exceeds Max %v", attempt, d, p.Max)
				}
				a := attempt
				if a < 0 {
					a = 0
				}
				ceiling := time.Duration(float64(p.Base) * math.Pow(2, float64(a)))
				if ceiling > p.Max || ceiling <= 0 {
					ceiling = p.Max
				}
				if d > ceiling {
					t.Fatalf("attempt %d: sleep %v exceeds exponential ceiling %v", attempt, d, ceiling)
				}
			}
		})
	}
}

func TestBackoffGrowsExponentially(t *testing.T) {
	p := DefaultRetryPolicy()
	full := func() float64 { return 0.999999999 }
	prev := time.Duration(0)
	for attempt := 0; attempt < 5; attempt++ {
		d := p.Backoff(attempt, full)
		if d <= prev && d < p.Max {
			t.Fatalf("attempt %d: backoff %v did not grow past %v", attempt, d, prev)
		}
		prev = d
	}
	if got := p.Backoff(0, func() float64 { return 0 }); got != 0 {
		t.Errorf("full jitter with rnd()=0 should be able to retry immediately, got %v", got)
	}
}

func TestParseRetryAfter(t *testing.T) {
	now := time.Date(2026, 8, 3, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		in     string
		want   time.Duration
		wantOK bool
	}{
		{"", 0, false},
		{"1", time.Second, true},
		{"0", 0, true},
		{"120", 2 * time.Minute, true},
		{"-5", 0, true}, // nonsense delta clamps to zero, not a negative sleep
		{"1.5", 1500 * time.Millisecond, true},
		{"soon", 0, false},
		{"Mon, 03 Aug 2026 12:00:30 GMT", 30 * time.Second, true},
		{"Mon, 03 Aug 2026 11:59:00 GMT", 0, true}, // already past
	}
	for _, tc := range cases {
		got, ok := ParseRetryAfter(tc.in, now)
		if ok != tc.wantOK || got != tc.want {
			t.Errorf("ParseRetryAfter(%q) = (%v, %v), want (%v, %v)", tc.in, got, ok, tc.want, tc.wantOK)
		}
		if got < 0 {
			t.Errorf("ParseRetryAfter(%q) returned a negative duration", tc.in)
		}
	}
}

func TestDelayPrefersRetryAfterOverBackoff(t *testing.T) {
	p := RetryPolicy{
		MaxAttempts:   4,
		Base:          time.Millisecond,
		Max:           2 * time.Millisecond,
		MaxTotalWait:  time.Minute,
		MaxRetryAfter: 30 * time.Second,
	}
	now := time.Now()

	resp := &http.Response{Header: http.Header{}}
	resp.Header.Set("Retry-After", "7")
	d, ok := p.delayAt(now, 0, resp, func() float64 { return 0 })
	if !ok || d != 7*time.Second {
		t.Fatalf("delay = (%v, %v), want (7s, true)", d, ok)
	}

	// Over the cap: refuse to retry in-process rather than squat on a worker.
	resp.Header.Set("Retry-After", "31")
	if d, ok := p.delayAt(now, 0, resp, nil); ok {
		t.Fatalf("delay = (%v, true), want ok=false for an over-cap Retry-After", d)
	}

	// No header: fall back to jittered exponential backoff.
	resp.Header.Del("Retry-After")
	d, ok = p.delayAt(now, 0, resp, func() float64 { return 0.5 })
	if !ok || d > p.Max {
		t.Fatalf("delay = (%v, %v), want a bounded backoff", d, ok)
	}
}

func TestNormalizedPolicyRejectsNonsense(t *testing.T) {
	p := RetryPolicy{MaxAttempts: 0, Base: -1, Max: -1}.normalized()
	if p.MaxAttempts < 1 || p.Base <= 0 || p.Max < p.Base || p.MaxTotalWait <= 0 || p.MaxRetryAfter <= 0 {
		t.Fatalf("normalized policy is still nonsense: %+v", p)
	}
}

func TestSleepCtx(t *testing.T) {
	if err := sleepCtx(context.Background(), 0); err != nil {
		t.Fatalf("zero sleep returned %v", err)
	}

	start := time.Now()
	if err := sleepCtx(context.Background(), 30*time.Millisecond); err != nil {
		t.Fatalf("sleep returned %v", err)
	}
	if time.Since(start) < 25*time.Millisecond {
		t.Fatal("sleepCtx returned early")
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	start = time.Now()
	err := sleepCtx(ctx, 10*time.Second)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("sleepCtx ignored cancellation")
	}
}
