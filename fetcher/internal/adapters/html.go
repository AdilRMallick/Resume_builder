package adapters

import (
	"regexp"
	"strings"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

// This file holds the HTML -> readable text machinery shared by every adapter.
// The ATS APIs hand back HTML fragments in their description fields; the
// fallback adapter hands back a whole page. Both end up here.

// dropTags never contribute to a job description. Removed before extraction so
// they cannot inflate a candidate's score in the readability pass either.
var dropTags = map[atom.Atom]bool{
	atom.Script:   true,
	atom.Style:    true,
	atom.Noscript: true,
	atom.Iframe:   true,
	atom.Svg:      true,
	atom.Canvas:   true,
	atom.Nav:      true,
	atom.Header:   true,
	atom.Footer:   true,
	atom.Aside:    true,
	atom.Form:     true,
	atom.Button:   true,
	atom.Select:   true,
	atom.Template: true,
	atom.Object:   true,
	atom.Video:    true,
	atom.Audio:    true,
}

// blockTags force a line break when they open and close.
var blockTags = map[atom.Atom]bool{
	atom.P: true, atom.Div: true, atom.Section: true, atom.Article: true,
	atom.Main: true, atom.Ul: true, atom.Ol: true, atom.Li: true,
	atom.Table: true, atom.Tr: true, atom.Blockquote: true, atom.Pre: true,
	atom.H1: true, atom.H2: true, atom.H3: true, atom.H4: true, atom.H5: true,
	atom.H6: true, atom.Dl: true, atom.Dt: true, atom.Dd: true, atom.Hr: true,
	atom.Figure: true, atom.Figcaption: true, atom.Address: true,
}

// looksEscaped reports whether a string is HTML that has been entity-escaped,
// e.g. Greenhouse's `content` field, which arrives as "&lt;p&gt;Hello&lt;/p&gt;".
func looksEscaped(s string) bool {
	return strings.Contains(s, "&lt;") && !strings.Contains(s, "<")
}

// htmlFragmentToText renders an HTML fragment (or an entity-escaped one) as
// plain readable text.
func htmlFragmentToText(fragment string) string {
	if fragment == "" {
		return ""
	}
	if looksEscaped(fragment) {
		fragment = html.UnescapeString(fragment)
	}
	doc, err := html.Parse(strings.NewReader(fragment))
	if err != nil {
		// html.Parse only errors on reader failure, never on malformed markup,
		// so this is effectively unreachable for a string reader. Degrade to a
		// regex tag strip rather than losing the posting.
		return collapse(html.UnescapeString(tagStripper.ReplaceAllString(fragment, " ")))
	}
	prune(doc)
	return collapse(renderText(doc))
}

var tagStripper = regexp.MustCompile(`(?s)<[^>]*>`)

// prune deletes dropTags subtrees and comments in place.
func prune(n *html.Node) {
	var next *html.Node
	for c := n.FirstChild; c != nil; c = next {
		next = c.NextSibling
		switch {
		case c.Type == html.CommentNode:
			n.RemoveChild(c)
		case c.Type == html.ElementNode && dropTags[c.DataAtom]:
			n.RemoveChild(c)
		default:
			prune(c)
		}
	}
}

// renderText walks a node emitting text with structural line breaks. List items
// get a leading "- " so bullet structure survives into the extracted text,
// which matters because requirement extraction is bullet oriented.
//
// Newlines inside a text node are source formatting, not content, so they
// become spaces exactly as a browser would render them. The only newlines in
// the output are the ones this function injects, which is what makes the result
// stable across differently indented copies of the same markup, and therefore
// stable as a cache key.
func renderText(n *html.Node) string {
	var b strings.Builder
	var pre int
	var walk func(*html.Node)
	walk = func(node *html.Node) {
		switch node.Type {
		case html.TextNode:
			if pre > 0 {
				b.WriteString(node.Data)
			} else {
				b.WriteString(strings.ReplaceAll(node.Data, "\n", " "))
			}
			return
		case html.ElementNode:
			switch node.DataAtom {
			case atom.Br:
				b.WriteString("\n")
				return
			case atom.Li:
				b.WriteString("\n- ")
			case atom.Pre:
				pre++
				b.WriteString("\n")
			default:
				if blockTags[node.DataAtom] {
					b.WriteString("\n")
				}
			}
		}
		for c := node.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
		if node.Type == html.ElementNode {
			switch node.DataAtom {
			case atom.Li:
				// No closing newline: the next item's "\n- " separates them, and
				// the enclosing list closes the block.
			case atom.Pre:
				pre--
				b.WriteString("\n")
			default:
				if blockTags[node.DataAtom] {
					b.WriteString("\n")
				}
			}
		}
	}
	walk(n)
	return b.String()
}

var (
	horizontalWS = regexp.MustCompile(`[ \t\x{00a0}\x{2007}\x{202f}\f\v\r]+`)
	blankRuns    = regexp.MustCompile(`\n{3,}`)
)

// collapse normalises extracted text: uniform spaces, no trailing whitespace,
// at most one blank line between blocks. Deterministic output matters because
// the JD text is hashed for the match cache key.
func collapse(s string) string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = horizontalWS.ReplaceAllString(s, " ")
	lines := strings.Split(s, "\n")
	for i, ln := range lines {
		lines[i] = strings.TrimSpace(ln)
	}
	s = strings.Join(lines, "\n")
	s = blankRuns.ReplaceAllString(s, "\n\n")
	return strings.TrimSpace(s)
}

// collapseInline is collapse for single-line fields like title and location.
func collapseInline(s string) string {
	return strings.TrimSpace(horizontalWS.ReplaceAllString(strings.Join(strings.Fields(html.UnescapeString(s)), " "), " "))
}

// ---------------------------------------------------------------------------
// Readability-style main content extraction
// ---------------------------------------------------------------------------

// scoreCarrier tags elements whose text is real prose rather than chrome.
var scoreCarrier = map[atom.Atom]bool{
	atom.P: true, atom.Li: true, atom.Pre: true, atom.Blockquote: true,
	atom.Td: true, atom.Dd: true,
}

// containerTags are eligible to be chosen as the main content node.
var containerTags = map[atom.Atom]bool{
	atom.Div: true, atom.Section: true, atom.Article: true, atom.Main: true,
	atom.Td: true, atom.Body: true, atom.Ul: true, atom.Ol: true, atom.Dl: true,
}

// extractMainText runs a cut-down Readability over a full HTML document and
// returns the text of the highest scoring container.
//
// The algorithm, in short: drop chrome, give every prose element a score from
// its length and comma count, propagate that score up to its parent and half of
// it to its grandparent, then penalise candidates whose text is mostly anchor
// text (navigation and "related jobs" rails). The winner's subtree is rendered
// as text. If nothing scores, the whole body is used.
func extractMainText(doc *html.Node) string {
	prune(doc)

	scores := map[*html.Node]float64{}
	var collect func(*html.Node)
	collect = func(n *html.Node) {
		if n.Type == html.ElementNode && scoreCarrier[n.DataAtom] {
			text := strings.TrimSpace(renderText(n))
			if runeLen(text) >= 25 {
				s := 1 + float64(strings.Count(text, ",")) + min(float64(runeLen(text))/100, 3)
				if p := nearestContainer(n.Parent); p != nil {
					scores[p] += s
					if gp := nearestContainer(p.Parent); gp != nil {
						scores[gp] += s / 2
					}
				}
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			collect(c)
		}
	}
	collect(doc)

	var best *html.Node
	var bestScore float64
	for node, s := range scores {
		adjusted := s * (1 - linkDensity(node))
		// Ties are possible between a wrapper and its child; prefer the deeper
		// node so we keep less chrome. Depth comparison keeps this stable
		// across map iteration order.
		if adjusted > bestScore || (adjusted == bestScore && best != nil && depth(node) > depth(best)) {
			best, bestScore = node, adjusted
		}
	}
	if best == nil {
		if body := findFirst(doc, atom.Body); body != nil {
			best = body
		} else {
			best = doc
		}
	}
	return collapse(renderText(best))
}

// nearestContainer walks up to the first element that may hold main content.
func nearestContainer(n *html.Node) *html.Node {
	for ; n != nil; n = n.Parent {
		if n.Type == html.ElementNode && containerTags[n.DataAtom] {
			return n
		}
	}
	return nil
}

// linkDensity is the fraction of a node's text that sits inside anchors.
func linkDensity(n *html.Node) float64 {
	total := runeLen(strings.TrimSpace(renderText(n)))
	if total == 0 {
		return 0
	}
	linked := 0
	var walk func(*html.Node)
	walk = func(node *html.Node) {
		if node.Type == html.ElementNode && node.DataAtom == atom.A {
			linked += runeLen(strings.TrimSpace(renderText(node)))
			return
		}
		for c := node.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(n)
	return min(float64(linked)/float64(total), 1)
}

func depth(n *html.Node) int {
	d := 0
	for p := n.Parent; p != nil; p = p.Parent {
		d++
	}
	return d
}

func runeLen(s string) int { return len([]rune(s)) }

// findFirst returns the first element with the given tag, depth first.
func findFirst(n *html.Node, a atom.Atom) *html.Node {
	if n.Type == html.ElementNode && n.DataAtom == a {
		return n
	}
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		if found := findFirst(c, a); found != nil {
			return found
		}
	}
	return nil
}

func attr(n *html.Node, key string) string {
	for _, a := range n.Attr {
		if strings.EqualFold(a.Key, key) {
			return a.Val
		}
	}
	return ""
}

// metaContent returns the content of the first <meta> matching either
// property= or name= (case insensitive).
func metaContent(doc *html.Node, keys ...string) string {
	want := make(map[string]bool, len(keys))
	for _, k := range keys {
		want[strings.ToLower(k)] = true
	}
	var found string
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if found != "" {
			return
		}
		if n.Type == html.ElementNode && n.DataAtom == atom.Meta {
			if want[strings.ToLower(attr(n, "property"))] || want[strings.ToLower(attr(n, "name"))] {
				if c := strings.TrimSpace(attr(n, "content")); c != "" {
					found = c
					return
				}
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(doc)
	return found
}

// textOfFirst returns the collapsed text of the first element with the tag.
func textOfFirst(doc *html.Node, a atom.Atom) string {
	n := findFirst(doc, a)
	if n == nil {
		return ""
	}
	return collapseInline(renderText(n))
}
