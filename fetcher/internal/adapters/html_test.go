package adapters

import (
	"strings"
	"testing"
)

func TestHTMLFragmentToText(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "paragraphs become blocks",
			in:   "<p>First paragraph.</p><p>Second paragraph.</p>",
			want: "First paragraph.\n\nSecond paragraph.",
		},
		{
			name: "list items keep bullet structure",
			in:   "<ul><li>Go</li><li>PostgreSQL</li></ul>",
			want: "- Go\n- PostgreSQL",
		},
		{
			name: "br becomes a newline",
			in:   "Detroit, MI<br>Remote",
			want: "Detroit, MI\nRemote",
		},
		{
			name: "script and style are dropped",
			in:   "<div><style>.a{color:red}</style><p>Kept.</p><script>evil()</script></div>",
			want: "Kept.",
		},
		{
			name: "entities are decoded",
			in:   "<p>Docker &amp; Kubernetes &mdash; nice&nbsp;to have</p>",
			want: "Docker & Kubernetes — nice to have",
		},
		{
			name: "entity escaped html is unescaped first",
			in:   "&lt;p&gt;Greenhouse double escapes &amp;amp; then we fix it.&lt;/p&gt;",
			want: "Greenhouse double escapes & then we fix it.",
		},
		{
			name: "whitespace collapses deterministically",
			in:   "<p>Too    many\n\n\n   spaces</p>",
			want: "Too many spaces",
		},
		{
			name: "empty input stays empty",
			in:   "",
			want: "",
		},
		{
			name: "unclosed tags do not lose text",
			in:   "<div><p>Unclosed paragraph<div>and a sibling",
			want: "Unclosed paragraph\n\nand a sibling",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := htmlFragmentToText(tc.in); got != tc.want {
				t.Errorf("htmlFragmentToText(%q)\n got %q\nwant %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestCollapseIsIdempotent(t *testing.T) {
	// JD text is hashed for the match cache key, so normalisation has to be a
	// fixed point: running it twice must not produce a different hash.
	in := "  Line one \n\n\n\n Line two\t\ttabbed  \n"
	once := collapse(in)
	if twice := collapse(once); once != twice {
		t.Errorf("collapse not idempotent:\n once %q\ntwice %q", once, twice)
	}
	if strings.Contains(once, "\t") || strings.Contains(once, "\n\n\n") {
		t.Errorf("collapse left raw whitespace: %q", once)
	}
}

func TestCollapseInline(t *testing.T) {
	cases := []struct{ in, want string }{
		{"  Software  Engineer,\nNew Grad ", "Software Engineer, New Grad"},
		{"Detroit,&nbsp;MI", "Detroit, MI"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := collapseInline(tc.in); got != tc.want {
			t.Errorf("collapseInline(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestLooksEscaped(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"&lt;p&gt;hello&lt;/p&gt;", true},
		{"<p>hello</p>", false},
		{"plain text", false},
		{"<p>a &lt; b</p>", false}, // real markup that merely mentions an entity
	}
	for _, tc := range cases {
		if got := looksEscaped(tc.in); got != tc.want {
			t.Errorf("looksEscaped(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}
