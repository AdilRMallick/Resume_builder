package adapters

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"testing"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

func TestRegistryDispatchAndFallback(t *testing.T) {
	reg := NewRegistry()

	cases := []struct {
		name string
		raw  string
		want string
	}{
		{"greenhouse legacy", "https://boards.greenhouse.io/acmerobotics/jobs/4012345", "greenhouse"},
		{"greenhouse current", "https://job-boards.greenhouse.io/acmerobotics/jobs/4012345", "greenhouse"},
		{"greenhouse eu", "https://job-boards.eu.greenhouse.io/acmerobotics/jobs/4012345", "greenhouse"},
		{"lever", "https://jobs.lever.co/northwind/" + leverUUID, "lever"},
		{"lever eu", "https://jobs.eu.lever.co/northwind/" + leverUUID, "lever"},
		{"ashby", "https://jobs.ashbyhq.com/halcyon/" + ashbyListedID, "ashby"},
		{"smartrecruiters", "https://jobs.smartrecruiters.com/Acme/744000123456789-software-engineer", "smartrecruiters"},
		// Everything unrecognised must land on the fallback rather than error.
		{"company career page", "https://careers.vectorfreight.com/jobs/junior-platform-engineer", "fallback"},
		{"smartrecruiters careers page", "https://careers.smartrecruiters.com/Acme", "fallback"},
		{"workday", "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Detroit/SWE_R-1", "fallback"},
		{"lookalike host", "https://notgreenhouse.io/acme/jobs/1", "fallback"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, err := reg.For(tc.raw)
			if err != nil {
				t.Fatalf("For(%s): %v", tc.raw, err)
			}
			if a.Name() != tc.want {
				t.Errorf("For(%s) = %s, want %s", tc.raw, a.Name(), tc.want)
			}
		})
	}
}

func TestRegistryRejectsUnusableURLs(t *testing.T) {
	reg := NewRegistry()
	for _, raw := range []string{
		"ftp://files.acme.com/jobs.txt",
		"mailto:jobs@acme.com",
		"://not a url",
	} {
		if a, err := reg.For(raw); err == nil {
			t.Errorf("For(%q) = %s, want error", raw, a.Name())
		} else if !errors.Is(err, domain.ErrPermanent) {
			t.Errorf("For(%q) error = %v, want ErrPermanent", raw, err)
		}
	}
}

// stubAdapter exists to prove the "adding an ATS touches only the registry"
// claim: a brand new adapter is dispatched with no other change to the package.
type stubAdapter struct {
	name string
	host string
}

func (s stubAdapter) Name() string { return s.name }

func (s stubAdapter) Detect(u *url.URL) bool { return hostMatches(u, s.host) }

func (s stubAdapter) Fetch(_ context.Context, _ Doer, rawURL string) (domain.JobDescription, error) {
	return domain.JobDescription{RawText: "stub for " + rawURL, Adapter: s.name}, nil
}

func TestRegistryIsExtendedByListOnly(t *testing.T) {
	reg := NewRegistryWith(NewFallback(), NewGreenhouse(), stubAdapter{name: "smartrecruiters", host: "smartrecruiters.com"})

	a, err := reg.For("https://jobs.smartrecruiters.com/Acme/743999")
	if err != nil {
		t.Fatalf("For: %v", err)
	}
	if a.Name() != "smartrecruiters" {
		t.Fatalf("For = %s, want smartrecruiters", a.Name())
	}
	if got := reg.Names(); len(got) != 3 || got[2] != "fallback" {
		t.Errorf("Names() = %v, want the fallback last", got)
	}

	// Hosts the new adapter does not claim still reach the fallback.
	if a, err := reg.For("https://careers.acme.com/jobs/1"); err != nil || a.Name() != "fallback" {
		t.Errorf("For(careers.acme.com) = %v, %v; want fallback", a, err)
	}
}

func TestRegistryFetchRoutesToAdapter(t *testing.T) {
	body := fixture(t, "greenhouse_job.json")
	srv, doer := serve(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})

	// WithBaseURL is applied to every API adapter the registry builds, so the
	// whole registry can be exercised against one fixture server.
	reg := NewRegistry(WithBaseURL(srv.URL), WithAltBaseURL(srv.URL))
	jd, err := reg.Fetch(ctxT(t), doer, "https://boards.greenhouse.io/acmerobotics/jobs/4012345")
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if jd.Adapter != "greenhouse" {
		t.Errorf("Adapter = %q, want greenhouse", jd.Adapter)
	}
	if jd.Title != "Software Engineer, New Grad (2027)" {
		t.Errorf("Title = %q", jd.Title)
	}
}

func TestRegistryNamesCoversEveryAdapter(t *testing.T) {
	got := NewRegistry().Names()
	want := []string{"greenhouse", "lever", "ashby", "smartrecruiters", "fallback"}
	if len(got) != len(want) {
		t.Fatalf("Names() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Names() = %v, want %v", got, want)
		}
	}
}
