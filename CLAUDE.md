# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A more accessible browsing interface for the Sanskrit text collection at sa.wikisource.org. Wikisource's category structure is hard to browse (no good overview for non-technical users, disorienting subcategory nesting, no metadata like filesize, no transliteration). This project builds `docs/data/tree.json` from Wikimedia's monthly XML dump exports and renders it as a static, searchable, transliteration-aware site published to GitHub Pages from `docs/`. It also maintains a historical changelog of how the corpus has grown over time, rendered on the About page.

## Architecture

Three parts connected by generated JSON files:

1. **Pipeline** (`pipeline/`) — a multi-stage Python pipeline, run stage by stage via the `Makefile` targets below, that turns a downloaded MediaWiki XML dump into `docs/data/tree.json`:
   - **Fetch** (`pipeline/fetch.py`) — locates, downloads, verifies, and decompresses the current monthly Content File Export for sa.wikisource.org from `dumps.wikimedia.org/other/mediawiki_content_current/`. Only a 3-month rolling window is available at this endpoint. Discovery has no API: generation starts monthly on the 1st, and a run is only complete once `SHA256SUMS` appears alongside it, so `find_latest_export` checks candidate month directories for that file's presence rather than trusting a listing. The single XML export covers every namespace the pipeline needs (Main, Category, Index, Page, Template, Module) in one download.
   - **Parse** (`pipeline/parse_dump.py`) — stream-parses the dump XML (`iterparse`, one `<page>` at a time, O(1) memory) into per-namespace page records (`DumpIndex`).
   - **Build tree** (`pipeline/build_tree.py`) — constructs the Main-namespace subpage tree (pure tree, split on `/` — a page's parent is the *nearest existing ancestor path*, redirect-resolved at every level, see "Subpage parenting" below) and the Category digraph (manually-maintained, not guaranteed acyclic or fully connected — see "Multi-parented categories" below).
   - **Transclusion** (`pipeline/transclusion.py`) — detects ProofreadPage `<pages index="..." />` transclusion links between Main-namespace pages and Index-namespace scan items, and derives content→category membership.
   - **Content size** (`pipeline/content_size.py`) — real per-page size computation: parse wikitext with `mwparserfromhell`, expand templates by looking up and substituting the matching Template page from the same dump, and transliterate via `skrutable` for the IAST byte count. No heuristics/estimates (unlike the retired v1 scraper).
   - **Process** (`pipeline/process.py`) — runs the above in sequence and assembles a single JSON tree (`docs/data/tree.json`). This is the *only* input the frontend consumes for the live corpus view. See "Key data shape" below for the schema.

2. **Frontend** (`docs/`) — a static, dependency-free vanilla JS app. `app.js` fetches `data/tree.json` client-side and renders a two-pane UI: an expandable/collapsible sidebar tree and a main content pane. `about.js` fetches `data/changelog.json` and renders the historical changelog plus trend charts on `about.html`. No build step, no bundler, no framework — `docs/` is served as-is by GitHub Pages. The only external dependency is the Sanscript CDN script (loaded in `index.html`/`about.html`) used for on-the-fly Devanagari → IAST/ITRANS/HK/ISO/SLP1 transliteration, applied purely in the browser (source data is always stored in Devanagari).

3. **Historical backfill / changelog** (`pipeline/backfill.py` and friends) — walks backward through every available historical monthly dump, builds a throwaway `tree.json`-shaped snapshot for each month, and appends a pairwise size/count/item-level diff between consecutive months to `docs/data/changelog.json`. See "Historical backfill and the changelog" below for the full design.

### Everything the pipeline writes lives under `data/`

**One gitignored tree, not two** (consolidated 2026-08-28 — `dump/` used to sit
at the repo root):

    data/dump/           MediaWiki exports + backfill caches (~9 GB)
    data/text_extract/   the corpus text, when `make extract-text` has run

`.gitignore` carries a single `/data/` rule, so anything new the pipeline
writes is ignored by default rather than needing its own line — and the leading
slash keeps `docs/data/` tracked, which is the part that actually ships.

The layout matches the other two Atlases, where `data/fulltext_cache/` holds
what a site served and `data/text_extract/` holds clean text derived from it.
This repo has no `fulltext_cache/`: its equivalent is the bulk XML in
`data/dump/`, which is a dump export rather than a per-text walk.

Because the frontend has no build step, `docs/` (including `docs/data/tree.json` and `docs/data/changelog.json`) is what's actually deployed — regenerating these and committing them *is* the deploy step for content updates.

## Commands

```
make refresh-dump         # download/verify/decompress the current monthly dump into data/dump/
make refresh-dump-force   # same, but force re-download/re-verify/re-decompress
make process               # build docs/data/tree.json from the downloaded dump
make backfill               # walk the full historical range, rebuild docs/data/changelog.json from scratch
make regen-changelog         # rebuild docs/data/changelog.json from already-cached snapshots only, no network access
make verify                # check the committed docs/ artifacts agree with each other (offline, seconds)
make extract-text          # process + write the corpus text out (NEEDS rivulet; exits 2 without it)
make serve                 # serve docs/ locally on port 8001
make ngrok                 # expose the local server via a public ngrok tunnel (for mobile testing)
```

### The monthly update sequence

When a new monthly dump appears, run these four in order, then bump
`__code_version__` in `docs/VERSION` by hand if the frontend changed
(`process.py` deliberately never touches that field):

```
make refresh-dump        # discover + download the new month; deletes the prior month's .xml/.bz2
make process             # build docs/data/tree.json, stamp VERSION's __content_version__ to the new month
make backfill            # rebuild docs/data/changelog.json, now including the new month-to-month transition
make audit-update-about  # re-run the structural audit against the new dump, refresh its findings in about.html
```

`make audit-update-about` goes last because it reads the dump XML directly (not `tree.json`), so it needs `refresh-dump` to have landed the new month, and its regenerated findings should describe the same dump the rest of the run just published. It rewrites only the `<ul>` between the `AUDIT:START`/`AUDIT:END` markers in `docs/about.html` — never the dump or `tree.json`.

Two things about this sequence are easy to get wrong:

- **`make process` is what demotes the previous month**, not `make backfill`. Restamping `__content_version__` is the whole mechanism: the prior month stops matching `ensure_snapshot`'s route 2 and becomes an ordinary history month resolved by route 1. `make process` also writes `content-<date>.json.gz`, which pre-positions the new month for cheap reassembly later.
- **`make regen-changelog` cannot substitute for `make backfill` here.** `make process` writes `docs/data/tree.json` and the content cache, but never `data/dump/_backfill_snapshots/tree-<date>.json.gz` — snapshots are backfill's own storage layer. Since `regen-changelog` derives its month list by globbing that directory, a brand-new month is absent from the list entirely and its transition is silently never diffed. Only a real `pipeline.backfill` run creates the snapshot, via route 2's copy of the live `tree.json`. Use `regen-changelog` only when every month involved *already* has a snapshot.

There is no test suite, linter, or build step in this repo — the one automated check is `make verify` (see "Deploy" below), which validates generated artifacts rather than code. `app.js`/`about.js` fetch their JSON data via relative paths, so `docs/` must be served over HTTP (`make serve`), not opened via `file://`.

The `make` targets above are run by hand today. A GitHub Action driving fetch → process on the dump's own monthly cadence is the intended eventual automation, not yet implemented; publishing itself is already automated (below).

## Text extraction lives in rivulet, and this repo does not require it

`make process` computes the markup-free Devanāgarī and its IAST for every page,
uses the byte counts, and drops the strings. `--extract-text` is the flag that
writes them out — and **that writer moved to the private `rivulet` package**,
because producing a redistributable corpus is a publishing act, while counting
its bytes is not.

Everything else stays here: the dump download, the tree build, the sizes. **The
repo runs to completion without rivulet**; only `make extract-text` needs it,
and it exits **2** rather than crashing when it is absent (see
`pipeline/fulltext.py` — the one place this repo names rivulet).

**It stays a flag on `process`, never a stage of its own.** The expansion is
expensive and `process` already parallelizes it across a process pool; a
standalone extractor has to redo the parse, the template index, the pool, the
transclusion map and the augmentation. The first attempt did exactly that and
took **17.7 minutes against `process`'s 3–5** for the same computation. Writing
files is the only new work — everything else is already in hand by the time the
writer is called.

Dependency direction is one-way: **rivulet may import from this Atlas; this
Atlas may never require rivulet.**

## Deploy (`.github/workflows/deploy.yml`)

GitHub Pages publishes `docs/` via GitHub Actions, not the legacy branch-and-folder builder. Every push to `main` runs two jobs: `verify-publish` (the gate) and then `deploy`. `workflow_dispatch` allows re-running a deploy from the Actions tab without pushing a commit, which the legacy builder never permitted.

Because `docs/` has no build step, **what is committed there is exactly what ships** — there is no later stage that would notice a half-finished update. `pipeline/verify_publish.py` is that missing stage. It checks only the committed artifacts against each other (no dump, no network, seconds to run), so it can also be run locally as `make verify` before pushing:

- the three files the frontend fetches (`tree.json`, `changelog.json`, `source_eras.json`) exist and are non-empty;
- `docs/VERSION` has all three version fields, with the two dates well-formed;
- **`changelog.json`'s newest entry matches `__content_version__`** — the check the module exists for, catching exactly the two footguns above: `make process` without `make backfill`, or `make regen-changelog` used in its place (which omits a brand-new month *silently*, with no error). Without this gate the About page ships a history that stops one month short of the corpus the rest of the site displays;
- `changelog.json`'s entries chain without month gaps (`old_date` of each equals `date` of its predecessor);
- `tree.json` has root, the schema fields `app.js` needs, and positive `count`/`text_count`.

It deliberately asserts no absolute figures — the corpus grows monthly, so a fixed threshold would need constant bumping — only invariants that hold across any correct run. Zero, though, is treated as a broken build rather than a small corpus.

Note that `tree.json` records no dump date of its own, so it cannot be cross-checked against `__content_version__`; that date lives only in `VERSION`, stamped by `process.py` from the dump filename. The changelog is the artifact carrying dates, which is why the staleness check is anchored there.

## Key data shape (`docs/data/tree.json`)

```
{ "root": Node }

Node (category):
  { id, type: "category", title, children: [Node],
    pages: [PageNode], index_items: [IndexItemNode], stats }

Node (category-pointer): a second+ filing of a category already emitted
elsewhere in the tree (multi-parent category). Appears inline among its
parent's own `children`, alongside real category nodes.
  { id, type: "category-pointer", title, points_to: <id>, stats }

PageNode (Main-namespace page, filed into this category via its own direct
[[वर्गः:...]] tag):
  { id, type: "page", title, url, stats, subpages: [PageNode] }
  subpages come from the Main-namespace subpage tree (title split on "/").

Node (page-pointer): a second+ filing of a page already emitted elsewhere in
the tree (a page tagged with >1 category directly).
  { id, type: "page-pointer", title, url, points_to: <id> }
  No stats/subpages of its own; resolve via points_to.

IndexItemNode (Index-namespace item with ZERO transclusion anywhere in
Main-namespace content -- i.e. raw/unpublished OCR):
  { id, type: "index-item", title, url, stats }
  Never expandable into individual पृष्ठम्:Title/N (scanned-leaf) rows --
  those are only ever summed into this node's own stats, never listed.

Node (index-item-pointer): a second+ filing of an Index item already
emitted elsewhere in the tree. Same shape/resolution as page-pointer.
  { id, type: "index-item-pointer", title, url, points_to: <id> }

stats: { raw_bytes, content_bytes, transliterated_bytes, count, text_count, last_changed }
```

- `title` fields are raw Devanagari (the `वर्गः:`/`अनुक्रमणिका:` namespace prefix is stripped); the frontend transliterates on render, never the pipeline.
- `stats.raw_bytes` is raw MediaWiki wikitext size — dominated by markup/template/category-tag overhead on short pages, not a meaningful "how much content" number on its own. `stats.content_bytes` is real, locally-computed content size after markup stripping and template expansion. `stats.transliterated_bytes` is the IAST byte count of that same content — the frontend's headline "effective size" figure, since IAST is smaller on disk (~51% of the Devanāgarī byte count; the About page rounds this to "approximately 50%") and more common for cross-collection comparison.
- `count` = number of distinct Main pages + Index items reachable from a node, including every subpage individually. Dedup is enforced at build time: the first depth-first occurrence of a category/page/Index-item builds real content and folds its stats into every ancestor's rollup; every later occurrence anywhere else in the tree is emitted as a `-pointer` node instead and skipped when summing ancestor stats — so an item reachable via two paths is counted exactly once, at whichever ancestor its two paths first converge (not only at root).
- `text_count` = number of distinct top-level *texts* reachable from a node — a Main page with no `/`-parent (breadcrumb subpages don't count separately even when independently filed under their own category tag, see "Silent subpage category divergence" below), or an Index item (always top-level). This is what the frontend sidebar shows as the browsable text count, since `count`'s per-subpage granularity overcounts what a reader would call one text.
- The pipeline hardcodes an exclusion list of Wikisource maintenance/junk categories (e.g. `निष्कासनाय`, `अनिर्दिष्टानि पुटानि`) in `parse_dump.py`'s `EXCLUDED_CATEGORIES` — add new junk categories there, not in the frontend.

### Subpage parenting (`build_main_tree` / `_resolve_ancestor`)

A Main page's parent is **the nearest ancestor path that actually exists as a page**, not simply "everything before the last `/`". `_resolve_ancestor` tries, in order: the exact literal immediate parent; then each higher ancestor, longest-first; and at every level it both redirect-resolves the candidate and allows a whitespace-normalized match (`_normalize_path` strips spaces around each `/` segment — sa.wikisource has real titles like `अब्धिनौयानमीमांसा /चतुर्थं खण्डम्`, which MediaWiki itself normalizes when resolving subpages on the live wiki). If nothing resolves, the page stays top-level — a parent is **never synthesized**, and those genuinely-stranded pages are reported by `pipeline.audit`'s `find_unresolvable_slash_paths` instead.

Three constraints worth preserving if this is ever touched again:

- **Exact-literal-first is load-bearing, not an optimization.** `भविष्यपुराणम् /पर्व १ (ब्राह्मपर्व)` is itself a real page whose *own title* carries a stray space, with 226 chapters nesting under it by exact match. Normalizing the path before trying it verbatim would miss, fall through to the shallow root, and pull all of them up a level. There are 73 such normalized-key collisions, which is why `titles_by_normalized` maps to a *list* of real titles rather than one.
- **Redirect resolution happens at every level, not just the immediate parent.** `श्रीमद्भागवत महापुराण/स्कंध ०१/अध्यायः ०१` only reaches its real home by resolving the *root* segment's redirect to `श्रीमद्भागवतपुराणम्`.
- **A candidate resolving to the title itself is rejected.** `कथासरित्सागरः/लम्बकः १३` is a redirect pointing *down* at its own child, which otherwise makes that child its own parent — it then belongs to no root and disappears from the tree entirely (it was absent from a shipped `tree.json` for exactly this reason).

Because `parent_title is None` is simultaneously the predicate for `text_count`, orphan-bucket eligibility, the "parent already carries this tag" filing suppression, and the audit's candidate pool, a parenting miss inflates the text count, pollutes the orphan bucket, *and* corrupts the audit's input at once. Deliberately **not** inferred as a general rule: non-`/` separators, since a hyphen or space carries no structural meaning on MediaWiki — those stay flat and are reported by `find_separator_family_candidates` for a human to fix upstream with a page move. See `notes/wikisource-editing-plan.md`.

#### The flat-family allowlist (`FLAT_FAMILY_PATTERNS`)

The one exception to that, and deliberately an **allowlist rather than a rule**. Two works encode a chapter hierarchy in a separator `_resolve_ancestor` can never see, at a scale that dominates the whole corpus's text count:

| Pattern | Destination | Pages |
|---|---|---|
| `महाभारतम्-NN-<parva>-NNN` | `महाभारतम्/<parva>` (18 pages) | 2,315 |
| `ऋग्वेदः सूक्तं M.S` | `ऋग्वेदः मण्डल M` (10 pages) | 1,028 |

Both were verified against the 2026-07-01 dump before being added, and the bar for adding a row is that same evidence: **every destination already exists on-wiki** as a real non-redirect page, the pattern matches the family exhaustively (2,315/2,315 and 1,028/1,028, no exceptions), and the resulting child counts match the works' real structure (ऋग्वेदः reproduces the canonical saṃhitā's per-maṇḍala counts exactly: 191/43/62/58/87/75/104/103/114/191). `महाभारतम्/आदिपर्व/००१` is one chapter an editor already converted to `/` form by hand, and sa.wikisource carries 18 redirects from the hyphen form to the slash form — so this transcribes a relationship the wiki already asserts elsewhere, rather than inventing one. It is not a licence to infer structure generally.

Why it can't be generalized: below these two families the shapes stop being regular — stems that don't exist at all (`समराङ्गणसूत्रधार अध्याय`), chapter *ranges* rather than chapters (`अष्टाङ्गसंग्रहः ... अध्याय १-५`), naive splits landing mid-parenthetical (`सिद्धान्तकौमुदी (बालमनोरमा पूर्व २-२)`) — and 2,544 flat titles have an inferred stem that coincidentally *is* a real page, so "the stem exists" cannot tell a chapter from a shared prefix (`ऋग्वेदः देवतासूची` is a standalone index sharing the `ऋग्वेदः` prefix). A rule loose enough to catch these two would silently mis-nest hundreds of others, and the failure would be invisible — the tree would look plausible and be wrong.

`_resolve_flat_family` never synthesizes a parent: if a destination stops existing, its pages fall back to top-level exactly like an unresolvable `/` path. `pipeline.audit`'s `check_flat_family_allowlist` asserts every row is still live on each run — a row matching zero pages ("dead, remove it"), pages falling back to top-level, or a destination that became a redirect all surface as loud audit lines rather than a quietly wrong tree. Adding a row therefore costs one table entry and no new machinery; **verify the destination exists and the match count equals the family's real size first.**

### Multi-parented categories (`category-pointer`) and multi-filed pages (`page-pointer`)

Wikisource's category graph is not a strict tree: a category can legitimately be filed under more than one parent, and a page/Index item can legitimately carry more than one category tag. Neither occurrence is more "real" or "canonical" than the other — which one ends up holding the actual content in the JSON is purely an artifact of depth-first build order, not a meaningful distinction. The frontend renders every occurrence as independently selectable/expandable with its own real stats, linking sibling occurrences via a "see also" pointer.

### Untranscluded Index items and OCR content

Wikisource's OCR/"Proofreading" workflow stores scanned page images as `Index:` items (namespace `अनुक्रमणिका`), with individual scanned/proofread leaves as `Page:` items (namespace `पृष्ठम्`, titled `Title/N`). When an Index item's content has been transcluded into a real Main-namespace page (via a ProofreadPage `<pages index="..." />` tag), the Atlas shows the Main-namespace page as the real content and skips listing the Index item separately, since it would just be a duplicate. When an Index item has **zero** transclusion anywhere in Main content, it's shown as its own `index-item` node, with stats summed from its untranscluded `पृष्ठम्:Title/N` leaf pages (never listed individually — only rolled up into the Index item's own stats).

### Orphan bucket (`असम्बद्धवर्गीकृतम्`)

Any Main page or untranscluded Index item unreachable from the root category (`वर्गसर्वस्वम्`) by category descent — either zero category tags, or tags that only point to categories themselves never filed under any reachable parent — is collected into an artificial top-level category, `असम्बद्धवर्गीकृतम्` ("improperly categorized"), appended as a sibling of the real category tree under root. It's listed and browsable in the frontend, but root's own headline stats deliberately exclude its totals, since it isn't part of the "central," properly-organized corpus.

## Historical backfill and the changelog (`pipeline/backfill.py`)

`docs/data/changelog.json` is an append-only array of pairwise month-to-month comparisons (`pipeline/compare.py`'s `build_report()`), each carrying old/new size and count totals plus item-level added/removed/changed-timestamp lists. The About page (`docs/about.html`/`about.js`) renders this as a browsable history plus trend charts, with a "Granularity: Month/Quarter/Year" control that nets adjacent months together client-side (see `about.js`'s `groupEntries`/`reduceGroup`) — no separate precomputed granularity in the JSON itself.

Building this history uses **two** source kinds, both handled by `pipeline/backfill.py`:

1. **Current era** (`pipeline/fetch.py` / `mediawiki_content_current`) — only a 3-month rolling window is available. Used for dates ≥ `LEGACY_CUTOVER`.
2. **Materialized** (`_ensure_materialized_month`) — **every** month older than that, back to `MATERIALIZED_FLOOR` (2012-02 — `वर्गसर्वस्वम्`'s earliest revision is 2012-01-20T10:18:19Z, so a 2012-01-01 cutoff predates the root category by 19 days and can only raise `RootCategoryMissing`; 2012-02-01 is the first cutoff that lands after it).

   **Internet Archive and legacy-format dumps are deliberately no longer used**, even for the many months where a real archived dump exists. An archived dump records the titles pages bore *at that date*; a reconstruction records the titles they bear *today*. Since `text_count` derives from title breadcrumbs, the two count the same corpus differently, and a series that switched sources stepped by hundreds of texts at every switch — artifacts that looked like corpus events. One method applied uniformly is less faithful per month but is the only way months compare to each other. See `notes/interpretive-decisions.md` §6 for the full rationale and what it costs, and `notes/internet-archive-dumps.md` for which months IA actually covers and how to fetch one if a period-accurate snapshot is ever needed. `ensure_month` therefore has no era-detection logic: `>= LEGACY_CUTOVER` → live fetch, everything else → materialize. `default_months()` is a plain calendar enumeration (no network query), so coverage is gap-free by construction — there is no longer any such thing as a "hole" to detect.

   `compute_materialized_months()`, `materialized_months()`, `_ensure_legacy_month`, and the `fetch_legacy` import still exist but are **no longer consulted by routing**; `pipeline/update_source_eras.py` and `run_backfill_sequence.sh` still reference some of them.

   `pipeline/materialize_snapshots.py` reconstructs each month on demand from `sawikisource-latest-pages-meta-history.xml.bz2` (every surviving revision ever made): for a cutoff date D, a page's state is the newest revision with timestamp ≤ D. Each month's reconstruction is generated on demand, one at a time, right when `ensure_month` needs it, and its raw XML is deleted again immediately after its snapshot is written — never more than one materialized dump on disk at a time. The underlying meta-history dump itself (~533MB) is downloaded once and cached, since re-downloading it is the expensive part. See `pipeline/materialize_snapshots.py`'s docstring for known deviations from a genuine dump of that month, and `pipeline/validate_materialization.py` (kept in the repo for future re-validation, not run automatically) for accuracy validation against real dumps at the era boundaries — confirmed within ~0.5-0.6% on every metric for the 2022-2025 gap; re-run against other era boundaries (2014-07, 2018-03/2018-08, 2019-03/2020-07) before trusting those ranges equally.

   **Dump vintage.** Every materialized month inherits the vintage of whichever meta-history run happens to be cached, and nothing in the code or its outputs records which one that is: `MATERIALIZE_SOURCE_URL` fetches the undated `latest/` alias, the file is saved under that same undated name, and `_ensure_materialize_source` is `if dest.exists(): return dest` — no freshness check, by design (`cleanup_raw_dump` never touches it). So the vintage has to be derived from the newest `<timestamp>` in the 6.5GB decompressed file, and no tree snapshot, `changelog.json` entry, or `source_eras.json` field carries it. **The currently cached dump is the 2026-07-01 run** (newest revision 2026-07-02, since runs cut at run time rather than midnight; `<generator>MediaWiki 1.46.0-wmf.26</generator>`) — established in `notes/pre-2012/pre-2012-corpus-history.md`'s "Files" section, which is also where the category-free stats path used to validate this method byte-for-byte against `changelog.json` lives. Two consequences: `materialize_snapshots.py`'s deviation #1 (pages deleted before the dump was taken are absent) makes every materialized month a **lower bound** as of that date, and the ~0.5-0.6% validation figure above was measured against that specific vintage. Refreshing the cached dump therefore invalidates all 91 materialized months (the six ranges above, as currently detected) — deliberately re-download it only if the deleted-page drift matters more than reproducibility, then delete both `data/dump/_backfill_snapshots/tree-<date>.json.gz` *and* `data/dump/_backfill_content_cache/content-<date>.json.gz` for every materialized month, re-run scoped `pipeline.backfill --months` walks, `make regen-changelog`, and re-note the new vintage here. This is the one genuinely expensive kind of rebuild (~3-5 min per month plus re-materialization): a new vintage invalidates the *cached inputs*, so route 3 can't be used and each month must go through `process_dump` again. Contrast a tree-assembly fix, which reuses those same caches and re-derives the full history in ~3 minutes — see "Two on-disk layers per month" below.

`ensure_month` dispatches purely on the date: `>= LEGACY_CUTOVER` → live fetch into `1_current_format_live/<date>/`; anything older → materialize into `3_materialized/<date>/`. The other two era folders (`2_legacy_format_live/`, `4_legacy_format_archive/`) are **dead** — nothing writes to them any more, though `DEFAULT_*_ROOT` constants and the corresponding `ensure_month` parameters still exist. Once a month's snapshot is written, its raw dump directory is deleted immediately (`cleanup_raw_dump`) — pass `--keep-raw-dumps` to disable this.

**Genuinely too-early dumps**: sa.wikisource's category system, and later the ProofreadPage extension (Index/Page namespaces), didn't always exist. The oldest available Internet Archive dump (2011-10-13) predates both — confirmed only 3 categories existed on the entire site at that point, none of them `वर्गसर्वस्वम्`. `parse_dump.py`'s `index_ns_id()`/`page_ns_id()` return `None` (not an error) when those namespaces are genuinely absent from a dump's siteinfo, and `process_dump()` raises a distinct `RootCategoryMissing` when the root category itself doesn't exist yet, which `backfill.py`'s `main()` catches and skips (logging a note) rather than aborting the whole run.

`pipeline/run_backfill_sequence.sh` drives `pipeline.backfill` one month-pair at a time (so a failure on one pair doesn't lose earlier progress), starting from the newest anchored current-era month and walking backward through every legacy + materialized month. Deletes and rebuilds `docs/data/changelog.json` from scratch on every run (see below). Safe to interrupt and rerun — already-fetched/materialized dumps and already-built snapshots are skipped/reused, not redone; the changelog itself is always fully rebuilt, which is cheap.

`pipeline/backfill.py` deliberately does NOT write `docs/data/tree.json` or `docs/VERSION` — those reflect the live, current-month pipeline state, not a historical replay.

`docs/data/source_eras.json` (read by `about.html`'s Snapshots section, to describe era 1/era 2's current live-rolling-window start dates) is refreshed by a separate module, `pipeline/update_source_eras.py`, not by `pipeline.backfill` itself — it does two live network lookups (~1 minute total) that have nothing to do with any particular month-pair, so folding it into every `pipeline.backfill` invocation would mean paying that cost on every one of `run_backfill_sequence.sh`'s 150+ per-step calls for no reason. `run_backfill_sequence.sh` runs it once, standalone, after its whole walk finishes.

`pipeline/fetch_legacy.py`'s `list_available_months()` (merged live-rolling-window + Internet Archive month listing, used by `_ensure_legacy_month`, `default_months()`, `update_source_eras`, and `run_backfill_sequence.sh`'s own upfront `--list` call) is genuinely expensive — 2 listing requests plus one more request *per date* in each listing, dozens total, not just 2. Since `run_backfill_sequence.sh` spawns a fresh `python -m pipeline.backfill` subprocess per step, an in-memory cache wouldn't help; it's cached to disk instead (`data/dump/_fetch_legacy_months_cache.json`, 24h TTL — see `LIST_AVAILABLE_MONTHS_CACHE`/`LIST_AVAILABLE_MONTHS_CACHE_TTL`), so every caller across an entire `run_backfill_sequence.sh` walk shares one query instead of re-deriving the identical listing on every step. Pass `use_cache=False` (or `fetch_legacy --list --no-cache`) to force a fresh query.

### Two on-disk layers per month, and what deleting each one triggers

For each backfilled month, `ensure_snapshot` writes two separate gitignored, gzipped files under `data/dump/`, plus one shared, git-tracked output:

- **`data/dump/_backfill_content_cache/content-<date>.json.gz`** (the *input* layer) — the small, cheap-to-derive-but-annoying-to-lose inputs `build_tree_json` needs: per-page byte counts (raw/content/transliterated — the output of `compute_all_content_sizes`, the genuinely slow step: `mwparserfromhell` parsing, template expansion, `skrutable` transliteration), category tags, redirect targets, timestamps, and transclusion results. See `pipeline/content_cache.py`.
- **`data/dump/_backfill_snapshots/tree-<date>.json.gz`** (the *output* layer) — the fully assembled `tree.json`-shaped snapshot, same schema as `docs/data/tree.json`. What `pipeline/compare.py` actually diffs pairwise.
- **`docs/data/changelog.json`** (git-tracked, not gitignored) — the append-only pairwise diffs between consecutive snapshots, keyed by `(old_date, date)`. This is the only one of the three that's committed and deployed.

`ensure_snapshot` skips a month entirely (both the cache and the tree snapshot) if `tree-<date>.json[.gz]` already exists. `docs/data/changelog.json` itself is deleted at the start of every `run_backfill_sequence.sh` run and rebuilt from scratch: `pipeline.backfill`'s `main()` always recomputes and overwrites (not skips) the changelog entry for every consecutive snapshot pair it sees, matched by `(old_date, date)` — cheap, since it's just a diff of two already-cached snapshots, no XML parsing — so every run reflects the current tree-assembly logic (`build_tree_json`/`build_category_graph`), never a stale entry left over from before a rollup/dedup/assembly fix (e.g. the redirect-parenting or subpage-category-divergence fixes, or the orphan-bucket `all_stats` fix). `id`s are stable across a rerun as long as the changelog isn't deleted mid-sequence by hand; deleting it (as `run_backfill_sequence.sh` does up front) does reset `next_id` to 1 and renumber every entry on that walk — harmless for display, since the changelog viewer sorts by `date`, not `id`.

Deleting `data/dump/_backfill_snapshots/tree-<date>.json.gz` is **cheap**, not a full reprocess. `ensure_snapshot` resolves each month by the cheapest route that works, trying four in order:

1. An existing `tree-<date>.json.gz` → reused as-is.
2. The live `docs/data/tree.json`, when `date_str` matches `docs/VERSION`'s `__content_version__` → copied into a snapshot.
3. The cached `content-<date>.json.gz` → reassembled via `rebuild_tree_from_cache` (`build_tree_json` only, **no network, seconds per month**).
4. A full fetch + `process_dump` → the slow path (download, `parse_dump`, `compute_all_content_sizes`), and the only one that needs the raw dump.

Route 3 is the one that matters: it makes a **tree-assembly** fix (`build_main_tree`/`build_tree_json`/`build_category_graph`/rollup/dedup/parenting) propagate across every already-backfilled month for the cost of re-running assembly alone. Deleting all snapshots and re-running the full range took **~3 minutes** for 174 months as of 2026-07-30 (173 via route 3, 1 via route 2, zero fetches, zero re-materializations) — so a retroactive rebuild is the *default* response to an assembly change, not a last resort. There is no reason to accept a step discontinuity in `changelog.json` to avoid this cost.

Route 3 only applies when the cached *inputs* are still valid. When they aren't — a change to `compute_all_content_sizes`, `parse_dump`, `transclusion.py`, or a refreshed meta-history dump for materialized months — pass `force_reprocess=True` to skip routes 2–3 and go straight to the dump. That path *is* genuinely slow (~3-5 min per month, plus re-download/re-materialization) and is the only situation where scoping tightly with `--months` is worth it.

Deleting `data/dump/_backfill_content_cache/content-<date>.json.gz` alone (without also deleting the tree snapshot) has no effect on the next run, since `ensure_snapshot` never re-derives a snapshot that already exists — the cache is only read when a snapshot is actually being rebuilt (i.e. also deleted).

In short: **`make backfill` always redoes the changelog** (deleted and rebuilt every run); **to redo tree snapshots after an assembly-logic change, delete them and rerun** — route 3 rebuilds them from the content cache in seconds each, so doing the whole history costs ~3 minutes and is the normal thing to do. Only a change to the cached *inputs* themselves needs `force_reprocess` and a real re-fetch.

`make regen-changelog` is a narrower, fully offline variant of the same rebuild: it lists whatever dates already have a snapshot under `data/dump/_backfill_snapshots/` (no network calls — not even `fetch_legacy.list_available_months()`) and passes exactly those as `--months` to `pipeline.backfill`, so every month is an instant snapshot-reuse and the whole run is just re-diffing already-cached snapshots (well under a minute for the full range, as of this writing). Use it instead of `make backfill` whenever the snapshots themselves are already trusted and only `pipeline/compare.py`'s diffing logic (or something in `all_stats`/`build_tree_json`, if those snapshots already reflect the fix) needs to be picked up in the changelog — e.g. how this repo's `असम्बद्धवर्गीकृतम्` orphan-bucket trend-chart dips got fixed. It does not fetch, materialize, or rebuild any snapshot — if a month's snapshot is missing or wrong, it's silently excluded from the diff sequence (or diffed with wrong data) rather than fixed; use `make backfill` (or a scoped `python -m pipeline.backfill --months`) for that.

**This makes it the wrong tool for a newly processed month.** `make process` writes `docs/data/tree.json` and `content-<date>.json.gz` but no snapshot, so immediately after processing a new month that month has no `tree-<date>.json.gz` — it therefore never appears in `regen-changelog`'s glob-derived `--months` list, and the newest transition is silently missing from the changelog with no error. The snapshot only comes into existence during a real `pipeline.backfill` run, where route 2 copies the live `tree.json` into it. See "The monthly update sequence" above.

## Notes

- `docs/VERSION` holds `__code_version__` (bump manually on user-visible frontend changes), `__data_version__` (pipeline-run date, stamped automatically by `process.py`'s `_stamp_data_version`), and `__content_version__` (the Wikimedia dump export's own date, also stamped automatically) — three separate dates, since a pipeline run's date and the dump's own snapshot date can differ.
- `notes/` holds prototype/spec material not yet absorbed into the maintained codebase, and one-off historical analysis scripts kept for the record (not meant to be re-run routinely).
- **Deliberate non-goals**: sub-monthly freshness (would require live API + `list=recentchanges` deltas, not just dump exports); full revision history (the pipeline reads `mediawiki_content_current`, the current-state export, not `_history`); partial-transclusion coverage tracking at Page-namespace granularity (transcluded/untranscluded is tracked as binary per Index item, on purpose — see "Untranscluded Index items" above).
