// Package store is the fetcher's Postgres write path.
//
// The Go service owns exactly two tables' worth of writes - posting_jd and
// run_metric - and reads posting. It deliberately does not use an ORM or share
// migrations with the Python side: the schema contract is the SQL in this file,
// and Alembic (jme/models.py) remains the single source of truth for DDL.
package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

type Store struct {
	pool *pgxpool.Pool
}

func New(ctx context.Context, dsn string) (*Store, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

func SHA256(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// AlreadyResolved reports whether this posting already has usable JD text.
//
// ARCHITECTURE.md section 7: "Permanent cache by posting id. Job descriptions do
// not change; never refetch a successful resolution." This is also what makes
// redelivery cheap - a reclaimed message for an already-resolved posting costs one
// index lookup instead of an HTTP round trip.
func (s *Store) AlreadyResolved(ctx context.Context, postingID int64) (bool, error) {
	var resolved bool
	err := s.pool.QueryRow(ctx, `
		SELECT fetch_status = 'ok' AND char_count > 0
		FROM posting_jd
		WHERE posting_id = $1
	`, postingID).Scan(&resolved)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("check resolved: %w", err)
	}
	return resolved, nil
}

// RecordSuccess writes resolved JD text. Idempotent by construction: the upsert is
// keyed on posting_id, so a redelivered message overwrites rather than duplicating.
func (s *Store) RecordSuccess(ctx context.Context, postingID int64, jd domain.JobDescription) (string, error) {
	hash := SHA256(jd.RawText)
	_, err := s.pool.Exec(ctx, `
		INSERT INTO posting_jd (
			posting_id, adapter, raw_text, text_sha256, title, location,
			fetch_status, fetch_error, attempts, char_count, extracted_at, updated_at
		)
		VALUES ($1, $2, $3, $4, $5, $6, 'ok', NULL, 1, $7, now(), now())
		ON CONFLICT (posting_id) DO UPDATE SET
			adapter      = EXCLUDED.adapter,
			raw_text     = EXCLUDED.raw_text,
			text_sha256  = EXCLUDED.text_sha256,
			title        = EXCLUDED.title,
			location     = EXCLUDED.location,
			fetch_status = 'ok',
			fetch_error  = NULL,
			attempts     = posting_jd.attempts + 1,
			char_count   = EXCLUDED.char_count,
			extracted_at = now(),
			updated_at   = now()
	`, postingID, jd.Adapter, jd.RawText, hash, nullable(jd.Title), nullable(jd.Location), len(jd.RawText))
	if err != nil {
		return "", fmt.Errorf("upsert posting_jd: %w", err)
	}
	return hash, nil
}

// RecordFailure records why a fetch failed without losing the posting.
//
// ARCHITECTURE.md section 8: "Failures degrade rather than drop." The posting stays
// in the queue and is matched on title and company alone, flagged low confidence.
func (s *Store) RecordFailure(ctx context.Context, postingID int64, adapter string, fetchErr error) error {
	status := string(domain.StatusFor(fetchErr))
	_, err := s.pool.Exec(ctx, `
		INSERT INTO posting_jd (
			posting_id, adapter, fetch_status, fetch_error, attempts, char_count, updated_at
		)
		VALUES ($1, $2, $3, $4, 1, 0, now())
		ON CONFLICT (posting_id) DO UPDATE SET
			adapter      = EXCLUDED.adapter,
			fetch_status = EXCLUDED.fetch_status,
			fetch_error  = EXCLUDED.fetch_error,
			attempts     = posting_jd.attempts + 1,
			updated_at   = now()
	`, postingID, adapter, status, fetchErr.Error())
	if err != nil {
		return fmt.Errorf("record fetch failure: %w", err)
	}
	return nil
}

// MarkPending claims a posting before the network call, so a crash mid-fetch is
// visible as a pending row rather than as no row at all.
func (s *Store) MarkPending(ctx context.Context, postingID int64) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO posting_jd (posting_id, fetch_status, attempts, char_count, updated_at)
		VALUES ($1, 'pending', 0, 0, now())
		ON CONFLICT (posting_id) DO NOTHING
	`, postingID)
	if err != nil {
		return fmt.Errorf("mark pending: %w", err)
	}
	return nil
}

// RecordMetric appends to run_metric. Same table the Python services write, so the
// funnel reads end-to-end from one place.
func (s *Store) RecordMetric(
	ctx context.Context, runID, stage, metric string, value float64, labels map[string]string,
) error {
	var encoded []byte
	if len(labels) > 0 {
		var err error
		encoded, err = json.Marshal(labels)
		if err != nil {
			return fmt.Errorf("encode metric labels: %w", err)
		}
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO run_metric (run_id, stage, metric, value, labels, recorded_at)
		VALUES ($1, $2, $3, $4, $5, now())
	`, runID, stage, metric, value, encoded)
	if err != nil {
		return fmt.Errorf("record metric: %w", err)
	}
	return nil
}

// AdapterCoverage is the honest measure of whether the fetcher works: the share of
// active postings resolved to JD text, broken down by adapter (ARCHITECTURE.md
// section 8 calls this a first-class metric).
type AdapterCoverage struct {
	Adapter  string
	Total    int64
	Resolved int64
}

func (s *Store) AdapterCoverage(ctx context.Context) ([]AdapterCoverage, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT COALESCE(jd.adapter, 'unattempted') AS adapter,
		       count(*) AS total,
		       count(*) FILTER (WHERE jd.fetch_status = 'ok') AS resolved
		FROM posting p
		LEFT JOIN posting_jd jd ON jd.posting_id = p.id
		WHERE p.inactive_at IS NULL
		GROUP BY 1
		ORDER BY 2 DESC
	`)
	if err != nil {
		return nil, fmt.Errorf("adapter coverage: %w", err)
	}
	defer rows.Close()

	var out []AdapterCoverage
	for rows.Next() {
		var c AdapterCoverage
		if err := rows.Scan(&c.Adapter, &c.Total, &c.Resolved); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// PostingExists guards against work for a posting that has been removed. A missing
// posting is a permanent failure, not something to retry.
func (s *Store) PostingExists(ctx context.Context, postingID int64) (bool, error) {
	var exists bool
	err := s.pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM posting WHERE id = $1)`, postingID).Scan(&exists)
	if err != nil {
		return false, fmt.Errorf("check posting exists: %w", err)
	}
	return exists, nil
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
