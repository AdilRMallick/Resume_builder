package adapters

import (
	"net/http"
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

func TestFallbackDetectAlwaysTrue(t *testing.T) {
	fb := NewFallback()
	for _, raw := range []string{
		"https://careers.vectorfreight.com/jobs/junior-platform-engineer",
		"https://boards.greenhouse.io/acme/jobs/1",
		"http://example.test/",
	} {
		u, err := url.Parse(raw)
		if err != nil {
			t.Fatalf("parse %s: %v", raw, err)
		}
		if !fb.Detect(u) {
			t.Errorf("Detect(%s) = false, want true (fallback is the terminal case)", raw)
		}
	}
}

func TestFallbackExtractsMainContent(t *testing.T) {
	page := fixture(t, "fallback_career_page.html")
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(page)
	})

	jd, err := NewFallback().Fetch(ctxT(t), doer, srv.URL+"/jobs/junior-platform-engineer")
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if jd.Adapter != "fallback" {
		t.Errorf("Adapter = %q, want fallback", jd.Adapter)
	}
	if jd.Title != "Junior Platform Engineer" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Company != "Vector Freight" {
		t.Errorf("Company = %q", jd.Company)
	}
	if jd.Location != "Chicago, IL" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.SourceURL != srv.URL+"/jobs/junior-platform-engineer" {
		t.Errorf("SourceURL = %q", jd.SourceURL)
	}

	assertContains(t, jd.RawText, []string{
		"Vector Freight moves twelve thousand loads a day",
		"- Backend services in Go, deployed to Kubernetes on AWS",
		"- Comfort writing SQL against a relational database",
		"We sponsor H-1B and TN visas for this role.",
	})
	// Chrome, scripts, and the link-heavy rails must be gone. If any of these
	// survive, every downstream requirement extraction inherits the noise.
	assertNotContains(t, jd.RawText, []string{
		"console.log", "dataLayer", "display:flex",
		"All openings", "Life at Vector", // nav
		"Related roles", "Senior Platform Engineer", // aside
		"Privacy", // footer
		"<p>", "<div", "&copy;",
	})
}

// TestFallbackRejectsClientRenderedShell: a JS shell has no description. Storing
// its chrome as a job description is worse than failing, because the coverage
// metric would count it as a success.
func TestFallbackRejectsClientRenderedShell(t *testing.T) {
	page := fixture(t, "fallback_spa_shell.html")
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(page)
	})

	_, err := NewFallback().Fetch(ctxT(t), doer, srv.URL+"/careers/1")
	assertFetchError(t, err, domain.ErrPermanent, "fallback")
}

func TestFallbackRejectsNonHTTPScheme(t *testing.T) {
	_, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {})
	_, err := NewFallback().Fetch(ctxT(t), doer, "file:///etc/passwd")
	assertFetchError(t, err, domain.ErrPermanent, "fallback")
}

func TestTrimTitleChrome(t *testing.T) {
	cases := []struct{ in, want string }{
		{"Junior Platform Engineer | Vector Freight Careers", "Junior Platform Engineer"},
		{"Data Engineer - Acme", "Data Engineer"},
		{"Site Reliability Engineer — Halcyon Labs", "Site Reliability Engineer"},
		{"Software Engineer", "Software Engineer"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := trimTitleChrome(tc.in); got != tc.want {
			t.Errorf("trimTitleChrome(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
