# 4. Cluster failures with a deterministic hashing embedder and agglomerative linkage

**Status:** accepted · **Date:** 2026-09-13

## Context

A red run with sixty failures is usually not sixty problems. It is two or three problems, one of
which hit forty cases. The list view cannot show that — it shows sixty rows, sorted by nothing in
particular, and triage becomes scrolling.

Grouping failures by what they have in common turns the list into a shape: *"forty of these say the
model stopped emitting the `refund_id` field."* That is where triage should start.

Two decisions are needed: how to turn failure text into something comparable, and how to group it.

## Decision

**Embed with a local, deterministic hashing embedder.** `HashingEmbedder` ("hashing-v1") hashes word
unigrams, word bigrams and character 4-grams into a fixed-width vector with blake2b, then
L2-normalises. No model call, no network, no state.

**Group with average-linkage agglomerative clustering on cosine distance**, cut at a distance
threshold.

Both are swappable — `Embedder` is a `Protocol`, and a real embedding API can be dropped in behind
it — but the default is the local one.

## Why not an embedding API

**Clustering would cost money and take network.** Sixty failures is sixty embedding calls, on a code
path that runs after a run has already failed, in CI, where the user is waiting. Charging for the
privilege of being told why the run broke is a bad trade.

**It would not be reproducible.** Providers update embedding models. Clustering the same run twice,
six months apart, would produce different clusters for reasons entirely outside this repository.
Anything used in a test would need mocking, and then the test is testing the mock.

**The text is not hard.** Failure text is assertion messages and model output: short, full of shared
vocabulary, and heavy with the exact tokens that distinguish one failure mode from another —
`timeout`, `refund_id`, `KeyError`. Lexical overlap is a genuinely good signal here, and semantic
similarity between paraphrases is not the problem being solved. Character 4-grams are in the feature
set specifically because real model output is full of ids, typos and punctuation noise that
word-level features alone handle badly.

The honest limit: two failures that mean the same thing in different words will not cluster. For
this text, that is rare, and a swappable `Embedder` is the escape hatch when it stops being rare.

## Why not k-means

**Nobody knows k.** The number of distinct failure modes in a run is the thing being discovered; it
cannot be an input. Elbow methods and silhouette scores are ways of guessing it that are harder to
explain than the problem.

**Being wrong about k is worse than being slow.** k-means with k too low merges two real problems
into one cluster and hides the smaller one — exactly the failure this feature exists to prevent.

**The threshold means something.** `threshold` is a cosine distance: *how unalike two failures can
be and still belong together*. It can be explained in a sentence and tuned by looking at output.
`k` cannot be explained at all.

Agglomerative clustering is O(n²) in memory and time. At the scale of a failing run — tens to low
hundreds of cases — that is microseconds, and the cases are bounded by what a human will triage
anyway.

## Consequences

**Clusters are stable across runs and reproducible in tests.** Same failures in, same clusters out,
forever, with no fixtures and no mocked provider.

**The cluster label is extracted, not generated.** `top_terms` picks the terms that distinguish a
cluster's members, so a label reads like `"tool · source · fixture · result"` — keywords, not a
sentence. Less polished than an LLM-written summary, and free, deterministic, and impossible to
hallucinate.

**The threshold is a tuning knob with no universal right value.** Too low fragments one problem into
five clusters; too high merges distinct ones. It is configurable, and the cluster view shows member
counts so a bad setting is visible rather than silent.

**Clustering is computed on demand, not on every run.** It is a `POST` to the clusters endpoint,
because most runs are green and clustering zero failures is wasted work.
