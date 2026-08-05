package adapters

import (
	"context"
	"errors"
	"net/url"
	"strings"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"

	"github.com/adilmallick/job-match-engine/fetcher/internal/domain"
)

// FallbackAdapter handles every host no dedicated adapter claims: company career
// pages, SmartRecruiters, Workday's server-rendered variants, one-off bespoke
// boards. It fetches the URL and runs a readability-style main content
// extraction (see extractMainText in html.go).
//
// Its output is lower quality than an API adapter's by construction, which is
// exactly why adapter coverage is tracked per adapter: the fallback's share of
// resolutions is the number that says how much of the corpus is only
// approximately understood.
//
// Client-rendered pages (Workday, most React boards) yield a near-empty body.
// The adapter returns ErrPermanent rather than a stub, because a posting with a
// cookie banner for a description is worse than a posting with none: it would
// count as covered and produce a confident, wrong match.
type FallbackAdapter struct {
	// minTextLen is the shortest extraction treated as a real job description.
	minTextLen int
}

// defaultMinTextLen is deliberately low. Short postings exist; the point is only
// to reject navigation chrome and JS shells.
const defaultMinTextLen = 200

func NewFallback() *FallbackAdapter {
	return &FallbackAdapter{minTextLen: defaultMinTextLen}
}

func (a *FallbackAdapter) Name() string { return "fallback" }

// Detect always returns true: the fallback is the registry's terminal case and
// is never consulted by host.
func (a *FallbackAdapter) Detect(*url.URL) bool { return true }

func (a *FallbackAdapter) Fetch(ctx context.Context, doer Doer, rawURL string) (domain.JobDescription, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "unsupported scheme %q", u.Scheme)
	}

	body, err := getBody(ctx, doer, a.Name(), rawURL, "text/html,application/xhtml+xml")
	if err != nil {
		return domain.JobDescription{}, err
	}

	doc, err := html.Parse(strings.NewReader(string(body)))
	if err != nil {
		return domain.JobDescription{}, permanentf(a.Name(), rawURL, "parse html: %w", err)
	}

	// Metadata first: og: tags and <h1> often live inside <header>, which the
	// extraction pass prunes away.
	jd := domain.JobDescription{
		Title:     fallbackTitle(doc),
		Company:   fallbackCompany(doc, u),
		Location:  metaContent(doc, "job:location", "og:locality", "geo.placename"),
		SourceURL: rawURL,
	}

	jd.RawText = extractMainText(doc)
	if runeLen(jd.RawText) < a.minTextLen {
		return domain.JobDescription{}, fetchErr(domain.ErrPermanent, a.Name(), rawURL, 0,
			errors.New("extracted content too short; page is likely client rendered"))
	}
	return finish(jd, a.Name(), rawURL)
}

// fallbackTitle prefers structured metadata, then the first heading, then the
// document title with any trailing " | Company" chrome removed.
func fallbackTitle(doc *html.Node) string {
	if t := metaContent(doc, "og:title", "twitter:title"); t != "" {
		return collapseInline(t)
	}
	if h := textOfFirst(doc, atom.H1); h != "" {
		return h
	}
	return trimTitleChrome(textOfFirst(doc, atom.Title))
}

// fallbackCompany prefers og:site_name, then the registrable-looking part of
// the host. Host derived names are rough ("careers.acme.com" -> "acme"), but the
// posting row already carries the feed's company name; this is a backstop.
func fallbackCompany(doc *html.Node, u *url.URL) string {
	if c := metaContent(doc, "og:site_name", "application-name"); c != "" {
		return collapseInline(c)
	}
	labels := strings.Split(strings.ToLower(u.Hostname()), ".")
	for i, l := range labels {
		if l == "www" || l == "careers" || l == "jobs" || l == "boards" || l == "apply" {
			continue
		}
		if i < len(labels)-1 {
			return l
		}
	}
	return ""
}

// titleSeparators are the characters career pages use to append site chrome.
var titleSeparators = []string{" | ", " – ", " — ", " - ", " · ", " :: "}

func trimTitleChrome(title string) string {
	for _, sep := range titleSeparators {
		if i := strings.LastIndex(title, sep); i > 0 {
			return strings.TrimSpace(title[:i])
		}
	}
	return title
}
