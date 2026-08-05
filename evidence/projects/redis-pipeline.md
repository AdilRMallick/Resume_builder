# PLACEHOLDER - example project writeup

This file shows the shape a good project writeup takes. The content is a stand-in.
Replace it with a project you actually built.

## Problem

PLACEHOLDER: One paragraph on what was actually hard. Not the feature list, the
constraint. Something like: a few hundred URLs across a dozen hosts had to be fetched
without getting the IP blocked, and workers had to be able to die mid-job without
losing or duplicating work.

## Design

- PLACEHOLDER: The central decision, stated as a tradeoff. For example: Redis Streams
  with consumer groups rather than a list-based queue, because `XACK`, `XAUTOCLAIM`,
  and `XPENDING` give real delivery semantics and answer "what happens when a worker
  dies" with a mechanism instead of a hope.
- PLACEHOLDER: A second decision, with the rejected alternative named.
- PLACEHOLDER: The thing you would do differently now, and why.

## Mechanism

PLACEHOLDER: Describe how it actually works, in enough detail that someone could
argue with it. Mention the data structures and the failure path, not just the happy
path. This is the part of the corpus that makes a citation defensible.

```
placeholder: a small diagram or code snippet is fine here
```

Fenced code blocks are never reflowed or split by the chunker, so a short snippet
stays intact with the paragraph that explains it.

## Results

- PLACEHOLDER: A number, its baseline, and how it was measured.
- PLACEHOLDER: A second number, ideally one that got worse, and what you traded for it.
