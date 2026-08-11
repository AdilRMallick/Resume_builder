package adapters

import (
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

const smartRecruitersID = "744000123456789"

func TestSmartRecruitersDetect(t *testing.T) {
	cases := []struct {
		raw  string
		want bool
	}{
		{"https://jobs.smartrecruiters.com/NorthwindLabs/" + smartRecruitersID + "-software-engineer", true},
		{"https://jobs.eu.smartrecruiters.com/NorthwindLabs/" + smartRecruitersID + "-software-engineer", true},
		{"https://www.smartrecruiters.com/NorthwindLabs/" + smartRecruitersID + "-software-engineer", true},
		{"https://jobs.smartrecruiters.com/oneclick-ui/company/NorthwindLabs/publication/5f4727d2-0078-4099-b072-a81dba76a99e", true},
		{"https://api.smartrecruiters.com/v1/companies/NorthwindLabs/postings/" + smartRecruitersID, true},
		// A company careers page is not one posting. Leave it to fallback
		// instead of claiming it and returning a permanent URL-shape error.
		{"https://careers.smartrecruiters.com/NorthwindLabs", false},
		{"https://notsmartrecruiters.com/NorthwindLabs/123-title", false},
		{"https://jobs.lever.co/northwind/x", false},
	}
	adapter := NewSmartRecruiters()
	for _, tc := range cases {
		u, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %s: %v", tc.raw, err)
		}
		if got := adapter.Detect(u); got != tc.want {
			t.Errorf("Detect(%s) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestSmartRecruitersURLParsing(t *testing.T) {
	cases := []struct {
		name        string
		raw         string
		wantCompany string
		wantID      string
		wantErr     bool
	}{
		{"jobs page", "https://jobs.smartrecruiters.com/NorthwindLabs/" + smartRecruitersID + "-software-engineer", "NorthwindLabs", smartRecruitersID, false},
		{"www page", "https://www.smartrecruiters.com/QADInc/744000044936095-software-engineer", "QADInc", "744000044936095", false},
		{"query ignored", "https://jobs.smartrecruiters.com/NorthwindLabs/" + smartRecruitersID + "-software-engineer?trid=abc", "NorthwindLabs", smartRecruitersID, false},
		{"api v1", "https://api.smartrecruiters.com/v1/companies/NorthwindLabs/postings/" + smartRecruitersID, "NorthwindLabs", smartRecruitersID, false},
		{"api uuid", "https://api.smartrecruiters.com/v1/companies/NorthwindLabs/postings/5f4727d2-0078-4099-b072-a81dba76a99e", "NorthwindLabs", "5f4727d2-0078-4099-b072-a81dba76a99e", false},
		{"legacy api", "https://api.smartrecruiters.com/v1/NorthwindLabs/postings/" + smartRecruitersID, "NorthwindLabs", smartRecruitersID, false},
		{"oneclick publication", "https://jobs.smartrecruiters.com/oneclick-ui/company/NorthwindLabs/publication/5f4727d2-0078-4099-b072-a81dba76a99e?dcr_ci=NorthwindLabs", "NorthwindLabs", "5f4727d2-0078-4099-b072-a81dba76a99e", false},
		{"oneclick job", "https://jobs.smartrecruiters.com/oneclick-ui/company/NorthwindLabs/job/714887450", "NorthwindLabs", "714887450", false},
		{"external referral", "https://jobs.smartrecruiters.com/external-referrals/company/NorthwindLabs/publication/5f4727d2-0078-4099-b072-a81dba76a99e", "NorthwindLabs", "5f4727d2-0078-4099-b072-a81dba76a99e", false},
		{"company only", "https://jobs.smartrecruiters.com/NorthwindLabs", "", "", true},
		{"no numeric id", "https://jobs.smartrecruiters.com/NorthwindLabs/software-engineer", "", "", true},
		{"api missing company", "https://api.smartrecruiters.com/v1/postings/" + smartRecruitersID, "", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.Parse(tc.raw)
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			company, id, err := smartRecruitersIDs(u)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got company=%q id=%q", company, id)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if company != tc.wantCompany || id != tc.wantID {
				t.Errorf("got company=%q id=%q, want company=%q id=%q", company, id, tc.wantCompany, tc.wantID)
			}
		})
	}
}

func TestSmartRecruitersFetch(t *testing.T) {
	var gotURI string
	srv, doer := serveJSON(t, "smartrecruiters_posting.json", &gotURI)

	adapter := NewSmartRecruiters(WithBaseURL(srv.URL))
	jd, err := adapter.Fetch(
		ctxT(t),
		doer,
		"https://jobs.smartrecruiters.com/NorthwindLabs/"+smartRecruitersID+"-software-engineer-university-graduate",
	)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if want := "/v1/companies/NorthwindLabs/postings/" + smartRecruitersID; gotURI != want {
		t.Errorf("requested %q, want %q", gotURI, want)
	}
	if jd.Adapter != "smartrecruiters" {
		t.Errorf("Adapter = %q, want smartrecruiters", jd.Adapter)
	}
	if jd.Title != "Software Engineer, University Graduate" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Company != "Northwind Labs" {
		t.Errorf("Company = %q", jd.Company)
	}
	if jd.Location != "Remote; Detroit, MI, US" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.SourceURL != srv.URL+"/v1/companies/NorthwindLabs/postings/"+smartRecruitersID {
		t.Errorf("SourceURL = %q", jd.SourceURL)
	}

	assertContains(t, jd.RawText, []string{
		"Company Description\nNorthwind builds software for regional logistics networks.",
		"Job Description\nBuild and operate Go services that process shipment events.",
		"Qualifications\n- Experience with Go or Python",
		"- Working knowledge of PostgreSQL",
		"Additional Information\nThis role offers visa sponsorship.",
	})
	assertNotContains(t, jd.RawText, []string{"<p>", "&lt;", "track()"})
}

func TestSmartRecruitersSkipsEmptySections(t *testing.T) {
	body := `{
	  "name": "Platform Engineer",
	  "company": {"name": "Northwind Labs"},
	  "location": {"remote": true},
	  "jobAd": {"sections": {
	    "companyDescription": {"title": "Company Description", "text": ""},
	    "jobDescription": {"title": "", "text": "Own the production platform."}
	  }}
	}`
	srv, doer := serveBody(t, "application/json", body)

	jd, err := NewSmartRecruiters(WithBaseURL(srv.URL)).Fetch(
		ctxT(t), doer, "https://jobs.smartrecruiters.com/NorthwindLabs/"+smartRecruitersID+"-platform-engineer",
	)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if jd.RawText != "Own the production platform." {
		t.Errorf("RawText = %q", jd.RawText)
	}
	if jd.Location != "Remote" {
		t.Errorf("Location = %q, want Remote", jd.Location)
	}
}

func TestSmartRecruitersEmptyDescriptionIsPermanent(t *testing.T) {
	srv, doer := serveBody(t, "application/json", `{"name":"Ghost Role","jobAd":{"sections":{}}}`)

	_, err := NewSmartRecruiters(WithBaseURL(srv.URL)).Fetch(
		ctxT(t), doer, "https://jobs.smartrecruiters.com/NorthwindLabs/"+smartRecruitersID+"-ghost-role",
	)
	assertFetchError(t, err, domain.ErrPermanent, "smartrecruiters")
}
