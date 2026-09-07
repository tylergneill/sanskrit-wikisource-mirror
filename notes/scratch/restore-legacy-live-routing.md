# Restore legacy-format-live routing in `ensure_month`

**Status:** scoped, not implemented. Written 2026-08-04.

## The problem in one sentence

`docs/data/source_eras.json` labels 2025-12 .. 2026-04 as *legacy-format live*,
and the About page's timeline renders them that way, but `ensure_month` can no
longer produce them by that route — those months survive only as cached
artifacts, and regenerating them would silently reclassify them as
materialized.

## What is actually on disk right now (verified 2026-08-04)

- `docs/data/source_eras.json`: `era2_rolling_start = "2025-12-01"`,
  `materialized_ranges` ends at `2025-11-01`. So era 2 covers 2025-12 through
  2026-04 (era 1 starts at `LEGACY_CUTOVER`, 2026-05-01).
- `dump/_backfill_content_cache/content-2025-12-01.json.gz` .. `content-2026-04-01.json.gz`
  all carry mtime **2026-08-03 17:59** — untouched by the 2026-08-04 backfill run.
  These hold the real legacy-live-derived byte counts, category tags, redirects,
  timestamps, and transclusion results.
- `dump/_backfill_snapshots/tree-*.json.gz` were all rewritten 2026-08-04
  14:25–14:26, but via **route 3** (reassembly from the caches above), so they
  inherit the legacy-live inputs. Snapshot mtimes are therefore *not* evidence
  of how a month was sourced; the content cache is.
- `dump/2_legacy_format_live/` is **empty**.

The data is correctly labeled. Only the code path that produced it is gone.

## Why it broke

`7f23737` ("materialize every historical month, drop Internet Archive as a
source") collapsed `ensure_month`'s three-way dispatch into a two-way one.

Before:

```python
if date_str in materialized_months():          # hole-detection list
    return _ensure_materialized_month(...)
if date_str < LEGACY_CUTOVER:
    return _ensure_legacy_month(...)           # BOTH live-window and IA
# else: current-format live
```

After (current `backfill.py:375`):

```python
if date_str >= LEGACY_CUTOVER:
    ...                                        # current-format live
return _ensure_materialized_month(...)         # everything older
```

That commit's target was Internet Archive **archived** dumps (era 4), for a
real and well-documented reason: an archived dump records the titles pages bore
*at that date*, a reconstruction records the titles they bear *today*, and since
`text_count` derives from title breadcrumbs the two count the same corpus
differently — the series stepped by hundreds of texts at every source switch.
See `notes/interpretive-decisions.md` §6.

The **legacy-format live rolling window** (era 2) was collateral. It did not
have the IA title-drift problem, and it was never the thing being cut. But
`_ensure_legacy_month` served both sources through one function, so removing
the branch that called it removed both at once.

## Why this matters even though the data is currently correct

Route 3 masks it. As long as the content caches survive, every rebuild
reproduces the legacy-live numbers. But:

- Delete `content-2025-12-01.json.gz` .. `content-2026-04-01.json.gz` and rerun,
  and those five months come back **materialized** — the timeline silently
  changes source type, and `text_count` may step at the new boundary.
- Any `force_reprocess=True` run over that range does the same.
- Refreshing the cached meta-history dump (see CLAUDE.md, "Dump vintage")
  invalidates caches wholesale and would take these months with it.

So this is a latent trap whose blast radius is exactly the artifact class
`7f23737` was written to prevent.

## Scope of the fix

### 1. Add a second cutoff constant

```python
LEGACY_CUTOVER = "2026-05-01"        # existing: era 2 -> era 1
MATERIALIZED_CUTOVER = "2025-12-01"  # new: era 3 -> era 2
```

`MATERIALIZED_CUTOVER` should track `source_eras.json`'s `era2_rolling_start`
conceptually, but must be a **constant, not a live query** — the legacy live
window's floor drifts forward over time, and if routing followed that drift,
already-built months would silently change era as the window moved. Pin it and
move it deliberately, the same way `LEGACY_CUTOVER` is pinned. Note the
inversion this creates: the live window will eventually roll past 2025-12, at
which point those months genuinely can't be re-fetched live any more and the
constant becomes a statement about how the *cached* data was built. That is
acceptable and worth a comment, but it means the restored path is
re-runnable only while the window still covers it.

### 2. Restore the three-way dispatch in `ensure_month` (`backfill.py:351`)

```python
if date_str >= LEGACY_CUTOVER:
    ...                                     # current-format live, unchanged
if date_str >= MATERIALIZED_CUTOVER:
    return _ensure_legacy_month(date_str, legacy_live_dump_root, legacy_archive_dump_root)
return _ensure_materialized_month(...)
```

`_ensure_legacy_month` (`backfill.py:390`) is **still present and intact** — it
was kept for `update_source_eras.py`'s IA bookkeeping. It already checks both
era roots for an already-fetched month and globs `<root>/<ym>-*/` rather than
assuming day 01, because the underlying snapshot can fall on any day. Verify it
still runs; do not rewrite it from scratch. Working prior version at
`git show 7f23737^:pipeline/backfill.py`.

**Constrain it to the live window only.** Its docstring describes routing to
*either* era root depending on which source served the month. Since IA is
deliberately gone, the restored path must reach only
`fetch_legacy`'s live-window source and never the archive — otherwise
`7f23737`'s decision is quietly reverted for any month IA also covers. Confirm
what `fetch_legacy.fetch_snapshot` does when both sources have a date, and
force the live one.

### 3. Mirror the routing in `cleanup_raw_dump` (`backfill.py:504`)

Its docstring already warns: *"The branch here MUST mirror ensure_month's
routing exactly, or dumps leak."* `7f23737` fixed a 17GB leak caused by exactly
this drifting out of sync. Any change to `ensure_month`'s dispatch requires the
same change here, in the same commit.

### 4. Docs to update in the same change

- `pipeline/backfill.py` module docstring, lines ~43–52 — currently states
  `ensure_month` has no era detection and calls `dump/2_legacy_format_live/` a
  dead folder. Both become false.
- `CLAUDE.md` — the "Historical backfill and the changelog" section makes the
  same two claims ("`ensure_month` therefore has no era-detection logic";
  "The other two era folders … are **dead**").

## How to verify the fix without a full rebuild

The content caches are the ground truth for what legacy-live produced. A
correct restoration reproduces them:

1. Back up `content-2026-04-01.json.gz` (one month, the cheapest to re-fetch —
   it is still inside the live window).
2. Delete it *and* `tree-2026-04-01.json.gz`, forcing route 4.
3. Run `python -m pipeline.backfill --months 2026-03-01 2026-04-01`.
4. Confirm the run fetched via the legacy-live path (not materialization), and
   that the regenerated content cache matches the backup.

If it matches, routing is restored faithfully. If it differs, the restored path
is not equivalent to what built the shipped data, and that difference needs
explaining before the range is rebuilt.

Do **not** validate by deleting the whole 2025-12 .. 2026-04 range at once —
if the fix is wrong, the legacy-live-derived caches are gone and the only way
back is a re-fetch that may no longer be possible once the live window rolls
forward.

## Related

- `notes/interpretive-decisions.md` §6 — why IA archived dumps were dropped.
- `notes/internet-archive-dumps.md` — IA coverage and how to fetch one if a
  period-accurate snapshot is ever needed.
- CLAUDE.md, "Two on-disk layers per month" — route 1/2/3/4 resolution, and why
  the content cache rather than the snapshot is the meaningful provenance layer.
