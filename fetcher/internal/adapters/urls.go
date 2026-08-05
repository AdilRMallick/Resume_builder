package adapters

import (
	"errors"
	"net/url"
	"strings"
)

// errURLShape marks a URL that this adapter claimed by host but cannot parse.
// Callers turn it into ErrPermanent: retrying will not change the URL.
func errURLShape(msg string) error { return errors.New(msg) }

// hostMatches reports whether u's host is domain or a subdomain of it. Port and
// case are ignored. Written by hand rather than with strings.HasSuffix so that
// "notgreenhouse.io" does not match "greenhouse.io".
func hostMatches(u *url.URL, domain string) bool {
	if u == nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	domain = strings.ToLower(domain)
	return host == domain || strings.HasSuffix(host, "."+domain)
}

// pathSegments splits a URL path into its non-empty segments.
func pathSegments(p string) []string {
	parts := strings.Split(p, "/")
	out := parts[:0]
	for _, s := range parts {
		if s != "" {
			if unescaped, err := url.PathUnescape(s); err == nil {
				s = unescaped
			}
			out = append(out, s)
		}
	}
	return out
}

func firstPathSegment(p string) string {
	if segs := pathSegments(p); len(segs) > 0 {
		return segs[0]
	}
	return ""
}
