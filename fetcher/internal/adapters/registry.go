package adapters

import (
	"context"
	"net/url"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// Registry picks the adapter for a URL and runs it.
//
// Adding an ATS is a one-line change to the slice in NewRegistry and a new file
// implementing Adapter. Nothing else in the fetcher knows adapters by name; the
// worker asks the registry and records whatever Name() comes back, so the
// coverage metric picks up a new adapter with no further wiring.
type Registry struct {
	adapters []Adapter
	fallback Adapter
}

// NewRegistry returns the production registry. Order matters only if two
// adapters could claim the same host, which none currently do.
func NewRegistry(opts ...Option) *Registry {
	return &Registry{
		adapters: []Adapter{
			NewGreenhouse(opts...),
			NewLever(opts...),
			NewAshby(opts...),
			// Add new ATS adapters here. Nothing else needs to change.
		},
		fallback: NewFallback(),
	}
}

// NewRegistryWith builds a registry from an explicit adapter list. Tests use it;
// so would a config-driven build that disables an adapter.
func NewRegistryWith(fallback Adapter, adapters ...Adapter) *Registry {
	return &Registry{adapters: adapters, fallback: fallback}
}

// For returns the adapter that handles rawURL. A URL that parses but matches no
// dedicated adapter gets the fallback, so this only errors on an unparseable or
// non-HTTP URL.
func (r *Registry) For(rawURL string) (Adapter, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, permanentf("registry", rawURL, "parse url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, permanentf("registry", rawURL, "unsupported scheme %q", u.Scheme)
	}
	for _, a := range r.adapters {
		if a.Detect(u) {
			return a, nil
		}
	}
	if r.fallback == nil {
		return nil, permanentf("registry", rawURL, "no adapter for host %q and no fallback", u.Host)
	}
	return r.fallback, nil
}

// Fetch resolves a posting URL with the matching adapter.
func (r *Registry) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	a, err := r.For(rawURL)
	if err != nil {
		return domain.JobDescription{}, err
	}
	return a.Fetch(ctx, doer, rawURL)
}

// Names lists every adapter name, fallback last. The metrics layer uses it to
// emit a zero for adapters that resolved nothing this run, so a newly broken
// adapter shows up as a zero rather than as a missing series.
func (r *Registry) Names() []string {
	names := make([]string, 0, len(r.adapters)+1)
	for _, a := range r.adapters {
		names = append(names, a.Name())
	}
	if r.fallback != nil {
		names = append(names, r.fallback.Name())
	}
	return names
}
