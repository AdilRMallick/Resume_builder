package store

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

func testDSN() string {
	if dsn := os.Getenv("JME_TEST_DATABASE_URL_GO"); dsn != "" {
		return dsn
	}
	return "postgresql://jme:jme@localhost:5433/jme"
}

func testStore(t *testing.T) *Store {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	s, err := New(ctx, testDSN())
	if err != nil {
		t.Skipf("no Postgres at %s (run `docker compose up -d`): %v", testDSN(), err)
	}
	t.Cleanup(s.Close)
	return s
}

// seedPosting inserts a throwaway posting and returns its id, cleaning up after.
func seedPosting(t *testing.T, s *Store) int64 {
	t.Helper()
	ctx := context.Background()
	key := fmt.Sprintf("test-%s-%d", t.Name(), time.Now().UnixNano())

	// Columns like is_remote and repost_count carry Python-side ORM defaults rather
	// than server defaults, so a raw insert has to supply them. The fetcher never
	// inserts postings in production - that is the ingestor's job - so this is test
	// scaffolding, not a shape the service depends on.
	var id int64
	err := s.pool.QueryRow(ctx, `
		INSERT INTO posting (
			canonical_key, company, title, url,
			is_remote, repost_count, first_seen_at, last_seen_at
		)
		VALUES ($1, 'Acme', 'New Grad SWE', 'https://example.com/jobs/1',
		        false, 0, now(), now())
		RETURNING id
	`, key).Scan(&id)
	if err != nil {
		t.Fatalf("seed posting: %v", err)
	}
	t.Cleanup(func() {
		_, _ = s.pool.Exec(context.Background(), `DELETE FROM posting WHERE id = $1`, id)
	})
	return id
}

func countJDRows(t *testing.T, s *Store, postingID int64) int {
	t.Helper()
	var n int
	if err := s.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM posting_jd WHERE posting_id = $1`, postingID).Scan(&n); err != nil {
		t.Fatalf("count posting_jd: %v", err)
	}
	return n
}

// TestRedeliveryDoesNotDuplicateJDRows is the database half of the at-least-once
// contract: the queue guarantees a message may arrive twice, so the write it drives
// has to be an upsert.
func TestRedeliveryDoesNotDuplicateJDRows(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	postingID := seedPosting(t, s)

	jd := domain.JobDescription{
		RawText: "We are looking for a new grad engineer with Go and Postgres experience.",
		Title:   "New Grad Software Engineer",
		Adapter: "greenhouse",
	}

	first, err := s.RecordSuccess(ctx, postingID, jd)
	if err != nil {
		t.Fatalf("first write: %v", err)
	}
	second, err := s.RecordSuccess(ctx, postingID, jd)
	if err != nil {
		t.Fatalf("redelivered write: %v", err)
	}

	if got := countJDRows(t, s, postingID); got != 1 {
		t.Fatalf("posting_jd rows = %d after redelivery, want 1", got)
	}
	if first != second {
		t.Fatalf("same text produced different hashes: %s vs %s", first, second)
	}

	var attempts int
	var status string
	if err := s.pool.QueryRow(ctx,
		`SELECT attempts, fetch_status::text FROM posting_jd WHERE posting_id = $1`,
		postingID).Scan(&attempts, &status); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("attempts = %d, want 2 (the redelivery should be counted, not hidden)", attempts)
	}
	if status != "ok" {
		t.Fatalf("fetch_status = %q, want ok", status)
	}
}

// TestFailureThenSuccessRecovers: a posting that failed once and later resolved must
// end up clean, with the stale error cleared.
func TestFailureThenSuccessRecovers(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	postingID := seedPosting(t, s)

	failure := &domain.FetchError{
		Kind: domain.ErrTransient, StatusCode: 503, Adapter: "fallback",
		URL: "https://example.com/jobs/1", Err: fmt.Errorf("upstream unavailable"),
	}
	if err := s.RecordFailure(ctx, postingID, "fallback", failure); err != nil {
		t.Fatalf("record failure: %v", err)
	}

	resolved, err := s.AlreadyResolved(ctx, postingID)
	if err != nil {
		t.Fatalf("already resolved: %v", err)
	}
	if resolved {
		t.Fatal("a failed fetch must not count as resolved")
	}

	if _, err := s.RecordSuccess(ctx, postingID, domain.JobDescription{
		RawText: "Job description text.", Adapter: "lever",
	}); err != nil {
		t.Fatalf("record success: %v", err)
	}

	if got := countJDRows(t, s, postingID); got != 1 {
		t.Fatalf("posting_jd rows = %d, want 1", got)
	}

	var status, adapter string
	var fetchError *string
	if err := s.pool.QueryRow(ctx,
		`SELECT fetch_status::text, adapter, fetch_error FROM posting_jd WHERE posting_id = $1`,
		postingID).Scan(&status, &adapter, &fetchError); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if status != "ok" || adapter != "lever" {
		t.Fatalf("status=%q adapter=%q, want ok/lever", status, adapter)
	}
	if fetchError != nil {
		t.Fatalf("stale fetch_error survived a successful retry: %q", *fetchError)
	}

	resolved, err = s.AlreadyResolved(ctx, postingID)
	if err != nil {
		t.Fatalf("already resolved: %v", err)
	}
	if !resolved {
		t.Fatal("a successful fetch should be permanently cached as resolved")
	}
}

// TestErrorStatusMapping pins the sentinel-to-column mapping. A drift here would
// silently mis-file failures and corrupt the coverage number.
func TestErrorStatusMapping(t *testing.T) {
	cases := []struct {
		err  error
		want domain.FetchStatus
	}{
		{nil, domain.StatusOK},
		{domain.ErrNotFound, domain.StatusNotFound},
		{domain.ErrRateLimited, domain.StatusRateLimited},
		{domain.ErrRobotsDenied, domain.StatusRobotsDenied},
		{domain.ErrPermanent, domain.StatusPermanentError},
		{domain.ErrTransient, domain.StatusTransientError},
		{domain.ErrCircuitOpen, domain.StatusTransientError},
	}
	for _, tc := range cases {
		if got := domain.StatusFor(tc.err); got != tc.want {
			t.Errorf("StatusFor(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}

	// and the retry policy that follows from it
	if domain.Retryable(domain.ErrNotFound) {
		t.Error("a 404 must not be retried")
	}
	if !domain.Retryable(domain.ErrTransient) {
		t.Error("a transient error must be retried")
	}
}

func TestMarkPendingIsIdempotent(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	postingID := seedPosting(t, s)

	for i := 0; i < 3; i++ {
		if err := s.MarkPending(ctx, postingID); err != nil {
			t.Fatalf("mark pending #%d: %v", i, err)
		}
	}
	if got := countJDRows(t, s, postingID); got != 1 {
		t.Fatalf("posting_jd rows = %d, want 1", got)
	}
}
