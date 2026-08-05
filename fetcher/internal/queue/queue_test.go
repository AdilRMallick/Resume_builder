package queue

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// Database 9, not 0: test streams stay out of the database a dev's own fetcher is
// using, so a local run and a test run cannot see each other's messages.
const defaultTestRedisURL = "redis://localhost:6380/9"

// testRedisURL matches the Go store suite's JME_TEST_DATABASE_URL_GO convention, so a
// machine that cannot bind the docker-compose ports -- a CI runner or a cloud sandbox
// with Redis on 6379 -- can point the suite somewhere else instead of patching this
// file.
func testRedisURL() string {
	if url := os.Getenv("JME_TEST_REDIS_URL_GO"); url != "" {
		return url
	}
	return defaultTestRedisURL
}

func testClient(t *testing.T) *redis.Client {
	t.Helper()
	url := testRedisURL()
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatalf("parse redis url %q: %v", url, err)
	}
	client := redis.NewClient(opts)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		// Skipping is right locally and wrong in CI: a Redis service that failed to
		// start would turn this whole suite into skips and still report success.
		// JME_REQUIRE_SERVICES is CI asserting the services are supposed to be there.
		if os.Getenv("JME_REQUIRE_SERVICES") != "" {
			t.Fatalf("JME_REQUIRE_SERVICES is set but no Redis at %s: %v", url, err)
		}
		t.Skipf("no Redis at %s (run `docker compose up -d`): %v", url, err)
	}
	return client
}

// each test gets its own stream so they can run in any order without interference
func testConfig(t *testing.T, consumer string) Config {
	t.Helper()
	stream := fmt.Sprintf("test:%s:%d", t.Name(), time.Now().UnixNano())
	return Config{
		Stream:            stream,
		Group:             "testers",
		Consumer:          consumer,
		DeadLetterStream:  stream + ":dead",
		BlockTimeout:      50 * time.Millisecond,
		BatchSize:         8,
		VisibilityTimeout: 20 * time.Millisecond,
		MaxAttempts:       3,
		ClaimInterval:     10 * time.Millisecond,
	}
}

func cleanup(t *testing.T, client *redis.Client, cfg Config) {
	t.Helper()
	t.Cleanup(func() {
		ctx := context.Background()
		client.Del(ctx, cfg.Stream, cfg.DeadLetterStream, cfg.Stream+":attempts")
		_ = client.Close()
	})
}

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// TestReclaimAfterWorkerDeath is the core guarantee: a worker that reads a message
// and then dies without acking must not strand that message.
//
// A SIGKILLed process is indistinguishable, from Redis's point of view, from one
// that read an entry and never acked it - the entry sits in the PEL accruing idle
// time either way. So the crash is simulated by issuing the XREADGROUP that a dead
// worker would have issued and then simply never acking it.
func TestReclaimAfterWorkerDeath(t *testing.T) {
	client := testClient(t)
	cfg := testConfig(t, "worker-b")
	cleanup(t, client, cfg)
	ctx := context.Background()

	survivor := New(client, cfg, quietLogger())
	if err := survivor.EnsureGroup(ctx); err != nil {
		t.Fatalf("ensure group: %v", err)
	}

	producer := NewProducer(client, cfg.Stream)
	if _, err := producer.Publish(ctx, domain.FetchJob{
		PostingID: 42, URL: "https://example.com/jobs/42", Company: "Acme",
	}); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// worker-a takes the message, then "dies" - no ack, no further reads
	dead, err := client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: cfg.Group, Consumer: "worker-a",
		Streams: []string{cfg.Stream, ">"}, Count: 1, Block: time.Second,
	}).Result()
	if err != nil {
		t.Fatalf("simulated worker read: %v", err)
	}
	if len(dead) != 1 || len(dead[0].Messages) != 1 {
		t.Fatalf("expected worker-a to receive exactly 1 message, got %+v", dead)
	}

	// nothing else can read it via ">" while it is owned by worker-a
	none, err := client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: cfg.Group, Consumer: cfg.Consumer,
		Streams: []string{cfg.Stream, ">"}, Count: 1, Block: 50 * time.Millisecond,
	}).Result()
	if err != redis.Nil && len(none) > 0 && len(none[0].Messages) > 0 {
		t.Fatal("message was readable by a second consumer while still owned by worker-a")
	}

	// let it go idle past the visibility timeout, then run the survivor
	time.Sleep(cfg.VisibilityTimeout + 20*time.Millisecond)

	var got domain.FetchJob
	done := make(chan struct{})
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	go func() {
		_ = survivor.Run(runCtx, func(_ context.Context, msg Message) error {
			if err := msg.Decode(&got); err != nil {
				return err
			}
			close(done)
			return nil
		})
	}()

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("survivor never reclaimed the abandoned message")
	}
	cancel()

	if got.PostingID != 42 {
		t.Fatalf("reclaimed the wrong payload: %+v", got)
	}

	// and once completed by the survivor, nothing is left pending
	waitFor(t, 2*time.Second, func() bool {
		pending, err := survivor.PendingSummary(ctx)
		return err == nil && pending.Count == 0
	}, "PEL did not drain after the survivor acked")
}

// TestDeadLetterAfterExactlyMaxAttempts pins the retry budget. Off-by-one here is
// the difference between "retries three times" and "retries forever".
func TestDeadLetterAfterExactlyMaxAttempts(t *testing.T) {
	client := testClient(t)
	cfg := testConfig(t, "worker-1")
	cleanup(t, client, cfg)
	ctx := context.Background()

	consumer := New(client, cfg, quietLogger())
	if err := consumer.EnsureGroup(ctx); err != nil {
		t.Fatalf("ensure group: %v", err)
	}

	producer := NewProducer(client, cfg.Stream)
	if _, err := producer.Publish(ctx, domain.FetchJob{
		PostingID: 7, URL: "https://example.com/jobs/7",
	}); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var mu sync.Mutex
	attempts := 0

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		_ = consumer.Run(runCtx, func(_ context.Context, msg Message) error {
			mu.Lock()
			attempts++
			seen := attempts
			mu.Unlock()
			return fmt.Errorf("simulated transient failure #%d", seen)
		})
	}()

	waitFor(t, 5*time.Second, func() bool {
		n, err := client.XLen(ctx, cfg.DeadLetterStream).Result()
		return err == nil && n == 1
	}, "message never reached the dead letter stream")

	// give the loop a moment to (incorrectly) retry again if the budget is wrong
	time.Sleep(200 * time.Millisecond)
	cancel()

	mu.Lock()
	final := attempts
	mu.Unlock()
	if final != cfg.MaxAttempts {
		t.Fatalf("handler ran %d times, want exactly MaxAttempts=%d", final, cfg.MaxAttempts)
	}

	entries, err := client.XRange(ctx, cfg.DeadLetterStream, "-", "+").Result()
	if err != nil {
		t.Fatalf("read dead letter: %v", err)
	}
	values := entries[0].Values
	if values["attempts"] != fmt.Sprint(cfg.MaxAttempts) {
		t.Fatalf("dead letter attempts = %v, want %d", values["attempts"], cfg.MaxAttempts)
	}
	if values["source_stream"] != cfg.Stream {
		t.Fatalf("dead letter source_stream = %v, want %s", values["source_stream"], cfg.Stream)
	}
	if values["reason"] == "" {
		t.Fatal("dead letter entry has no failure reason")
	}

	// dead lettered means acked: it must not still be pending
	pending, err := consumer.PendingSummary(ctx)
	if err != nil {
		t.Fatalf("pending: %v", err)
	}
	if pending.Count != 0 {
		t.Fatalf("dead lettered message is still pending (count=%d)", pending.Count)
	}
}

// TestPermanentFailureSkipsRetries: a handler that reports the work can never
// succeed should not burn the retry budget first.
func TestPermanentFailureSkipsRetries(t *testing.T) {
	client := testClient(t)
	cfg := testConfig(t, "worker-1")
	cleanup(t, client, cfg)
	ctx := context.Background()

	consumer := New(client, cfg, quietLogger())
	if err := consumer.EnsureGroup(ctx); err != nil {
		t.Fatalf("ensure group: %v", err)
	}
	producer := NewProducer(client, cfg.Stream)
	if _, err := producer.Publish(ctx, domain.FetchJob{PostingID: 9, URL: "https://x/9"}); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var mu sync.Mutex
	calls := 0

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		_ = consumer.Run(runCtx, func(_ context.Context, _ Message) error {
			mu.Lock()
			calls++
			mu.Unlock()
			return Permanent("posting has been deleted upstream")
		})
	}()

	waitFor(t, 3*time.Second, func() bool {
		n, err := client.XLen(ctx, cfg.DeadLetterStream).Result()
		return err == nil && n == 1
	}, "permanent failure never reached the dead letter stream")
	time.Sleep(150 * time.Millisecond)
	cancel()

	mu.Lock()
	defer mu.Unlock()
	if calls != 1 {
		t.Fatalf("handler ran %d times for a permanent failure, want 1", calls)
	}
}

// TestRedeliveryDoesNotDuplicateRows is the at-least-once contract stated as a test.
// The store here stands in for `posting_jd`, whose real write is an upsert keyed on
// posting_id - so a second delivery updates rather than inserts.
func TestRedeliveryDoesNotDuplicateRows(t *testing.T) {
	client := testClient(t)
	cfg := testConfig(t, "worker-1")
	cleanup(t, client, cfg)
	ctx := context.Background()

	consumer := New(client, cfg, quietLogger())
	if err := consumer.EnsureGroup(ctx); err != nil {
		t.Fatalf("ensure group: %v", err)
	}

	producer := NewProducer(client, cfg.Stream)
	job := domain.FetchJob{PostingID: 101, URL: "https://example.com/jobs/101"}
	for i := 0; i < 2; i++ {
		if _, err := producer.Publish(ctx, job); err != nil {
			t.Fatalf("publish: %v", err)
		}
	}

	var mu sync.Mutex
	rows := map[int64]int{} // posting_id -> write count, i.e. an upsert
	deliveries := 0

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		_ = consumer.Run(runCtx, func(_ context.Context, msg Message) error {
			var decoded domain.FetchJob
			if err := msg.Decode(&decoded); err != nil {
				return Permanent("undecodable: %v", err)
			}
			mu.Lock()
			rows[decoded.PostingID]++
			deliveries++
			mu.Unlock()
			return nil
		})
	}()

	waitFor(t, 3*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return deliveries >= 2
	}, "both deliveries never arrived")
	cancel()

	mu.Lock()
	defer mu.Unlock()
	if len(rows) != 1 {
		t.Fatalf("got %d distinct rows, want 1 - redelivery duplicated a row", len(rows))
	}
	if rows[101] < 2 {
		t.Fatalf("handler saw the posting %d times, expected at least 2 deliveries", rows[101])
	}
}

// TestGracefulShutdownFinishesInFlightWork: cancelling the context must not turn
// work that already completed into a redelivery.
func TestGracefulShutdownFinishesInFlightWork(t *testing.T) {
	client := testClient(t)
	cfg := testConfig(t, "worker-1")
	cfg.VisibilityTimeout = time.Hour // keep autoclaim out of this test
	cleanup(t, client, cfg)
	ctx := context.Background()

	consumer := New(client, cfg, quietLogger())
	if err := consumer.EnsureGroup(ctx); err != nil {
		t.Fatalf("ensure group: %v", err)
	}
	producer := NewProducer(client, cfg.Stream)
	if _, err := producer.Publish(ctx, domain.FetchJob{PostingID: 5, URL: "https://x/5"}); err != nil {
		t.Fatalf("publish: %v", err)
	}

	runCtx, cancel := context.WithCancel(ctx)
	started := make(chan struct{})
	finished := make(chan struct{})

	go func() {
		_ = consumer.Run(runCtx, func(_ context.Context, _ Message) error {
			close(started)
			// shutdown arrives while this handler is mid-flight
			time.Sleep(150 * time.Millisecond)
			close(finished)
			return nil
		})
	}()

	<-started
	cancel() // SIGTERM equivalent

	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		t.Fatal("in-flight handler was abandoned on shutdown")
	}

	// the ack must have landed despite the cancelled context
	waitFor(t, 2*time.Second, func() bool {
		pending, err := consumer.PendingSummary(context.Background())
		return err == nil && pending.Count == 0
	}, "completed work was left pending after shutdown, so it would be redelivered")
}

func waitFor(t *testing.T, timeout time.Duration, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal(msg)
}
