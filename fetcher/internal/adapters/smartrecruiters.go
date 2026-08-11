package adapters

import (
	"context"
	"net/url"
	"regexp"
	"strings"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// SmartRecruitersAdapter resolves public job pages through SmartRecruiters'
// Posting API. The detail endpoint is documented as:
//
//	GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{postingId}
//
// The public page puts both identifiers in its path. The API response keeps the
// description split into named sections; preserving those headings improves
// the downstream requirement extractor's ability to distinguish requirements
// from general company copy.
type SmartRecruitersAdapter struct {
	settings
}

const smartRecruitersDefaultBase = "https://api.smartrecruiters.com"

func NewSmartRecruiters(opts ...Option) *SmartRecruitersAdapter {
	return &SmartRecruitersAdapter{settings: newSettings(smartRecruitersDefaultBase, "", opts)}
}

func (a *SmartRecruitersAdapter) Name() string { return "smartrecruiters" }

func (a *SmartRecruitersAdapter) Detect(u *url.URL) bool {
	if u == nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	return host == "www.smartrecruiters.com" ||
		host == "api.smartrecruiters.com" ||
		(strings.HasPrefix(host, "jobs.") && strings.HasSuffix(host, ".smartrecruiters.com"))
}

func (a *SmartRecruitersAdapter) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse url: %w", err)
	}
	company, postingID, err := smartRecruitersIDs(u)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "%w", err)
	}

	endpoint := a.baseURL + "/v1/companies/" + url.PathEscape(company) +
		"/postings/" + url.PathEscape(postingID)
	body, err := getBody(ctx, doer, a.Name(), endpoint, "application/json")
	if err != nil {
		return domain.JobDescription{}, err
	}

	var posting smartRecruitersPosting
	if err := decodeJSON(a.Name(), endpoint, body, &posting); err != nil {
		return domain.JobDescription{}, err
	}

	jd := domain.JobDescription{
		RawText:   posting.text(),
		Title:     posting.Name,
		Location:  posting.Location.String(),
		Company:   posting.Company.Name,
		SourceURL: endpoint,
	}
	return finish(jd, a.Name(), rawURL)
}

var (
	smartRecruitersNumericID = regexp.MustCompile(`^(\d+)(?:-|$)`)
	smartRecruitersUUID      = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// smartRecruitersIDs accepts both public posting pages and API detail URLs:
//
//	https://jobs.smartrecruiters.com/{company}/{id}-{title-slug}
//	https://www.smartrecruiters.com/{company}/{id}-{title-slug}
//	https://jobs.smartrecruiters.com/oneclick-ui/company/{company}/publication/{uuid}
//	https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}
func smartRecruitersIDs(u *url.URL) (company, postingID string, err error) {
	segs := pathSegments(u.Path)
	if strings.EqualFold(u.Hostname(), "api.smartrecruiters.com") {
		for i := 0; i+3 < len(segs); i++ {
			if segs[i] == "companies" && segs[i+2] == "postings" {
				if segs[i+1] != "" && segs[i+3] != "" {
					return segs[i+1], segs[i+3], nil
				}
			}
		}
		// The older guide used /v1/{company}/postings/{id}; accept links
		// copied from it but always fetch through the current canonical path.
		if len(segs) >= 4 && segs[0] == "v1" && segs[2] == "postings" {
			return segs[1], segs[3], nil
		}
		return "", "", errURLShape("smartrecruiters api url has no companies/{company}/postings/{id}")
	}
	// Current pages use a oneclick route keyed by publication UUID. Referral
	// links use the same company/.../publication/... tail with another prefix.
	for i := 0; i+3 < len(segs); i++ {
		if segs[i] == "company" && (segs[i+2] == "publication" || segs[i+2] == "job") {
			id := segs[i+3]
			if smartRecruitersUUID.MatchString(id) {
				return segs[i+1], id, nil
			}
			if match := smartRecruitersNumericID.FindStringSubmatch(id); match != nil {
				return segs[i+1], match[1], nil
			}
		}
	}
	if len(segs) >= 2 {
		if match := smartRecruitersNumericID.FindStringSubmatch(segs[1]); match != nil {
			return segs[0], match[1], nil
		}
	}
	return "", "", errURLShape("smartrecruiters posting url has no {company}/{numeric-id}-{slug}")
}

type smartRecruitersPosting struct {
	Name    string `json:"name"`
	Company struct {
		Name string `json:"name"`
	} `json:"company"`
	Location smartRecruitersLocation `json:"location"`
	JobAd    struct {
		Sections struct {
			CompanyDescription    smartRecruitersSection `json:"companyDescription"`
			JobDescription        smartRecruitersSection `json:"jobDescription"`
			Qualifications        smartRecruitersSection `json:"qualifications"`
			AdditionalInformation smartRecruitersSection `json:"additionalInformation"`
		} `json:"sections"`
	} `json:"jobAd"`
}

type smartRecruitersSection struct {
	Title string `json:"title"`
	Text  string `json:"text"`
}

func (p smartRecruitersPosting) text() string {
	sections := []smartRecruitersSection{
		p.JobAd.Sections.CompanyDescription,
		p.JobAd.Sections.JobDescription,
		p.JobAd.Sections.Qualifications,
		p.JobAd.Sections.AdditionalInformation,
	}
	var out []string
	for _, section := range sections {
		text := htmlFragmentToText(section.Text)
		if text == "" {
			continue
		}
		if title := collapseInline(section.Title); title != "" {
			out = append(out, title+"\n"+text)
		} else {
			out = append(out, text)
		}
	}
	return strings.Join(out, "\n\n")
}

type smartRecruitersLocation struct {
	City    string `json:"city"`
	Region  string `json:"region"`
	Country string `json:"country"`
	Remote  bool   `json:"remote"`
}

func (l smartRecruitersLocation) String() string {
	parts := make([]string, 0, 3)
	for _, part := range []string{l.City, l.Region, strings.ToUpper(l.Country)} {
		if part != "" {
			parts = append(parts, part)
		}
	}
	place := strings.Join(parts, ", ")
	if l.Remote && place != "" {
		return "Remote; " + place
	}
	if l.Remote {
		return "Remote"
	}
	return place
}
