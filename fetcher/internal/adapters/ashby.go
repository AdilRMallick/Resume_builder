package adapters

import (
	"context"
	"errors"
	"net/url"
	"strings"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// AshbyAdapter resolves Ashby postings through Ashby's public JSON endpoints.
// No HTML scraping.
//
// UNVERIFIED ENDPOINT SHAPES. This machine has no outbound network, so neither
// endpoint below could be confirmed against live Ashby traffic. Both are
// implemented from the documented/observed shapes and both are covered by
// fixtures, but the field names are the thing to re-check first if Ashby
// coverage comes back at zero. Decoding is deliberately lenient: unknown fields
// are ignored, and either description field alone is enough to succeed.
//
// Primary endpoint, Ashby's documented public posting API:
//
//	GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
//	-> {"apiVersion":"1","jobs":[{"id","title","location","descriptionHtml",
//	    "descriptionPlain","department","team","isRemote",...}]}
//
// The board returns every listed job; the adapter selects the one whose id
// matches the posting URL. Jobs that are unlisted (link-only postings, common
// for new grad roles shared through a feed) are absent from that array, so on a
// miss the adapter falls back to the endpoint the hosted job board itself calls:
//
//	POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting
//	body {"operationName":"ApiJobPosting",
//	      "variables":{"organizationHostedJobsPageName":"{org}","jobPostingId":"{id}"},
//	      "query":"query ApiJobPosting(...) { jobPosting(...) { ... } }"}
//	-> {"data":{"jobPosting":{"title","descriptionHtml","locationName",...}}}
//
// Recognised posting URL shapes:
//
//	https://jobs.ashbyhq.com/{org}/{job_posting_id}
//	https://jobs.ashbyhq.com/{org}/{job_posting_id}/application
//	https://jobs.ashbyhq.com/{org}?jobPostingId={job_posting_id}   (embedded board)
type AshbyAdapter struct {
	settings
}

const (
	ashbyDefaultBase = "https://api.ashbyhq.com"
	ashbyGraphQLBase = "https://jobs.ashbyhq.com"
)

func NewAshby(opts ...Option) *AshbyAdapter {
	return &AshbyAdapter{settings: newSettings(ashbyDefaultBase, ashbyGraphQLBase, opts)}
}

func (a *AshbyAdapter) Name() string { return "ashby" }

func (a *AshbyAdapter) Detect(u *url.URL) bool {
	return hostMatches(u, "ashbyhq.com")
}

func (a *AshbyAdapter) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse url: %w", err)
	}
	org, postingID, err := ashbyIDs(u)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "%w", err)
	}

	jd, primaryErr := a.fromPostingAPI(ctx, doer, org, postingID)
	if primaryErr == nil {
		return finish(jd, a.Name(), rawURL)
	}
	// Only a missing or unusable board justifies a second request. A 429 or a
	// 5xx must surface as-is so the caller backs off instead of hammering a
	// second Ashby host.
	if !errors.Is(primaryErr, domain.ErrNotFound) && !errors.Is(primaryErr, domain.ErrPermanent) {
		return domain.JobDescription{}, primaryErr
	}

	jd, secondaryErr := a.fromGraphQL(ctx, doer, org, postingID)
	if secondaryErr != nil {
		// Report the primary failure; it describes the documented path.
		return domain.JobDescription{}, primaryErr
	}
	return finish(jd, a.Name(), rawURL)
}

// fromPostingAPI reads the public job board and picks out one posting.
func (a *AshbyAdapter) fromPostingAPI(ctx context.Context, doer Doer, org, postingID string) (domain.JobDescription, error) {
	endpoint := a.baseURL + "/posting-api/job-board/" + url.PathEscape(org) + "?includeCompensation=true"

	body, err := getBody(ctx, doer, a.Name(), endpoint, "application/json")
	if err != nil {
		return domain.JobDescription{}, err
	}

	var board ashbyBoard
	if err := decodeJSON(a.Name(), endpoint, body, &board); err != nil {
		return domain.JobDescription{}, err
	}
	for _, job := range board.Jobs {
		if !strings.EqualFold(job.ID, postingID) {
			continue
		}
		return domain.JobDescription{
			RawText:   preferPlain(job.DescriptionPlain, job.DescriptionHTML),
			Title:     job.Title,
			Location:  job.locationName(),
			Company:   board.company(org),
			SourceURL: endpoint,
		}, nil
	}
	return domain.JobDescription{}, fetchErr(domain.ErrNotFound, a.Name(), endpoint, 0,
		errors.New("posting id not present on the public job board"))
}

// fromGraphQL calls the endpoint the hosted Ashby job board uses for a single
// posting, which also returns unlisted postings.
func (a *AshbyAdapter) fromGraphQL(ctx context.Context, doer Doer, org, postingID string) (domain.JobDescription, error) {
	endpoint := a.altBaseURL + "/api/non-user-graphql?op=ApiJobPosting"

	payload := map[string]any{
		"operationName": "ApiJobPosting",
		"variables": map[string]any{
			"organizationHostedJobsPageName": org,
			"jobPostingId":                   postingID,
		},
		"query": ashbyPostingQuery,
	}
	body, err := postJSONBody(ctx, doer, a.Name(), endpoint, payload)
	if err != nil {
		return domain.JobDescription{}, err
	}

	var resp ashbyGraphQLResponse
	if err := decodeJSON(a.Name(), endpoint, body, &resp); err != nil {
		return domain.JobDescription{}, err
	}
	if len(resp.Errors) > 0 {
		return domain.JobDescription{}, fetchErr(domain.ErrPermanent, a.Name(), endpoint, 0,
			errors.New("graphql error: "+resp.Errors[0].Message))
	}
	p := resp.Data.JobPosting
	if p == nil {
		return domain.JobDescription{}, fetchErr(domain.ErrNotFound, a.Name(), endpoint, 0,
			errors.New("graphql returned a null jobPosting"))
	}
	return domain.JobDescription{
		RawText:   preferPlain(p.DescriptionPlain, p.DescriptionHTML),
		Title:     p.Title,
		Location:  p.LocationName,
		Company:   firstNonEmpty(p.Organization.Name, org),
		SourceURL: endpoint,
	}, nil
}

const ashbyPostingQuery = `query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id
    title
    descriptionHtml
    descriptionPlainText
    locationName
    employmentType
    departmentName
  }
}`

// ashbyIDs pulls the organisation slug and job posting id out of a posting URL.
func ashbyIDs(u *url.URL) (org, postingID string, err error) {
	segs := pathSegments(u.Path)
	if len(segs) == 0 {
		return "", "", errURLShape("ashby posting url has no organisation slug")
	}
	org = segs[0]

	if len(segs) >= 2 && segs[1] != "application" {
		return org, segs[1], nil
	}
	// Embedded boards keep the org in the path and the posting in the query.
	for _, key := range []string{"jobPostingId", "jobId", "posting"} {
		if v := u.Query().Get(key); v != "" {
			return org, v, nil
		}
	}
	return "", "", errURLShape("ashby posting url has no job posting id")
}

// ---------------------------------------------------------------------------

type ashbyBoard struct {
	APIVersion string `json:"apiVersion"`
	// Ashby has shipped the organisation name under more than one key; accept
	// either and fall back to the URL slug.
	OrganizationName string     `json:"organizationName"`
	Name             string     `json:"name"`
	Jobs             []ashbyJob `json:"jobs"`
}

func (b ashbyBoard) company(org string) string {
	return firstNonEmpty(b.OrganizationName, b.Name, org)
}

type ashbyJob struct {
	ID               string `json:"id"`
	Title            string `json:"title"`
	Location         string `json:"location"`
	DescriptionHTML  string `json:"descriptionHtml"`
	DescriptionPlain string `json:"descriptionPlain"`
	Department       string `json:"department"`
	Team             string `json:"team"`
	EmploymentType   string `json:"employmentType"`
	IsRemote         bool   `json:"isRemote"`
	IsListed         bool   `json:"isListed"`
	JobURL           string `json:"jobUrl"`
	SecondaryLocs    []struct {
		Location string `json:"location"`
	} `json:"secondaryLocations"`
}

func (j ashbyJob) locationName() string {
	parts := make([]string, 0, 1+len(j.SecondaryLocs))
	if j.Location != "" {
		parts = append(parts, j.Location)
	}
	for _, s := range j.SecondaryLocs {
		if s.Location != "" {
			parts = append(parts, s.Location)
		}
	}
	if len(parts) == 0 && j.IsRemote {
		return "Remote"
	}
	return strings.Join(parts, "; ")
}

type ashbyGraphQLResponse struct {
	Data struct {
		JobPosting *ashbyGraphQLPosting `json:"jobPosting"`
	} `json:"data"`
	Errors []struct {
		Message string `json:"message"`
	} `json:"errors"`
}

type ashbyGraphQLPosting struct {
	ID              string `json:"id"`
	Title           string `json:"title"`
	DescriptionHTML string `json:"descriptionHtml"`
	// Ashby's GraphQL schema names the plain variant descriptionPlainText while
	// the REST posting API names it descriptionPlain. Accept both.
	DescriptionPlain string `json:"descriptionPlainText"`
	LocationName     string `json:"locationName"`
	EmploymentType   string `json:"employmentType"`
	DepartmentName   string `json:"departmentName"`
	Organization     struct {
		Name string `json:"name"`
	} `json:"organization"`
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
