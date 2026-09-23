# Strata

A regulatory change-to-action workspace. Strata ingests successive versions of a
regulatory proceeding, detects and classifies what changed, verifies every cited
passage against the source text without a model, maps verified changes to a company's
obligations, walks the company graph to find the projects and documents downstream,
routes review to owners, and keeps the whole thing in an append-only event log that can
be replayed and rolled back.

`PRD.md` is the specification, `TDD.md` the design, `WALKTHROUGH.md` a trace of the core
loop file by file, and `NOTES.md` the build log.

## Setup

Requires Python 3.11 or newer. Nothing else: no database server, no API key.

```
make setup
```

Creates `.venv` and installs the dependencies from `pyproject.toml`.

## Commands

```
make setup   create the virtualenv and install dependencies
make run     reset the database, ingest, process v1 -> v2, serve on :8000
make test    run the test suite offline, from the committed cache
make eval    score the pipeline against data/gold/gold.json, write evals/results.md
make reset   delete strata.db and re-ingest
make live    re-record the model cache; needs ANTHROPIC_API_KEY in .env
```

`make run` opens with the review page already populated, because it processes v1 to v2
before starting the server. Use **Load next version** on the review page to run v2 to
v3.

Every model response is committed under `data/llm_cache/`, so `make test` and
`make eval` run with no API key and no network access. `make live` is the only command
that calls the API.

Cache entries are keyed by prompt version, so the directory holds every version
recorded during the build rather than only the current one: the live set is
`extract/v3` plus `mapping/v1`, and the superseded `extract/v1` and `extract/v2`
entries are kept so the responses behind each row of the prompt iteration table in
`NOTES.md` stay readable here. Each of those rows is reproducible by checking out that
row's commit and running `make eval`, which picks up both the prompt version and the
cache as they stood at that commit.

## What to look at

- `http://localhost:8000` — the review center, grouped by change. Each claim shows its
  quote with a verification badge, the obligations it links to, every path through the
  company graph to each impacted node, the recommended action and the assignee.
- **Expert queue** — items held back from owners: a rejected citation, a new obligation
  with no existing node, an unverified rationale, a low-confidence mapping.
- **Audit** — every event newest first, with a rollback control. Rollback is itself an
  event; nothing is deleted.
- `evals/results.md` — the scored run against the gold set.

## Suggested path through the product

1. `make run`, then open the review center. Read one change: the quote, the badge, the
   paths.
2. Open the expert queue. The v2 penalty paragraph is there as `new_obligation`,
   because no existing obligation can be the right link for a duty that did not exist.
3. Create the obligation with the prefilled form. That writes a `node_created` event;
   the company JSON file is never modified. The penalty paragraph raises two items,
   because the model reads the penalty and the bar on recovering it from ratepayers as
   two claims. Create the obligation on the first; the second then offers to link to the
   obligation you just made, which is the right answer when both describe one duty.
4. Back on the review page, **Load next version**. The v3 change to the same penalty now
   links to the obligation you just created.
5. Open the audit page and roll back to an earlier sequence number. The review page
   changes; the log does not shrink.

## Known limitations

- **The data is synthetic.** Three versions of one invented docket and one invented
  company. Real dockets will have numbering and layout the parser has not seen.
- **The committed cache is tied to the exact prompts.** Changing a prompt, a response
  schema, or the model name changes the cache key, and the affected calls then need
  `make live` to re-record. The failure is loud: a `CacheMiss` naming the call.
- **The next version has to be loaded in order, and with the prefilled wording.** The v3
  mapping prompt lists every obligation with its name and text, including the one an
  expert creates during v2 review, so the recorded v3 responses assume both that the
  obligation exists and that it is worded the way `make live` wrote it. Load v3 before
  resolving the expert queue, or edit the prefilled fields, and v3 needs an API key. The
  product handles this rather than failing: the run is rolled back so nothing partial is
  written, and the review page says which queue to resolve first. In a real system the
  mapping candidates would come from a retrieval step over a live obligation store
  rather than from a frozen cache.
- **Confidence values are model self-reports.** They are not calibrated probabilities
  and the escalation threshold of 0.7 has not been tuned against real reviewer
  behaviour. In the recorded run every link scores 0.93 or above, so the threshold does
  no work on this data: the escalations all come from the other rules.
- **One company, one jurisdiction, no authentication.** Every table carries a
  `company_id` and every query filters on it, so a second company is a data change, but
  there is no login and no tenant isolation beyond that column.
- **No notifications.** Routing creates a task and tells no one.
- **No PDF parsing and no live docket fetch.** Versions are markdown files with a fixed
  header and `[n]` paragraph markers.
- **The evals are small and were labelled by the author.** They prove the pipeline
  behaves as specified on the cases we thought of, not that it works on real dockets,
  and they do not measure run-to-run variance, because the cache freezes one run.
