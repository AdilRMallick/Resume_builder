package adapters

import (
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

const leverUUID = "6f6b1f2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b"

func TestLeverDetect(t *testing.T) {
	cases := []struct {
		raw  string
		want bool
	}{
		{"https://jobs.lever.co/northwind/" + leverUUID, true},
		{"https://jobs.eu.lever.co/northwind/" + leverUUID, true},
		{"https://api.lever.co/v0/postings/northwind/" + leverUUID, true},
		{"https://lever.co/", true},
		{"https://notlever.co/northwind/x", false},
		{"https://boards.greenhouse.io/acme/jobs/1", false},
	}
	lv := NewLever()
	for _, tc := range cases {
		u, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %s: %v", tc.raw, err)
		}
		if got := lv.Detect(u); got != tc.want {
			t.Errorf("Detect(%s) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestLeverURLParsing(t *testing.T) {
	cases := []struct {
		name     string
		raw      string
		wantSite string
		wantID   string
		wantErr  bool
	}{
		{"posting", "https://jobs.lever.co/northwind/" + leverUUID, "northwind", leverUUID, false},
		{"apply path", "https://jobs.lever.co/northwind/" + leverUUID + "/apply", "northwind", leverUUID, false},
		{"eu host", "https://jobs.eu.lever.co/northwind/" + leverUUID, "northwind", leverUUID, false},
		{"api url", "https://api.lever.co/v0/postings/northwind/" + leverUUID, "northwind", leverUUID, false},
		{"query string", "https://jobs.lever.co/northwind/" + leverUUID + "?lever-source=Simplify", "northwind", leverUUID, false},
		{"site only", "https://jobs.lever.co/northwind", "", "", true},
		{"not a uuid", "https://jobs.lever.co/northwind/senior-engineer", "", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.Parse(tc.raw)
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			site, id, err := leverIDs(u)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got site=%q id=%q", site, id)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if site != tc.wantSite || id != tc.wantID {
				t.Errorf("got site=%q id=%q, want site=%q id=%q", site, id, tc.wantSite, tc.wantID)
			}
		})
	}
}

func TestLeverFetch(t *testing.T) {
	var gotURI string
	srv, doer := serveJSON(t, "lever_posting.json", &gotURI)

	lv := NewLever(WithBaseURL(srv.URL))
	jd, err := lv.Fetch(ctxT(t), doer, "https://jobs.lever.co/northwind/"+leverUUID)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if want := "/v0/postings/northwind/" + leverUUID + "?mode=json"; gotURI != want {
		t.Errorf("requested %q, want %q", gotURI, want)
	}
	if jd.Adapter != "lever" {
		t.Errorf("Adapter = %q, want lever", jd.Adapter)
	}
	if jd.Title != "Backend Engineer, University Grad" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Location != "Chicago, IL; Remote (US)" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.Company != "northwind" {
		t.Errorf("Company = %q", jd.Company)
	}

	assertContains(t, jd.RawText, []string{
		"Northwind runs the payments ledger for mid-market retailers.",
		// List blocks carry the requirement bullets and must survive with their
		// heading, in order.
		"What you’ll do",
		"- Design and ship services in Go and Python",
		"Requirements",
		"- Comfortable with SQL and at least one compiled language",
		"- Familiar with Docker & CI pipelines",
		"Northwind does not provide visa sponsorship for this role.",
	})
	assertNotContains(t, jd.RawText, []string{"<li>", "<b>", "&amp;", "&rsquo;", "track()"})
}

// TestLeverFallsBackToHTMLDescription covers postings where Lever omits the
// pre-flattened plain text fields.
func TestLeverFallsBackToHTMLDescription(t *testing.T) {
	body := `{
	  "id": "` + leverUUID + `",
	  "text": "Platform Engineer",
	  "categories": {"location": "Remote"},
	  "description": "<div><p>We run a large Kubernetes estate.</p><ul><li>Own the deploy pipeline</li></ul></div>"
	}`
	srv, doer := serveBody(t, "application/json", body)

	jd, err := NewLever(WithBaseURL(srv.URL)).Fetch(ctxT(t), doer, "https://jobs.lever.co/northwind/"+leverUUID)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	assertContains(t, jd.RawText, []string{"We run a large Kubernetes estate.", "- Own the deploy pipeline"})
	if jd.Location != "Remote" {
		t.Errorf("Location = %q, want Remote", jd.Location)
	}
}

func TestLeverEmptyDescriptionIsPermanent(t *testing.T) {
	srv, doer := serveBody(t, "application/json", `{"id":"`+leverUUID+`","text":"Ghost Role"}`)

	_, err := NewLever(WithBaseURL(srv.URL)).Fetch(ctxT(t), doer, "https://jobs.lever.co/northwind/"+leverUUID)
	assertFetchError(t, err, domain.ErrPermanent, "lever")
}
