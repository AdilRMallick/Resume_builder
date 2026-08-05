package httpx

import (
	"context"
	"math"
	"net/http"
	"strconv"
	"time"
)

// RetryPolicy bounds how hard the client tries. Every field is a ceiling: the
// point of the policy is that a misbehaving host cannot pin a worker forever.
type RetryPolicy struct {
	// MaxAttempts counts the first try. 1 disables retrying.
	MaxAttempts int
	// Base is the first backoff interval, doubled each attempt before jitter.
	Base time.Duration
	// Max caps a single backoff interval.
	Max time.Duration
	// MaxTotalWait caps the sum of all backoffs for one Do call.
	MaxTotalWait time.Duration
	// MaxRetryAfter caps how long a Retry-After header may park us. A header
	// asking for longer is treated as "do not retry in this process"; the queue
	// harness will reschedule the job instead.
	MaxRetryAfter time.Duration
}

// DefaultRetryPolicy is deliberately patient rather than aggressive: this client
// reads public job postings, and getting the IP blocked is the failure mode that
// actually matters.
func DefaultRetryPolicy() RetryPolicy {
	return RetryPolicy{
		MaxAttempts:   4,
		Base:          500 * time.Millisecond,
		Max:           30 * time.Second,
		MaxTotalWait:  2 * time.Minute,
		MaxRetryAfter: 60 * time.Second,
	}
}

func (p RetryPolicy) normalized() RetryPolicy {
	if p.MaxAttempts <= 0 {
		p.MaxAttempts = 1
	}
	if p.Base <= 0 {
		p.Base = 500 * time.Millisecond
	}
	if p.Max <= 0 {
		p.Max = 30 * time.Second
	}
	if p.Max < p.Base {
		p.Max = p.Base
	}
	if p.MaxTotalWait <= 0 {
		p.MaxTotalWait = 2 * time.Minute
	}
	if p.MaxRetryAfter <= 0 {
		p.MaxRetryAfter = 60 * time.Second
	}
	return p
}

// Backoff returns the sleep before attempt+1, using full jitter:
//
//	sleep = uniform[0, min(Max, Base * 2^attempt))
//
// Full jitter rather than equal jitter because the whole point here is to break
// up the synchronised retry storm that N workers hitting one host produce. rnd
// must return a value in [0, 1); out-of-range values are clamped, so a hostile or
// buggy source can never produce a negative or unbounded sleep.
func (p RetryPolicy) Backoff(attempt int, rnd func() float64) time.Duration {
	p = p.normalized()
	if attempt < 0 {
		attempt = 0
	}
	if attempt > 30 { // 2^31 nanoseconds is already past any sane Max
		attempt = 30
	}
	ceiling := float64(p.Base) * math.Pow(2, float64(attempt))
	if ceiling > float64(p.Max) || math.IsInf(ceiling, 0) {
		ceiling = float64(p.Max)
	}
	f := 0.5
	if rnd != nil {
		f = rnd()
	}
	if math.IsNaN(f) || f < 0 {
		f = 0
	}
	if f >= 1 {
		f = math.Nextafter(1, 0)
	}
	d := time.Duration(f * ceiling)
	if d < 0 {
		d = 0
	}
	if d > p.Max {
		d = p.Max
	}
	return d
}

// Delay decides how long to wait before retrying a failed attempt, and whether
// retrying is worth it at all. A server that told us how long to wait is always
// believed over our own arithmetic, up to MaxRetryAfter.
func (p RetryPolicy) Delay(attempt int, resp *http.Response, rnd func() float64) (time.Duration, bool) {
	return p.delayAt(time.Now(), attempt, resp, rnd)
}

func (p RetryPolicy) delayAt(now time.Time, attempt int, resp *http.Response, rnd func() float64) (time.Duration, bool) {
	np := p.normalized()
	if resp != nil {
		if d, ok := ParseRetryAfter(resp.Header.Get("Retry-After"), now); ok {
			if d > np.MaxRetryAfter {
				return 0, false // hand it back to the queue instead of squatting
			}
			if d < 0 {
				d = 0
			}
			return d, true
		}
	}
	return np.Backoff(attempt, rnd), true
}

// ParseRetryAfter reads both forms of the header: delta-seconds and an HTTP-date.
// A date in the past yields zero, not a negative duration.
func ParseRetryAfter(v string, now time.Time) (time.Duration, bool) {
	if v == "" {
		return 0, false
	}
	if secs, err := strconv.ParseFloat(v, 64); err == nil {
		if secs < 0 || math.IsNaN(secs) || math.IsInf(secs, 0) {
			return 0, true
		}
		return time.Duration(secs * float64(time.Second)), true
	}
	if t, err := http.ParseTime(v); err == nil {
		d := t.Sub(now)
		if d < 0 {
			d = 0
		}
		return d, true
	}
	return 0, false
}

// sleepCtx sleeps for d unless ctx finishes first, in which case it returns
// ctx.Err(). Every wait in this package goes through here; there are no bare
// time.Sleep calls, because a cancelled fetch job must stop immediately.
func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
