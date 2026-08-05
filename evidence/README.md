# Evidence corpus

This directory is the "me" side of the match. Everything in here gets chunked,
embedded, and cited by the matcher, so what you write here is literally what the
system can claim on your behalf.

**The files next to this README are placeholders. Replace them with your own.**
They exist so `jme evidence ingest` does something sensible on a fresh clone.

> This README is skipped by the ingester's default walk only if you exclude it; by
> default every `.md` file under this directory is ingested, including this one.
> Delete it once you have real evidence in place, or keep it and accept one chunk of
> instructions in the corpus.

## What belongs here

- Resume bullets, verbatim. One bullet per line, as a markdown list.
- Project writeups: what you built, the actual mechanism, the numbers.
- Course and coursework descriptions, if they carry real artifacts.
- Hand-written notes about work that never made it onto a resume.

Repo READMEs do **not** belong here as copies. Configure them instead:

```
JME_EVIDENCE_REPOS=your-handle/your-repo,your-handle/another-repo
GITHUB_TOKEN=ghp_...        # optional: private repos and a higher rate limit
```

## How to write it so retrieval works

The chunker splits on markdown headings first, then packs bullets and paragraphs into
200-500 token chunks, and **never splits a bullet**. Two consequences worth writing
for:

1. **Use headings generously.** Each chunk carries its nearest heading, and a chunk
   never spans a heading boundary. `## Redis Streams consumer group` retrieves far
   better than a wall of text under `## Projects`.
2. **Make each bullet self-contained.** A bullet is the atomic unit of evidence and
   will be cited alone, without its neighbours. "Reduced p99 by 40%" is useless
   without saying what and how; "Cut fetch p99 from 4.1s to 2.4s by moving per-host
   rate limiting into a shared Redis token bucket" survives being read in isolation.

Concrete beats impressive. The gap report exists to tell you what to go learn, and it
can only do that if the corpus is honest about what you have actually done.

## Commands

```bash
jme evidence ingest --dry-run     # what would change, and whether the version bumps
jme evidence ingest               # chunk, embed, upsert
jme evidence list                 # every live chunk, with tags and embedding status
jme evidence show 42              # full text of one chunk
jme evidence tag 42 Redis Go      # manual canonical-skill tags (needs the taxonomy)
jme evidence version              # current corpus version and size
```

Re-running `ingest` with nothing changed is a no-op: no re-embedding, no version bump,
no cache invalidation. Only a real edit, addition, deletion, or tag change bumps
`evidence_version`, and a bump marks matches for active postings stale so they are
recomputed on the next scheduled run.
