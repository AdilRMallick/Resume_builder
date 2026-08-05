package adapters

import (
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

func TestGreenhouseDetect(t *testing.T) {
	cases := []struct {
		raw  string
		want bool
	}{
		{"https://boards.greenhouse.io/acmerobotics/jobs/4012345", true},
		{"https://job-boards.greenhouse.io/acmerobotics/jobs/4012345", true},
		{"https://job-boards.eu.greenhouse.io/acmerobotics/jobs/4012345", true},
		{"https://boards.greenhouse.io/embed/job_app?for=acmerobotics&token=4012345", true},
		{"https://boards-api.greenhouse.io/v1/boards/acmerobotics/jobs/4012345", true},
		{"https://greenhouse.io/", true},
		// Near misses that must not be claimed.
		{"https://notgreenhouse.io/acme/jobs/1", false},
		{"https://jobs.lever.co/northwind/6f6b1f2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b", false},
		{"https://careers.acme.com/greenhouse.io/jobs/1", false},
	}
	gh := NewGreenhouse()
	for _, tc := range cases {
		u, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %s: %v", tc.raw, err)
		}
		if got := gh.Detect(u); got != tc.want {
			t.Errorf("Detect(%s) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestGreenhouseURLParsing(t *testing.T) {
	cases := []struct {
		name      string
		raw       string
		wantBoard string
		wantJob   string
		wantErr   bool
	}{
		{"legacy board", "https://boards.greenhouse.io/acmerobotics/jobs/4012345", "acmerobotics", "4012345", false},
		{"current board", "https://job-boards.greenhouse.io/acme-robotics/jobs/4012345", "acme-robotics", "4012345", false},
		{"trailing fragment", "https://boards.greenhouse.io/acmerobotics/jobs/4012345#app", "acmerobotics", "4012345", false},
		{"apply path", "https://boards.greenhouse.io/acmerobotics/jobs/4012345/apply", "acmerobotics", "4012345", false},
		{"embed", "https://boards.greenhouse.io/embed/job_app?for=acmerobotics&token=4012345", "acmerobotics", "4012345", false},
		{"api url", "https://boards-api.greenhouse.io/v1/boards/acmerobotics/jobs/4012345", "acmerobotics", "4012345", false},
		{"gh_jid query", "https://boards.greenhouse.io/acmerobotics?gh_jid=4012345", "acmerobotics", "4012345", false},
		{"board only", "https://boards.greenhouse.io/acmerobotics", "", "", true},
		{"non numeric job", "https://boards.greenhouse.io/acmerobotics/jobs/not-a-number", "", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.Parse(tc.raw)
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			board, job, err := greenhouseIDs(u)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got board=%q job=%q", board, job)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if board != tc.wantBoard || job != tc.wantJob {
				t.Errorf("got board=%q job=%q, want board=%q job=%q", board, job, tc.wantBoard, tc.wantJob)
			}
		})
	}
}

func TestGreenhouseFetch(t *testing.T) {
	var gotURI string
	srv, doer := serveJSON(t, "greenhouse_job.json", &gotURI)

	gh := NewGreenhouse(WithBaseURL(srv.URL))
	jd, err := gh.Fetch(ctxT(t), doer, "https://job-boards.greenhouse.io/acmerobotics/jobs/4012345")
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if want := "/v1/boards/acmerobotics/jobs/4012345?questions=false"; gotURI != want {
		t.Errorf("requested %q, want %q", gotURI, want)
	}
	if jd.Adapter != "greenhouse" {
		t.Errorf("Adapter = %q, want greenhouse", jd.Adapter)
	}
	if jd.Title != "Software Engineer, New Grad (2027)" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Location != "Detroit, MI" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.Company != "Acme Robotics" {
		t.Errorf("Company = %q", jd.Company)
	}

	assertContains(t, jd.RawText, []string{
		"Acme Robotics builds autonomy software for warehouse fleets.",
		"About the role",
		"- BS or MS in Computer Science, graduating between December 2026 and June 2027",
		"- Exposure to Docker & Kubernetes is a plus",
		"We sponsor H-1B and TN visas for this role.",
	})
	// The content field arrives entity escaped; if unescaping or tag stripping
	// regressed, markup or the inline script leaks into the corpus.
	assertNotContains(t, jd.RawText, []string{"<p>", "&lt;", "&amp;", "window.__gh", "<script"})
}

func TestGreenhouseAPIBase(t *testing.T) {
	cases := []struct {
		name       string
		configured string
		host       string
		want       string
	}{
		{"us default", greenhouseDefaultBase, "job-boards.greenhouse.io", greenhouseDefaultBase},
		{"eu board", greenhouseDefaultBase, "job-boards.eu.greenhouse.io", greenhouseEUBase},
		{"eu legacy board", greenhouseDefaultBase, "boards.eu.greenhouse.io", greenhouseEUBase},
		{"override wins", "http://127.0.0.1:9", "job-boards.eu.greenhouse.io", "http://127.0.0.1:9"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := greenhouseAPIBase(tc.configured, tc.host); got != tc.want {
				t.Errorf("greenhouseAPIBase(%q, %q) = %q, want %q", tc.configured, tc.host, got, tc.want)
			}
		})
	}
}

func TestGreenhouseBadURLIsPermanent(t *testing.T) {
	srv, doer := serveJSON(t, "greenhouse_job.json", nil)
	gh := NewGreenhouse(WithBaseURL(srv.URL))

	_, err := gh.Fetch(ctxT(t), doer, "https://boards.greenhouse.io/acmerobotics")
	assertFetchError(t, err, domain.ErrPermanent, "greenhouse")
}
