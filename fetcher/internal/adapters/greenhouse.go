package adapters

import (
	"context"
	"net/url"
	"regexp"
	"strings"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// GreenhouseAdapter resolves Greenhouse postings through the public Job Board
// API. No HTML scraping: Greenhouse documents this endpoint and serves it
// without auth.
//
// Endpoint:
//
//	GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?questions=false
//
// Recognised posting URL shapes, all of which carry the board token and the job
// id in the path or query:
//
//	https://boards.greenhouse.io/{board}/jobs/{id}
//	https://job-boards.greenhouse.io/{board}/jobs/{id}
//	https://job-boards.eu.greenhouse.io/{board}/jobs/{id}      (EU data residency)
//	https://boards.greenhouse.io/embed/job_app?for={board}&token={id}
//	https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}
//
// The response's `content` field is HTML that has itself been entity escaped
// ("&lt;p&gt;..."), which htmlFragmentToText unescapes before parsing.
type GreenhouseAdapter struct {
	settings
}

const greenhouseDefaultBase = "https://boards-api.greenhouse.io"

// greenhouseEUBase mirrors the API for boards on Greenhouse's EU infrastructure.
// Confirmed only by the host naming convention, so a miss here degrades to a
// normal not-found rather than anything silent.
const greenhouseEUBase = "https://boards-api.eu.greenhouse.io"

func NewGreenhouse(opts ...Option) *GreenhouseAdapter {
	return &GreenhouseAdapter{settings: newSettings(greenhouseDefaultBase, "", opts)}
}

func (a *GreenhouseAdapter) Name() string { return "greenhouse" }

func (a *GreenhouseAdapter) Detect(u *url.URL) bool {
	return hostMatches(u, "greenhouse.io")
}

// greenhousePath matches /{board}/jobs/{id} with optional extra segments.
var greenhousePath = regexp.MustCompile(`^/(?:v1/boards/)?([A-Za-z0-9._-]+)/jobs/(\d+)`)

func (a *GreenhouseAdapter) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse url: %w", err)
	}
	board, jobID, err := greenhouseIDs(u)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "%w", err)
	}

	base := greenhouseAPIBase(a.baseURL, u.Hostname())
	endpoint := base + "/v1/boards/" + url.PathEscape(board) + "/jobs/" + url.PathEscape(jobID) + "?questions=false"

	body, err := getBody(ctx, doer, a.Name(), endpoint, "application/json")
	if err != nil {
		return domain.JobDescription{}, err
	}

	var payload greenhouseJob
	if err := decodeJSON(a.Name(), endpoint, body, &payload); err != nil {
		return domain.JobDescription{}, err
	}

	jd := domain.JobDescription{
		RawText:   htmlFragmentToText(payload.Content),
		Title:     payload.Title,
		Location:  payload.locationName(),
		Company:   payload.CompanyName,
		SourceURL: endpoint,
	}
	return finish(jd, a.Name(), rawURL)
}

// greenhouseAPIBase picks the API origin for a posting host. Boards on
// Greenhouse's EU infrastructure carry ".eu." in the host and are served by a
// parallel API origin. An explicit WithBaseURL (tests, or a future proxy)
// always wins over the rewrite.
func greenhouseAPIBase(configured, host string) string {
	if configured == greenhouseDefaultBase && strings.Contains(strings.ToLower(host), ".eu.") {
		return greenhouseEUBase
	}
	return configured
}

// greenhouseIDs pulls the board token and numeric job id out of a posting URL.
func greenhouseIDs(u *url.URL) (board, jobID string, err error) {
	q := u.Query()
	// Embedded board: /embed/job_app?for=acme&token=4012345
	if board, jobID = q.Get("for"), q.Get("token"); board != "" && jobID != "" {
		return board, jobID, nil
	}
	if m := greenhousePath.FindStringSubmatch(u.Path); m != nil {
		if m[1] != "embed" {
			return m[1], m[2], nil
		}
	}
	// Some boards link as /{board}/jobs/{id} but pass the id as ?gh_jid=.
	if jid := q.Get("gh_jid"); jid != "" {
		if seg := firstPathSegment(u.Path); seg != "" && seg != "embed" {
			return seg, jid, nil
		}
	}
	return "", "", errURLShape("greenhouse posting url has no {board}/jobs/{id}")
}

// ---------------------------------------------------------------------------

type greenhouseJob struct {
	ID          int64  `json:"id"`
	Title       string `json:"title"`
	Content     string `json:"content"`
	AbsoluteURL string `json:"absolute_url"`
	CompanyName string `json:"company_name"`
	Location    struct {
		Name string `json:"name"`
	} `json:"location"`
	Offices []struct {
		Name string `json:"name"`
	} `json:"offices"`
	Departments []struct {
		Name string `json:"name"`
	} `json:"departments"`
}

func (j greenhouseJob) locationName() string {
	if j.Location.Name != "" {
		return j.Location.Name
	}
	names := make([]string, 0, len(j.Offices))
	for _, o := range j.Offices {
		if o.Name != "" {
			names = append(names, o.Name)
		}
	}
	return strings.Join(names, "; ")
}
