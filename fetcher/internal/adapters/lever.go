package adapters

import (
	"context"
	"net/url"
	"regexp"
	"strings"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// LeverAdapter resolves Lever postings through the public v0 postings API. No
// HTML scraping.
//
// Endpoint:
//
//	GET https://api.lever.co/v0/postings/{site}/{posting_id}?mode=json
//
// Recognised posting URL shapes:
//
//	https://jobs.lever.co/{site}/{posting_id}
//	https://jobs.lever.co/{site}/{posting_id}/apply
//	https://jobs.eu.lever.co/{site}/{posting_id}          (EU data residency)
//	https://api.lever.co/v0/postings/{site}/{posting_id}
//
// The posting id is a UUID. Lever returns both HTML (`description`, `lists`,
// `additional`) and pre-flattened plain text (`descriptionPlain`,
// `additionalPlain`); the plain fields are preferred where present, and the
// `lists` blocks are always rendered from HTML because Lever has no plain
// equivalent for them and they hold the actual requirement bullets.
type LeverAdapter struct {
	settings
}

const leverDefaultBase = "https://api.lever.co"

func NewLever(opts ...Option) *LeverAdapter {
	return &LeverAdapter{settings: newSettings(leverDefaultBase, "", opts)}
}

func (a *LeverAdapter) Name() string { return "lever" }

func (a *LeverAdapter) Detect(u *url.URL) bool {
	return hostMatches(u, "lever.co")
}

// leverPostingID matches a UUID, the only shape a Lever posting id takes.
var leverPostingID = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func (a *LeverAdapter) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse url: %w", err)
	}
	site, postingID, err := leverIDs(u)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "%w", err)
	}

	endpoint := a.baseURL + "/v0/postings/" + url.PathEscape(site) + "/" + url.PathEscape(postingID) + "?mode=json"

	body, err := getBody(ctx, doer, a.Name(), endpoint, "application/json")
	if err != nil {
		return domain.JobDescription{}, err
	}

	var p leverPosting
	if err := decodeJSON(a.Name(), endpoint, body, &p); err != nil {
		return domain.JobDescription{}, err
	}

	jd := domain.JobDescription{
		RawText:   p.text(),
		Title:     p.Text,
		Location:  p.location(),
		Company:   site,
		SourceURL: endpoint,
	}
	return finish(jd, a.Name(), rawURL)
}

// leverIDs pulls the site slug and posting id out of a Lever URL.
func leverIDs(u *url.URL) (site, postingID string, err error) {
	segs := pathSegments(u.Path)
	// api.lever.co/v0/postings/{site}/{id} -> drop the API prefix.
	if len(segs) >= 4 && segs[0] == "v0" && segs[1] == "postings" {
		segs = segs[2:]
	}
	if len(segs) >= 2 && leverPostingID.MatchString(segs[1]) {
		return segs[0], segs[1], nil
	}
	return "", "", errURLShape("lever posting url has no {site}/{uuid}")
}

// ---------------------------------------------------------------------------

type leverPosting struct {
	ID               string `json:"id"`
	Text             string `json:"text"` // the job title
	HostedURL        string `json:"hostedUrl"`
	Description      string `json:"description"`
	DescriptionPlain string `json:"descriptionPlain"`
	Additional       string `json:"additional"`
	AdditionalPlain  string `json:"additionalPlain"`
	WorkplaceType    string `json:"workplaceType"`
	Country          string `json:"country"`
	Categories       struct {
		Commitment   string   `json:"commitment"`
		Department   string   `json:"department"`
		Location     string   `json:"location"`
		Team         string   `json:"team"`
		AllLocations []string `json:"allLocations"`
	} `json:"categories"`
	Lists []struct {
		Text    string `json:"text"`
		Content string `json:"content"`
	} `json:"lists"`
}

// text assembles the full posting body in reading order: intro, then each
// titled list block, then the closing section.
func (p leverPosting) text() string {
	var parts []string
	add := func(s string) {
		if s = strings.TrimSpace(s); s != "" {
			parts = append(parts, s)
		}
	}

	add(preferPlain(p.DescriptionPlain, p.Description))
	for _, l := range p.Lists {
		add(collapseInline(l.Text))
		add(htmlFragmentToText(l.Content))
	}
	add(preferPlain(p.AdditionalPlain, p.Additional))

	return strings.Join(parts, "\n\n")
}

func (p leverPosting) location() string {
	if len(p.Categories.AllLocations) > 0 {
		return strings.Join(p.Categories.AllLocations, "; ")
	}
	return p.Categories.Location
}

// preferPlain uses the API's own plain text when it is present and non-trivial,
// otherwise flattens the HTML twin.
func preferPlain(plain, markup string) string {
	if strings.TrimSpace(plain) != "" {
		return collapse(plain)
	}
	return htmlFragmentToText(markup)
}
