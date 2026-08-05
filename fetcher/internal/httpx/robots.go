package httpx

import (
	"context"
	"errors"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// maxRobotsBytes caps how much of a robots.txt we will read. Google's own limit
// is 500KiB; anything past that is not a robots file.
const maxRobotsBytes = 512 << 10

// DefaultRobotsTTL is how long a fetched robots.txt is trusted.
const DefaultRobotsTTL = 24 * time.Hour

// shortRobotsTTL is used when robots.txt could not be fetched. We fail open (a
// host being briefly down should not stall the whole run) but re-check soon.
const shortRobotsTTL = 5 * time.Minute

// Cache is the tiny slice of Redis the robots agent needs. Injectable so the
// parser and the caching behaviour are testable without a server.
type Cache interface {
	Get(ctx context.Context, key string) (string, bool, error)
	Set(ctx context.Context, key, val string, ttl time.Duration) error
}

// RedisCache is the production Cache.
type RedisCache struct {
	rdb redis.Cmdable
}

// NewRedisCache wraps a Redis client as a Cache.
func NewRedisCache(rdb redis.Cmdable) *RedisCache { return &RedisCache{rdb: rdb} }

func (c *RedisCache) Get(ctx context.Context, key string) (string, bool, error) {
	v, err := c.rdb.Get(ctx, key).Result()
	if errors.Is(err, redis.Nil) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return v, true, nil
}

func (c *RedisCache) Set(ctx context.Context, key, val string, ttl time.Duration) error {
	return c.rdb.Set(ctx, key, val, ttl).Err()
}

// RobotsAgent fetches, caches and applies robots.txt for the configured user
// agent. One robots.txt per host per TTL, shared across worker processes through
// the Cache, and memoised in-process on top so a run of 200 postings on one host
// parses it once.
type RobotsAgent struct {
	cache  Cache
	Prefix string
	TTL    time.Duration

	// Fetch performs the network GET for a robots.txt URL and returns the body
	// and status code. Client wires this to its own rate limited transport.
	Fetch func(ctx context.Context, robotsURL string) (body string, status int, err error)

	// token is the product token of our user agent, lowercased. "job-match-engine"
	// out of "job-match-engine/0.1 (+https://...)".
	token string

	mu    sync.Mutex
	local map[string]*robotsEntry // host -> parsed rules
	locks map[string]*sync.Mutex  // host -> single-flight guard

	now func() time.Time
}

type robotsEntry struct {
	rules   *robotsRules
	expires time.Time
}

// NewRobotsAgent builds an agent for the given user agent string.
func NewRobotsAgent(cache Cache, userAgent string) *RobotsAgent {
	return &RobotsAgent{
		cache:  cache,
		Prefix: DefaultKeyPrefix,
		TTL:    DefaultRobotsTTL,
		token:  productToken(userAgent),
		local:  map[string]*robotsEntry{},
		locks:  map[string]*sync.Mutex{},
		now:    time.Now,
	}
}

// Check reports whether u may be fetched, plus any Crawl-delay the host asked
// for. On an unusable robots.txt it fails open (allowed=true) and says so with a
// nil error, because a host that is briefly 500ing should not look like a host
// that forbade us.
func (r *RobotsAgent) Check(ctx context.Context, u *url.URL) (allowed bool, crawlDelay time.Duration, err error) {
	rules, err := r.rulesFor(ctx, u)
	if err != nil {
		return true, 0, err
	}
	return rules.allowed(pathAndQuery(u)), rules.crawlDelay, nil
}

func (r *RobotsAgent) rulesFor(ctx context.Context, u *url.URL) (*robotsRules, error) {
	host := hostKey(u)

	if rules := r.fromLocal(host); rules != nil {
		return rules, nil
	}

	// Single-flight per host: 8 workers starting at once must not each fetch
	// robots.txt.
	lock := r.lockFor(host)
	lock.Lock()
	defer lock.Unlock()

	if rules := r.fromLocal(host); rules != nil {
		return rules, nil
	}

	key := r.Prefix + ":robots:" + host
	ttl := r.TTL
	if ttl <= 0 {
		ttl = DefaultRobotsTTL
	}

	if r.cache != nil {
		if body, ok, err := r.cache.Get(ctx, key); err == nil && ok {
			rules := parseRobots(body, r.token)
			r.store(host, rules, ttl)
			return rules, nil
		} else if err != nil && ctx.Err() != nil {
			return emptyRobots(), ctx.Err()
		}
	}

	if r.Fetch == nil {
		return emptyRobots(), errors.New("robots: no fetch function configured")
	}

	robotsURL := (&url.URL{Scheme: u.Scheme, Host: u.Host, Path: "/robots.txt"}).String()
	body, status, err := r.Fetch(ctx, robotsURL)
	switch {
	case err != nil:
		// Network failure: fail open, but only briefly, and do not poison the
		// shared cache with a body we never received.
		rules := emptyRobots()
		r.store(host, rules, shortRobotsTTL)
		return rules, nil
	case status >= 200 && status < 300:
		// Good robots.txt. Cache the raw body, not the parse, so the format of
		// the cached value stays inspectable with redis-cli.
	case status >= 400 && status < 500:
		// No robots.txt (or forbidden): the standard reading is "everything is
		// allowed". Cache the emptiness for the full TTL.
		body = ""
	default:
		// 5xx or anything else weird: fail open, short TTL, no shared cache write.
		rules := emptyRobots()
		r.store(host, rules, shortRobotsTTL)
		return rules, nil
	}

	if r.cache != nil {
		_ = r.cache.Set(ctx, key, body, ttl)
	}
	rules := parseRobots(body, r.token)
	r.store(host, rules, ttl)
	return rules, nil
}

func (r *RobotsAgent) fromLocal(host string) *robotsRules {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.local[host]
	if !ok || r.now().After(e.expires) {
		return nil
	}
	return e.rules
}

func (r *RobotsAgent) store(host string, rules *robotsRules, ttl time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.local[host] = &robotsEntry{rules: rules, expires: r.now().Add(ttl)}
}

func (r *RobotsAgent) lockFor(host string) *sync.Mutex {
	r.mu.Lock()
	defer r.mu.Unlock()
	l, ok := r.locks[host]
	if !ok {
		l = &sync.Mutex{}
		r.locks[host] = l
	}
	return l
}

// ---------------------------------------------------------------------------
// Parser
//
// A deliberately small implementation of the robots.txt exclusion rules, sized
// to what ATS hosts actually publish:
//   - groups keyed by User-agent, longest matching agent token wins, "*" last
//   - Allow and Disallow with "*" wildcards and "$" end-of-path anchoring
//   - longest matching rule wins; Allow beats Disallow on an exact tie
//   - Crawl-delay, honoured by lowering the host's token bucket rate
//
// Everything else (Sitemap, Host, unknown directives) is ignored.
// ---------------------------------------------------------------------------

type robotsRule struct {
	pattern string
	re      *regexp.Regexp
	allow   bool
}

type robotsRules struct {
	rules      []robotsRule
	crawlDelay time.Duration
}

func emptyRobots() *robotsRules { return &robotsRules{} }

// allowed applies longest-match-wins to a path (plus query, which matters for
// rules like "Disallow: /*?preview=").
func (r *robotsRules) allowed(path string) bool {
	if r == nil || len(r.rules) == 0 {
		return true
	}
	if path == "" {
		path = "/"
	}
	best := -1
	allow := true
	for _, rule := range r.rules {
		if !rule.re.MatchString(path) {
			continue
		}
		n := len(rule.pattern)
		if n > best || (n == best && rule.allow) {
			best = n
			allow = rule.allow
		}
	}
	return allow
}

type robotsGroup struct {
	agents     []string
	rules      []robotsRule
	crawlDelay time.Duration
}

func parseRobots(body, token string) *robotsRules {
	var (
		groups     []*robotsGroup
		cur        *robotsGroup
		expectMore bool // still collecting User-agent lines for the current group
	)

	for _, raw := range strings.Split(body, "\n") {
		line := raw
		if i := strings.IndexByte(line, '#'); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		colon := strings.IndexByte(line, ':')
		if colon < 0 {
			continue
		}
		field := strings.ToLower(strings.TrimSpace(line[:colon]))
		value := strings.TrimSpace(line[colon+1:])

		switch field {
		case "user-agent":
			if cur == nil || !expectMore {
				cur = &robotsGroup{}
				groups = append(groups, cur)
				expectMore = true
			}
			cur.agents = append(cur.agents, strings.ToLower(value))
		case "disallow", "allow":
			if cur == nil {
				continue // rule outside any group; ignore
			}
			expectMore = false
			if value == "" {
				// "Disallow:" with no value means allow everything; an empty
				// Allow is meaningless. Either way there is no rule to add.
				continue
			}
			re, err := compileRobotsPattern(value)
			if err != nil {
				continue
			}
			cur.rules = append(cur.rules, robotsRule{pattern: value, re: re, allow: field == "allow"})
		case "crawl-delay":
			if cur == nil {
				continue
			}
			expectMore = false
			if f, err := strconv.ParseFloat(value, 64); err == nil && f > 0 && f < 3600 {
				cur.crawlDelay = time.Duration(f * float64(time.Second))
			}
		default:
			// Sitemap, Host, vendor extensions: not our problem.
		}
	}

	best := selectGroup(groups, token)
	if best == nil {
		return emptyRobots()
	}
	return &robotsRules{rules: best.rules, crawlDelay: best.crawlDelay}
}

// selectGroup picks the group whose user-agent token is the longest prefix match
// for ours, falling back to the "*" group. Matching is prefix based per the de
// facto standard: a group for "job-match" applies to "job-match-engine".
func selectGroup(groups []*robotsGroup, token string) *robotsGroup {
	var (
		best     *robotsGroup
		bestLen  = -1
		wildcard *robotsGroup
	)
	for _, g := range groups {
		for _, a := range g.agents {
			if a == "*" {
				if wildcard == nil {
					wildcard = g
				}
				continue
			}
			if token != "" && strings.HasPrefix(token, a) && len(a) > bestLen {
				best, bestLen = g, len(a)
			}
		}
	}
	if best != nil {
		return best
	}
	return wildcard
}

// compileRobotsPattern turns a robots path pattern into an anchored regexp.
// Only "*" (any run of characters) and a trailing "$" (end of path) are special;
// everything else is literal, hence the QuoteMeta on each segment.
func compileRobotsPattern(p string) (*regexp.Regexp, error) {
	anchorEnd := strings.HasSuffix(p, "$")
	if anchorEnd {
		p = strings.TrimSuffix(p, "$")
	}
	parts := strings.Split(p, "*")
	for i := range parts {
		parts[i] = regexp.QuoteMeta(parts[i])
	}
	expr := "^" + strings.Join(parts, ".*")
	if anchorEnd {
		expr += "$"
	}
	return regexp.Compile(expr)
}

// productToken extracts the comparable name from a full User-Agent string:
// "job-match-engine/0.1 (+https://example)" -> "job-match-engine".
func productToken(ua string) string {
	ua = strings.TrimSpace(strings.ToLower(ua))
	if i := strings.IndexAny(ua, "/ "); i > 0 {
		ua = ua[:i]
	}
	return ua
}

func pathAndQuery(u *url.URL) string {
	p := u.EscapedPath()
	if p == "" {
		p = "/"
	}
	if u.RawQuery != "" {
		return p + "?" + u.RawQuery
	}
	return p
}
