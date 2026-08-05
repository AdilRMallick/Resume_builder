// Package queue is a Redis Streams consumer-group harness.
//
// Why streams rather than a list-based queue (LPUSH/BRPOP): a list gives you no
// record that a message was ever handed out. If the worker holding it dies, the
// message is simply gone. Streams keep a per-group Pending Entries List (PEL): a
// message read by XREADGROUP stays in the PEL, attributed to that consumer, until
// it is XACKed. That gives three things a list cannot:
//
//	XACK        completion, explicitly, after the database work has committed
//	XAUTOCLAIM  recovery, by transferring ownership of entries idle longer than a
//	            visibility timeout to a live consumer
//	XPENDING    visibility, into what is stuck and for how long
//
// Delivery is at-least-once. That is a deliberate choice, not a limitation: the
// alternative (ack before processing) is at-most-once, which silently drops work.
// At-least-once pushes the burden onto handlers, which must be idempotent.
//
// The Python side (jme/queue.py) mirrors this exactly. Both agree on:
//   - entry shape: one field, "payload", holding a JSON object
//   - attempt tracking: a Redis hash "<stream>:attempts" keyed by message id
//   - dead letter shape: payload plus reason, attempts, source_stream, failed_at
package queue

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// Message is one delivery. Attempt is 1 on first delivery and increments on each
// redelivery, so a handler can behave differently on a retry if it needs to.
type Message struct {
	ID      string
	Values  map[string]any
	Attempt int
}

// Decode unmarshals the entry's payload field into out.
func (m Message) Decode(out any) error {
	return domain.Unmarshal(m.Values, out)
}

// Handler processes one message. Returning nil means "done, and any database work
// is committed" - only then is the message XACKed.
type Handler func(ctx context.Context, msg Message) error

// PermanentError tells the harness not to bother retrying: the message goes
// straight to the dead letter stream. Use it for malformed payloads and for work
// that can never succeed, such as a posting that no longer exists.
type PermanentError struct{ Reason string }

func (e *PermanentError) Error() string { return e.Reason }

func Permanent(format string, args ...any) error {
	return &PermanentError{Reason: fmt.Sprintf(format, args...)}
}

// Producer publishes to a stream.
type Producer struct {
	client *redis.Client
	stream string
	maxLen int64
}

func NewProducer(client *redis.Client, stream string) *Producer {
	return &Producer{client: client, stream: stream, maxLen: 100_000}
}

func (p *Producer) Publish(ctx context.Context, payload any) (string, error) {
	values, err := domain.Marshal(payload)
	if err != nil {
		return "", err
	}
	return p.client.XAdd(ctx, &redis.XAddArgs{
		Stream: p.stream,
		MaxLen: p.maxLen,
		Approx: true,
		Values: values,
	}).Result()
}

// Config tunes a Consumer. Zero values fall back to the defaults in New.
type Config struct {
	Stream            string
	Group             string
	Consumer          string
	DeadLetterStream  string
	BlockTimeout      time.Duration
	BatchSize         int64
	VisibilityTimeout time.Duration
	MaxAttempts       int
	ClaimInterval     time.Duration
}

// Consumer is one worker in a consumer group.
//
// Not safe for concurrent use by multiple goroutines. To scale out, create several
// Consumers with distinct Consumer names - that is what the group is for, and it is
// also what makes XAUTOCLAIM able to rescue a dead worker's messages.
type Consumer struct {
	client *redis.Client
	cfg    Config
	log    *slog.Logger

	attemptsKey string
	lastClaim   time.Time
}

func New(client *redis.Client, cfg Config, log *slog.Logger) *Consumer {
	if cfg.BlockTimeout == 0 {
		cfg.BlockTimeout = 5 * time.Second
	}
	if cfg.BatchSize == 0 {
		cfg.BatchSize = 8
	}
	if cfg.VisibilityTimeout == 0 {
		cfg.VisibilityTimeout = 2 * time.Minute
	}
	if cfg.MaxAttempts == 0 {
		cfg.MaxAttempts = 3
	}
	if cfg.ClaimInterval == 0 {
		cfg.ClaimInterval = 30 * time.Second
	}
	if log == nil {
		log = slog.Default()
	}
	return &Consumer{
		client:      client,
		cfg:         cfg,
		log:         log.With("stream", cfg.Stream, "consumer", cfg.Consumer),
		attemptsKey: cfg.Stream + ":attempts",
	}
}

// EnsureGroup creates the consumer group, and the stream with it if the stream does
// not exist yet. Starting at "0" rather than "$" means a group created after
// messages were published still sees them; "$" would skip everything already there.
func (c *Consumer) EnsureGroup(ctx context.Context) error {
	err := c.client.XGroupCreateMkStream(ctx, c.cfg.Stream, c.cfg.Group, "0").Err()
	if err != nil && !isBusyGroup(err) {
		return fmt.Errorf("create consumer group: %w", err)
	}
	return nil
}

func isBusyGroup(err error) bool {
	return err != nil && err.Error() == "BUSYGROUP Consumer Group name already exists"
}

// Run consumes until ctx is cancelled.
//
// Shutdown is graceful in the sense that matters: cancelling ctx stops the next
// blocking read, but a handler already running is allowed to finish, and its
// XACK still goes through - the acknowledgement uses a context detached from
// cancellation so a SIGTERM mid-job cannot turn completed work into a redelivery.
func (c *Consumer) Run(ctx context.Context, handler Handler) error {
	if err := c.EnsureGroup(ctx); err != nil {
		return err
	}
	c.log.Info("consumer started",
		"group", c.cfg.Group,
		"max_attempts", c.cfg.MaxAttempts,
		"visibility_timeout", c.cfg.VisibilityTimeout,
	)

	for {
		if ctx.Err() != nil {
			c.log.Info("consumer stopped")
			return nil
		}

		messages, err := c.read(ctx)
		if err != nil {
			if ctx.Err() != nil {
				c.log.Info("consumer stopped")
				return nil
			}
			c.log.Error("read failed", "error", err)
			select {
			case <-ctx.Done():
			case <-time.After(time.Second):
			}
			continue
		}

		for _, msg := range messages {
			c.process(ctx, msg, handler)
		}
	}
}

func (c *Consumer) read(ctx context.Context) ([]Message, error) {
	// Reclaim before reading new work. A worker that only ever reads ">" will never
	// notice a message stranded by a crashed peer.
	if time.Since(c.lastClaim) >= c.cfg.ClaimInterval {
		c.lastClaim = time.Now()
		claimed, err := c.Autoclaim(ctx)
		if err != nil {
			c.log.Error("autoclaim failed", "error", err)
		} else if len(claimed) > 0 {
			return claimed, nil
		}
	}

	streams, err := c.client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    c.cfg.Group,
		Consumer: c.cfg.Consumer,
		Streams:  []string{c.cfg.Stream, ">"},
		Count:    c.cfg.BatchSize,
		Block:    c.cfg.BlockTimeout,
	}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil // block timeout expired with nothing to read
	}
	if err != nil {
		return nil, err
	}

	var out []Message
	for _, stream := range streams {
		for _, entry := range stream.Messages {
			out = append(out, Message{ID: entry.ID, Values: entry.Values})
		}
	}
	return out, nil
}

// Autoclaim transfers ownership of entries idle longer than the visibility timeout
// to this consumer.
//
// This is the whole recovery story. A SIGKILLed worker leaves its messages in the
// PEL with an idle time that only grows; the next sweep by any live consumer takes
// them over and reprocesses them. Note the trade-off the visibility timeout encodes:
// too short and a slow-but-healthy worker has its work stolen and done twice (safe,
// because handlers are idempotent, but wasteful); too long and recovery from a real
// crash is delayed by that long.
func (c *Consumer) Autoclaim(ctx context.Context) ([]Message, error) {
	var out []Message
	cursor := "0-0"

	for {
		entries, next, err := c.client.XAutoClaim(ctx, &redis.XAutoClaimArgs{
			Stream:   c.cfg.Stream,
			Group:    c.cfg.Group,
			Consumer: c.cfg.Consumer,
			MinIdle:  c.cfg.VisibilityTimeout,
			Start:    cursor,
			Count:    c.cfg.BatchSize,
		}).Result()
		if err != nil {
			return out, err
		}

		for _, entry := range entries {
			c.log.Info("message reclaimed", "id", entry.ID)
			out = append(out, Message{ID: entry.ID, Values: entry.Values})
		}

		// XAUTOCLAIM returns "0-0" once it has walked the whole PEL.
		if next == "0-0" || next == "" || len(entries) == 0 {
			return out, nil
		}
		cursor = next
	}
}

func (c *Consumer) process(ctx context.Context, msg Message, handler Handler) {
	// Detached from cancellation: bookkeeping for a message we have already taken
	// ownership of must complete even when a shutdown is in flight.
	finishCtx := context.WithoutCancel(ctx)

	attempt, err := c.client.HIncrBy(finishCtx, c.attemptsKey, msg.ID, 1).Result()
	if err != nil {
		c.log.Error("attempt counter failed", "id", msg.ID, "error", err)
		return
	}
	msg.Attempt = int(attempt)

	handlerErr := handler(ctx, msg)

	switch {
	case handlerErr == nil:
		if err := c.ack(finishCtx, msg.ID); err != nil {
			// The work is committed but the ack failed. The message will be
			// redelivered and the handler's idempotency has to absorb it. Loud,
			// because it means the next run does duplicate effort.
			c.log.Error("ack failed after successful handler", "id", msg.ID, "error", err)
		}

	case isPermanent(handlerErr):
		c.deadLetter(finishCtx, msg, handlerErr.Error())

	case msg.Attempt >= c.cfg.MaxAttempts:
		c.log.Error("giving up on message",
			"id", msg.ID, "attempt", msg.Attempt, "error", handlerErr)
		c.deadLetter(finishCtx, msg, handlerErr.Error())

	default:
		// Leave it pending and unacked. No explicit requeue is needed or wanted:
		// the entry is still in the PEL, so the next autoclaim sweep past the
		// visibility timeout picks it up. That also gives redelivery a natural
		// backoff without a sleep here holding up the rest of the batch.
		c.log.Warn("message failed, will be redelivered",
			"id", msg.ID, "attempt", msg.Attempt, "max_attempts", c.cfg.MaxAttempts,
			"error", handlerErr)
	}
}

func isPermanent(err error) bool {
	var perm *PermanentError
	if errors.As(err, &perm) {
		return true
	}
	// A fetch that can never succeed is permanent too.
	return err != nil && !domain.Retryable(err)
}

func (c *Consumer) ack(ctx context.Context, id string) error {
	pipe := c.client.TxPipeline()
	pipe.XAck(ctx, c.cfg.Stream, c.cfg.Group, id)
	pipe.HDel(ctx, c.attemptsKey, id)
	_, err := pipe.Exec(ctx)
	return err
}

// deadLetter parks a message that will never succeed, then acks it so it stops
// being redelivered. Acking without recording it first would be a silent drop,
// which is the one outcome this whole design exists to prevent.
func (c *Consumer) deadLetter(ctx context.Context, msg Message, reason string) {
	payload, _ := msg.Values["payload"].(string)

	pipe := c.client.TxPipeline()
	pipe.XAdd(ctx, &redis.XAddArgs{
		Stream: c.cfg.DeadLetterStream,
		Values: map[string]any{
			"payload":       payload,
			"reason":        reason,
			"attempts":      fmt.Sprint(msg.Attempt),
			"source_stream": c.cfg.Stream,
			"failed_at":     time.Now().UTC().Format(time.RFC3339),
		},
	})
	pipe.XAck(ctx, c.cfg.Stream, c.cfg.Group, msg.ID)
	pipe.HDel(ctx, c.attemptsKey, msg.ID)

	if _, err := pipe.Exec(ctx); err != nil {
		c.log.Error("dead letter failed", "id", msg.ID, "error", err)
		return
	}
	c.log.Warn("message dead lettered",
		"id", msg.ID, "attempts", msg.Attempt, "reason", reason)
}

// Depth is the number of entries in the stream.
func (c *Consumer) Depth(ctx context.Context) (int64, error) {
	return c.client.XLen(ctx, c.cfg.Stream).Result()
}

// PendingSummary reports how much work is in flight and how stale the oldest of it
// is. Rising pending age is the signal that a worker died or is wedged.
func (c *Consumer) PendingSummary(ctx context.Context) (*redis.XPending, error) {
	return c.client.XPending(ctx, c.cfg.Stream, c.cfg.Group).Result()
}
