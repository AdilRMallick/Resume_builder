package adapters

import (
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

const (
	ashbyListedID   = "9a8b7c6d-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
	ashbyUnlistedID = "cafebabe-0000-4fff-9999-abcdefabcdef"
)

func TestAshbyDetect(t *testing.T) {
	cases := []struct {
		raw  string
		want bool
	}{
		{"https://jobs.ashbyhq.com/halcyon/" + ashbyListedID, true},
		{"https://api.ashbyhq.com/posting-api/job-board/halcyon", true},
		{"https://ashbyhq.com/", true},
		{"https://notashbyhq.com/halcyon/x", false},
		{"https://jobs.lever.co/northwind/x", false},
	}
	ab := NewAshby()
	for _, tc := range cases {
		u, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %s: %v", tc.raw, err)
		}
		if got := ab.Detect(u); got != tc.want {
			t.Errorf("Detect(%s) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestAshbyURLParsing(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantOrg string
		wantID  string
		wantErr bool
	}{
		{"posting", "https://jobs.ashbyhq.com/halcyon/" + ashbyListedID, "halcyon", ashbyListedID, false},
		{"application path", "https://jobs.ashbyhq.com/halcyon/" + ashbyListedID + "/application", "halcyon", ashbyListedID, false},
		{"embedded board", "https://jobs.ashbyhq.com/halcyon?jobPostingId=" + ashbyListedID, "halcyon", ashbyListedID, false},
		{"org only", "https://jobs.ashbyhq.com/halcyon", "", "", true},
		{"application without id", "https://jobs.ashbyhq.com/halcyon/application", "", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.Parse(tc.raw)
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			org, id, err := ashbyIDs(u)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got org=%q id=%q", org, id)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if org != tc.wantOrg || id != tc.wantID {
				t.Errorf("got org=%q id=%q, want org=%q id=%q", org, id, tc.wantOrg, tc.wantID)
			}
		})
	}
}

func TestAshbyFetchFromPostingAPI(t *testing.T) {
	body := fixture(t, "ashby_board.json")
	var gotURI string
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		gotURI = r.RequestURI
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})

	ab := NewAshby(WithBaseURL(srv.URL), WithAltBaseURL(srv.URL))
	jd, err := ab.Fetch(ctxT(t), doer, "https://jobs.ashbyhq.com/halcyon/"+ashbyListedID)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if want := "/posting-api/job-board/halcyon?includeCompensation=true"; gotURI != want {
		t.Errorf("requested %q, want %q", gotURI, want)
	}
	if jd.Adapter != "ashby" {
		t.Errorf("Adapter = %q, want ashby", jd.Adapter)
	}
	if jd.Title != "Software Engineer I, Platform" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Location != "Ann Arbor, MI; Remote - United States" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.Company != "Halcyon Labs" {
		t.Errorf("Company = %q", jd.Company)
	}
	assertContains(t, jd.RawText, []string{
		"About Halcyon Labs",
		"We build observability tooling for streaming data.",
		"- Graduating in 2027 with a degree in Computer Science or equivalent experience",
		"We are unable to sponsor work visas at this time.",
	})
	// The other posting on the same board must not leak in.
	assertNotContains(t, jd.RawText, []string{"Not the posting under test.", "<li>", ".x{color:red}"})
}

// TestAshbyGraphQLFallback covers link-only postings, which Ashby's public job
// board omits from the posting API response.
func TestAshbyGraphQLFallback(t *testing.T) {
	boardBody := fixture(t, "ashby_board.json")
	graphBody := fixture(t, "ashby_graphql.json")
	var seen []string
	var graphQLRequest map[string]any

	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.Path)
		switch r.URL.Path {
		case "/posting-api/job-board/halcyon":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(boardBody)
		case "/api/non-user-graphql":
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &graphQLRequest)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(graphBody)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})

	ab := NewAshby(WithBaseURL(srv.URL), WithAltBaseURL(srv.URL))
	jd, err := ab.Fetch(ctxT(t), doer, "https://jobs.ashbyhq.com/halcyon/"+ashbyUnlistedID)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if len(seen) != 2 || seen[0] != "GET /posting-api/job-board/halcyon" || seen[1] != "POST /api/non-user-graphql" {
		t.Fatalf("request sequence = %v, want board then graphql", seen)
	}
	vars, _ := graphQLRequest["variables"].(map[string]any)
	if vars["organizationHostedJobsPageName"] != "halcyon" || vars["jobPostingId"] != ashbyUnlistedID {
		t.Errorf("graphql variables = %v", vars)
	}
	if jd.Title != "Site Reliability Engineer, New Grad" {
		t.Errorf("Title = %q", jd.Title)
	}
	if jd.Location != "Remote - United States" {
		t.Errorf("Location = %q", jd.Location)
	}
	if jd.Company != "Halcyon Labs" {
		t.Errorf("Company = %q", jd.Company)
	}
	assertContains(t, jd.RawText, []string{
		"This posting is unlisted and reachable only by direct link.",
		"- Operate Kubernetes clusters across three regions",
		"- Terraform or another infrastructure-as-code tool",
	})
}

// TestAshbyDoesNotRetryOnRateLimit proves the second leg is reserved for
// missing postings. Firing another request at a rate limited vendor is the
// exact behaviour that gets an IP blocked.
func TestAshbyDoesNotRetryOnRateLimit(t *testing.T) {
	var seen int
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		seen++
		w.WriteHeader(http.StatusTooManyRequests)
	})

	ab := NewAshby(WithBaseURL(srv.URL), WithAltBaseURL(srv.URL))
	_, err := ab.Fetch(ctxT(t), doer, "https://jobs.ashbyhq.com/halcyon/"+ashbyListedID)
	assertFetchError(t, err, domain.ErrRateLimited, "ashby")
	if seen != 1 {
		t.Errorf("made %d requests, want 1", seen)
	}
}

func TestAshbyMissingEverywhereIsNotFound(t *testing.T) {
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/api/non-user-graphql" {
			_, _ = w.Write([]byte(`{"data":{"jobPosting":null}}`))
			return
		}
		_, _ = w.Write([]byte(`{"apiVersion":"1","jobs":[]}`))
	})

	ab := NewAshby(WithBaseURL(srv.URL), WithAltBaseURL(srv.URL))
	_, err := ab.Fetch(ctxT(t), doer, "https://jobs.ashbyhq.com/halcyon/"+ashbyListedID)
	assertFetchError(t, err, domain.ErrNotFound, "ashby")
}
