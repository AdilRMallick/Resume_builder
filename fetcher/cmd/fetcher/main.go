// Command fetcher is the Go half of the Job Match Engine.
//
// It consumes the jme:fetch Redis stream, resolves each posting URL to job
// description text through an ATS adapter, writes the result to Postgres, and
// publishes a jme:enrich message for the Python enricher.
//
// Everything interesting about it is a failure-handling decision:
//
//	a worker dies mid-job        XAUTOCLAIM hands the message to a live worker
//	a host rate limits us        shared Redis token bucket + backoff with jitter
//	a host keeps failing         circuit breaker opens; no network call is made
//	a posting is gone            permanent failure, dead lettered, not retried
//	a posting has no JD          recorded as a failed fetch, posting is NOT dropped
//	the same message arrives 2x  every write is an upsert keyed on posting_id
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/adilmallick/job-match-engine/fetcher/internal/adapters"
	"github.com/adilmallick/job-match-engine/fetcher/internal/config"
	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
	"github.com/adilmallick/job-match-engine/fetcher/internal/httpx"
	"github.com/adilmallick/job-match-engine/fetcher/internal/queue"
	"github.com/adilmallick/job-match-engine/fetcher/internal/store"
)

func main() {
	var (
		workers  = flag.Int("workers", 0, "number of concurrent workers (0 = config default)")
		runID    = flag.String("run-id", "", "run id for run_metric rows (default: generated)")
		once     = flag.Bool("once", false, "drain the stream and exit instead of blocking forever")
		coverage = flag.Bool("coverage", false, "print adapter coverage and exit")
		verbose  = flag.Bool("v", false, "debug logging")
	)
	flag.Parse()

	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	slog.SetDefault(log)

	if err := run(log, *workers, *runID, *once, *coverage); err != nil {
		log.Error("fatal", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger, workers int, runID string, once, coverageOnly bool) error {
	cfg := config.Load()
	if workers <= 0 {
		workers = cfg.Workers
	}
	if runID == "" {
		runID = fmt.Sprintf("fetch-%s", time.Now().UTC().Format("20060102T150405"))
	}

	// Signals first: a Ctrl-C during startup should still exit cleanly.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	st, err := store.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Close()

	if coverageOnly {
		return printCoverage(ctx, st)
	}

	redisOpts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return fmt.Errorf("parse redis url: %w", err)
	}
	rdb := redis.NewClient(redisOpts)
	defer rdb.Close()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("ping redis: %w", err)
	}

	client, err := httpx.New(cfg, rdb, httpx.WithMetrics(&storeMetrics{
		store: st, runID: runID, log: log,
	}))
	if err != nil {
		return fmt.Errorf("build http client: %w", err)
	}

	registry := adapters.NewRegistry()
	producer := queue.NewProducer(rdb, config.StreamEnrich)

	log.Info("fetcher starting",
		"workers", workers,
		"run_id", runID,
		"stream", config.StreamFetch,
		"adapters", registry.Names(),
		"rate_per_sec", cfg.HostRatePerSec,
	)

	w := &worker{
		store:    st,
		registry: registry,
		doer:     &ctxDoer{client: client},
		producer: producer,
		timeout:  cfg.FetchTimeout,
		runID:    runID,
		log:      log,
	}

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		consumer := queue.New(rdb, queue.Config{
			Stream:            config.StreamFetch,
			Group:             config.GroupFetch,
			Consumer:          consumerName(i),
			DeadLetterStream:  config.StreamFetchDead,
			BlockTimeout:      cfg.BlockTimeout,
			VisibilityTimeout: cfg.VisibilityTimeout,
			MaxAttempts:       cfg.MaxAttempts,
			ClaimInterval:     cfg.ClaimInterval,
		}, log)

		if once {
			// Drain mode: process whatever is queued, then stop. Useful in cron
			// pipelines and in CI, where blocking forever is the wrong shape.
			if err := drain(ctx, consumer, w.handle, log); err != nil {
				return err
			}
			break
		}

		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := consumer.Run(ctx, w.handle); err != nil {
				log.Error("consumer exited with error", "error", err)
			}
		}()
	}
	wg.Wait()

	log.Info("fetcher stopped", "run_id", runID)
	return printCoverage(context.WithoutCancel(ctx), st)
}

func consumerName(i int) string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "worker"
	}
	// Distinct per process AND per goroutine: XAUTOCLAIM can only rescue messages
	// from a consumer name that is no longer reading, so names must not collide.
	return fmt.Sprintf("%s-%d-%d", host, os.Getpid(), i)
}

// drain processes the currently queued work and returns. It stops on the first
// read that yields nothing, which is exactly right for a cron-style invocation.
func drain(ctx context.Context, c *queue.Consumer, handler queue.Handler, log *slog.Logger) error {
	if err := c.EnsureGroup(ctx); err != nil {
		return err
	}
	depth, err := c.Depth(ctx)
	if err != nil {
		return err
	}
	log.Info("draining", "stream_depth", depth)

	drained, cancel := context.WithCancel(ctx)
	defer cancel()

	idle := 0
	go func() {
		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-drained.Done():
				return
			case <-ticker.C:
				pending, err := c.PendingSummary(drained)
				if err == nil && pending.Count == 0 {
					idle++
					if idle >= 5 {
						cancel()
						return
					}
				} else {
					idle = 0
				}
			}
		}
	}()
	return c.Run(drained, handler)
}

// ---------------------------------------------------------------------------
// worker
// ---------------------------------------------------------------------------

type worker struct {
	store    *store.Store
	registry *adapters.Registry
	doer     adapters.Doer
	producer *queue.Producer
	timeout  time.Duration
	runID    string
	log      *slog.Logger
}

// handle resolves one posting. It must be idempotent: the queue is at-least-once,
// so this runs a second time for some fraction of messages by design.
func (w *worker) handle(ctx context.Context, msg queue.Message) error {
	var job domain.FetchJob
	if err := msg.Decode(&job); err != nil {
		// A payload we cannot parse will never parse. Retrying is pointless.
		return queue.Permanent("undecodable payload: %v", err)
	}
	if err := job.Validate(); err != nil {
		return queue.Permanent("invalid payload: %v", err)
	}

	exists, err := w.store.PostingExists(ctx, job.PostingID)
	if err != nil {
		return err
	}
	if !exists {
		return queue.Permanent("posting %d no longer exists", job.PostingID)
	}

	// Permanent cache by posting id: job descriptions do not change, so a
	// successful resolution is never refetched. This is what makes redelivery
	// cheap rather than a duplicated HTTP round trip.
	resolved, err := w.store.AlreadyResolved(ctx, job.PostingID)
	if err != nil {
		return err
	}
	if resolved {
		w.log.Debug("already resolved, skipping", "posting_id", job.PostingID)
		return nil
	}

	if err := w.store.MarkPending(ctx, job.PostingID); err != nil {
		return err
	}

	fetchCtx, cancel := context.WithTimeout(ctx, w.timeout)
	defer cancel()

	started := time.Now()
	jd, fetchErr := w.registry.Fetch(fetchCtx, w.doer, job.URL)
	elapsed := time.Since(started)

	if fetchErr != nil {
		return w.recordFailure(ctx, job, fetchErr, elapsed)
	}
	if len(jd.RawText) == 0 {
		// The adapter succeeded but produced nothing usable. Degrade rather than
		// drop: the posting stays, flagged, matched on title and company alone.
		return w.recordFailure(ctx, job,
			&domain.FetchError{
				Kind: domain.ErrPermanent, Adapter: jd.Adapter, URL: job.URL,
				Err: errors.New("adapter returned empty job description"),
			}, elapsed)
	}

	hash, err := w.store.RecordSuccess(ctx, job.PostingID, jd)
	if err != nil {
		return err // transient: retry, and the upsert makes that safe
	}

	// Only after the JD is committed do we hand the posting to the enricher.
	// Publishing first would let a crash between the two produce an enrich job
	// for text that was never stored.
	if _, err := w.producer.Publish(ctx, domain.EnrichJob{
		PostingID: job.PostingID,
		JDSHA256:  hash,
		Adapter:   jd.Adapter,
		CharCount: len(jd.RawText),
	}); err != nil {
		// The JD is safely stored. Failing here means the message is redelivered
		// and the enrich job is published on the retry - AlreadyResolved short
		// circuits the refetch, so the retry costs nothing.
		return fmt.Errorf("publish enrich job: %w", err)
	}

	_ = w.store.RecordMetric(ctx, w.runID, "fetch", "resolved", 1,
		map[string]string{"adapter": jd.Adapter})
	_ = w.store.RecordMetric(ctx, w.runID, "fetch", "latency_seconds", elapsed.Seconds(),
		map[string]string{"adapter": jd.Adapter})

	w.log.Info("resolved",
		"posting_id", job.PostingID,
		"adapter", jd.Adapter,
		"chars", len(jd.RawText),
		"ms", elapsed.Milliseconds(),
	)
	return nil
}

func (w *worker) recordFailure(
	ctx context.Context, job domain.FetchJob, fetchErr error, elapsed time.Duration,
) error {
	adapter := "unknown"
	var fe *domain.FetchError
	if errors.As(fetchErr, &fe) && fe.Adapter != "" {
		adapter = fe.Adapter
	}

	if err := w.store.RecordFailure(ctx, job.PostingID, adapter, fetchErr); err != nil {
		return err
	}
	_ = w.store.RecordMetric(ctx, w.runID, "fetch", "failed", 1, map[string]string{
		"adapter": adapter,
		"status":  string(domain.StatusFor(fetchErr)),
	})

	w.log.Warn("fetch failed",
		"posting_id", job.PostingID,
		"adapter", adapter,
		"status", domain.StatusFor(fetchErr),
		"retryable", domain.Retryable(fetchErr),
		"ms", elapsed.Milliseconds(),
		"error", fetchErr,
	)

	if !domain.Retryable(fetchErr) {
		// 404, robots denial, malformed response: the failure is recorded in
		// posting_jd, so dead lettering loses nothing and stops a pointless retry.
		return queue.Permanent("%v", fetchErr)
	}
	return fetchErr
}

// ---------------------------------------------------------------------------
// small bridges
// ---------------------------------------------------------------------------

// ctxDoer adapts httpx.Client (Do takes an explicit context, because rate-limit
// waiting and breaker checks happen before the request is sent) to the
// adapters.Doer shape (Do takes only a request). The context travels on the
// request, which every adapter builds with http.NewRequestWithContext.
type ctxDoer struct{ client *httpx.Client }

func (d *ctxDoer) Do(req *http.Request) (*http.Response, error) {
	return d.client.Do(req.Context(), req)
}

// storeMetrics forwards httpx's per-request observations into run_metric, so
// fetch latency and per-host outcomes land in the same table as every other
// stage's numbers.
type storeMetrics struct {
	store *store.Store
	runID string
	log   *slog.Logger
}

func (m *storeMetrics) ObserveFetch(host, status string, seconds float64) {
	// Deliberately fire-and-forget on a detached context: a metrics write must
	// never fail a fetch or be cancelled by the shutdown it is describing.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(context.Background()), 3*time.Second)
	defer cancel()
	if err := m.store.RecordMetric(ctx, m.runID, "fetch", "http_seconds", seconds,
		map[string]string{"host": host, "status": status}); err != nil {
		m.log.Debug("metric write failed", "error", err)
	}
}

func printCoverage(ctx context.Context, st *store.Store) error {
	rows, err := st.AdapterCoverage(ctx)
	if err != nil {
		return err
	}
	var total, resolved int64
	fmt.Println("\nadapter coverage (active postings)")
	fmt.Println("----------------------------------")
	for _, r := range rows {
		total += r.Total
		resolved += r.Resolved
		fmt.Printf("  %-14s %6d / %-6d  %5.1f%%\n",
			r.Adapter, r.Resolved, r.Total, pct(r.Resolved, r.Total))
	}
	fmt.Printf("  %-14s %6d / %-6d  %5.1f%%\n\n", "OVERALL", resolved, total, pct(resolved, total))
	return nil
}

func pct(n, d int64) float64 {
	if d == 0 {
		return 0
	}
	return float64(n) / float64(d) * 100
}
