"""Backfill historical changelog entries from older monthly dump exports.

TWO source kinds, all handled here:

1. **Current era** (pipeline.fetch / mediawiki_content_current): only a
   3-month rolling window is available online for sawikisource --
   2026-05-01, 2026-06-01, 2026-07-01 as of this writing. The format itself
   launched 2026-01-30 (announced on xmldatadumps-l), and the same 3-month
   window holds for unrelated wikis too (enwiki, dewiki checked directly)
   with 2026-04-01 404ing on enwiki -- consistent with active pruning to a
   rolling window, though no page documents a retention policy in writing;
   this is an inference from the observed pattern, not a cited policy.

2. **Materialized** (_ensure_materialized_month): EVERY month older than
   LEGACY_CUTOVER, back to MATERIALIZED_FLOOR (2012-02, the first month
   whose cutoff is after वर्गसर्वस्वम् -- this Atlas's tree model's root
   category -- first existed at 2012-01-20T10:18:19Z; see
   RootCategoryMissing). pipeline/materialize_snapshots.py reconstructs a
   month from sawikisource-latest-pages-meta-history.xml.bz2 (every
   surviving revision ever made): for a cutoff date D, the wiki's state at D
   is just, per page, the newest revision <= D. The ~533MB meta-history dump
   is downloaded once (auto-fetched on first need, cached at
   dump/_materialize_src/) and reused for every materialized month; each
   month's reconstruction is generated on demand, one at a time, the moment
   ensure_month needs it -- never all months up front, since each
   materialized XML runs 1-2GB and there's no reason to hold more than one
   on disk at a time (see cleanup_raw_dump). See
   pipeline/materialize_snapshots.py's docstring for the known deviations
   from a genuine dump of that month.

**Internet Archive and legacy-format dumps are deliberately NOT used**, even
for the many months where a real archived dump exists (76 of them, 2011-09
to 2022-05). An archived dump records the titles pages bore at that date; a
reconstruction records the titles they bear today. Since text_count derives
from title breadcrumbs, the two count the same corpus differently, so a
series that switched sources stepped by hundreds of texts at every switch --
artifacts that read as corpus events. One method applied uniformly is less
faithful per month but is the only way months compare to each other. See
notes/interpretive-decisions.md section 6 for the rationale and its costs,
and notes/internet-archive-dumps.md for what IA actually holds and how to
fetch it if a period-accurate snapshot is ever needed.

Consequently ensure_month has no era detection: >= LEGACY_CUTOVER goes to
pipeline.fetch (dump/1_current_format_live/<date>/), everything older is
materialized (dump/3_materialized/<date>/). default_months() is a plain
calendar enumeration, so coverage is gap-free by construction -- there is no
longer any "hole" to detect, and compute_materialized_months() /
materialized_months() / MATERIALIZED_MONTHS have been deleted outright so
nothing can branch on a stale list again (_ensure_legacy_month and the
fetch_legacy import survive for update_source_eras.py's IA bookkeeping);
dump/2_legacy_format_live/ and dump/4_legacy_format_archive/ are now dead
folders. Each month is processed into a
throwaway tree.json-shaped snapshot, and pipeline.compare runs pairwise
across consecutive months, appending each diff to docs/data/changelog.json.

Once a month's snapshot is written, its raw dump directory is deleted
immediately (cleanup_raw_dump) -- the multi-GB .xml/.bz2 export is never
read again afterward, only the snapshot is (by pipeline.compare, or by a
resumed run's ensure_snapshot existence check). Pass --keep-raw-dumps to
disable this and keep raw dumps around for inspection. This now includes
materialized-era months too: unlike the raw meta-history dump they're
reconstructed from (which stays cached, since re-downloading it is the
expensive part), a materialized month's XML is cheap to regenerate on
demand from that local cache, so there's no reason to keep it around after
its snapshot exists.

Deliberately does NOT write docs/data/tree.json or docs/VERSION -- those
reflect the live, current-month pipeline state, not a historical replay.
This calls process.py's internals directly rather than shelling out to
`python -m pipeline.process`, specifically to skip its unconditional
_stamp_data_version() call (see process.py:main), which would otherwise
overwrite docs/VERSION with backfill dates.

With no --months given, the default is the full available range: every
month from MATERIALIZED_FLOOR up to the current era (see default_months(),
a plain calendar enumeration -- each reconstructed on demand as it is
reached), plus the current-era months.

For a smart, resumable, one-month-at-a-time walk through this whole range
(so results can be inspected incrementally rather than run in one long
batch), use `make backfill` / pipeline/run_backfill_sequence.sh instead of
calling this module directly with the full default range.

Regardless of the order --months lists dates in (run_backfill_sequence.sh
passes each step as `OLDER NEWER`), the actual fetch/snapshot work in
main() always happens newest-to-oldest -- so within any single invocation,
nothing older is ever fetched/processed before something newer. Only the
final pairwise-comparison step (cheap once every snapshot already exists)
runs in --months's given order, since that's what the changelog's
old_date/date pairing and existing_transitions dedup rely on.

Usage:
    python -m pipeline.backfill --months 2022-04-01 2022-05-01
    python -m pipeline.backfill --snapshot-dir /tmp/snapshots
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from pipeline import fetch as fetch_mod
from pipeline import fetch_legacy
from pipeline.build_tree import build_category_graph, build_main_tree, refile_category
from pipeline.content_cache import build_content_cache, load_content_cache, rebuild_inputs_from_cache, write_content_cache
from pipeline.parse_dump import parse_dump
from pipeline.process import build_tree_json, compute_all_content_sizes
from pipeline.snapshot_io import write_json_gz
from pipeline.transclusion import build_reverse_transclusion_map, build_transclusion_map
from pipeline.compare import build_report, print_summary

# Below this date, months are fetched from the Internet Archive
# (pipeline.fetch_legacy) instead of mediawiki_content_current
# (pipeline.fetch) -- see module docstring. This is the format's launch
# date, not the rolling window's own start, so it never needs to move again
# even as the window itself slides forward -- see current_era_months() below
# for the part that actually tracks "what's live right now".
LEGACY_CUTOVER = "2026-05-01"


def current_era_months() -> list[str]:
    """Queries mediawiki_content_current live (via pipeline.fetch.find_export)
    for every complete month from LEGACY_CUTOVER through today, oldest first.
    Replaces a hardcoded CURRENT_ERA_MONTHS list, which went stale the moment
    a new month rolled into the live 3-month rolling window (e.g. once August
    has a complete export, a hardcoded "May/June/July" list would silently
    keep omitting August from every backfill run, and NEWEST_ANCHORED in
    run_backfill_sequence.sh would never catch up) -- see
    notes/about-page-fact-check.md's "deltas stop short of the live dump"
    investigation for the incident that surfaced this. Walks forward from
    LEGACY_CUTOVER rather than backward from today, since the window's exact
    width isn't guaranteed to stay 3 months forever and this reflects
    whatever Wikimedia is actually serving right now, not an assumed count."""
    months = []
    year, month = (int(p) for p in LEGACY_CUTOVER.split("-")[:2])
    today = _dt.date.today()
    while _dt.date(year, month, 1) <= today:
        date_str = f"{year:04d}-{month:02d}-01"
        if fetch_mod.find_export(date_str) is not None:
            months.append(date_str)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months

# Materialized months are wherever pipeline.fetch_legacy's two sources
# (live rolling window + Internet Archive) have no dump at all, between the
# earliest and latest months either source ever covers -- computed once
# below by scanning for interior holes in fetch_legacy.list_available_months(),
# rather than maintained as hardcoded date ranges. This used to be two fixed
# ranges (2022-06/2025-10, the Internet-Archive/live-window gap, and
# 2012-01/2014-06, the earlier pre-legacy-coverage gap) -- both still detected
# automatically by the scan below, along with two narrower holes the old
# hardcoded ranges missed entirely (2015-01, 2015-05) and two wider ones
# (2018-04 through 2018-07, 2019-04 through 2020-06) -- all five confirmed
# live against archive.org's advancedsearch API to have zero
# sawikisource-<date> item for any month in range, not merely undetected.
#
# Bounded at 2012-01: वर्गसर्वस्वम् (this Atlas's tree model's root category)
# doesn't exist before then (its earliest revision is
# 2012-01-20T10:18:19Z, confirmed against the cached meta-history dump), so
# earlier holes (e.g. 2011-11, 2011-12) are genuinely too early for this
# Atlas's tree model regardless of materialization -- process_dump already
# catches this per-month via RootCategoryMissing rather than needing a
# hole-detection cutoff here, but there's no point materializing a month
# that will just be skipped.
#
# Availability is expected to stay fixed in practice (Internet Archive's
# volunteer sawikisource pipeline stalled after 2022-05-01, and the live
# rolling windows only ever grow forward, never backfill past holes), so
# this is computed once per process rather than re-verified continuously --
# see fetch_legacy.list_available_months's own disk cache (24h TTL) for why
# repeated calls within a run_backfill_sequence.sh walk are still cheap.
#
# Each detected month is reconstructed on demand via
# pipeline/materialize_snapshots.py: a full-state XML derived from
# sawikisource-latest-pages-meta-history.xml.bz2 (every surviving revision
# ever made, auto-downloaded once and cached -- see
# _ensure_materialize_source), taking, per page, the newest revision <= that
# month's cutoff. See that script's module docstring for the known
# deviations from a "real" dump of that month (deleted-page handling,
# title/namespace drift, heuristic redirect re-derivation).
# The earliest month whose cutoff is after वर्गसर्वस्वम् (this Atlas's root
# category) first existed: its very first revision is 2012-01-20T10:18:19Z,
# verified against the cached meta-history dump. A 2012-01-01 cutoff predates
# that by 19 days, so process_dump could only ever raise RootCategoryMissing
# on it -- the floor sits at 2012-02-01 so that month is never fetched,
# materialized, or parsed just to be skipped. (Consistent with the historical
# record: 2012-01-01 is the one month in range with no content cache and no
# snapshot, and docs/data/changelog.json starts at 2012-02-01.)
MATERIALIZED_FLOOR = "2012-02-01"


# NOTE: compute_materialized_months() / materialized_months() /
# MATERIALIZED_MONTHS used to live here -- a live scan of
# fetch_legacy.list_available_months() for interior holes in Internet
# Archive's coverage, i.e. "which months must be materialized because no real
# dump exists". They are gone on purpose. Now that EVERY month older than
# LEGACY_CUTOVER is materialized (see ensure_month), that list answers a
# question nothing asks, and keeping it around was actively dangerous: it
# still returned only the ~91 originally-detected holes, so any code that
# branched on `date_str in materialized_months()` silently stopped matching
# most materialized months. That bit twice -- cleanup_raw_dump leaked 17GB of
# raw XML, and run_backfill_sequence.sh would have walked only the months IA
# happened to cover. Route on the date (< LEGACY_CUTOVER) instead; there is no
# list to consult and therefore no list to drift out of sync.


MATERIALIZE_SOURCE_URL = (
    "https://dumps.wikimedia.org/sawikisource/latest/"
    "sawikisource-latest-pages-meta-history.xml.bz2"
)

# Four numbered era folders under dump/, in the same newest-to-oldest order
# run_backfill_sequence.sh walks in -- see notes on each era above and in
# CLAUDE.md's "Historical backfill and the changelog" section:
#   1_current_format_live   -- pipeline.fetch, mediawiki_content_current
#   2_legacy_format_live    -- pipeline.fetch_legacy, live rolling window
#   3_materialized          -- every month < LEGACY_CUTOVER
#   4_legacy_format_archive -- pipeline.fetch_legacy, Internet Archive
DEFAULT_DUMP_ROOT = Path(__file__).resolve().parent.parent / "data" / "dump" / "1_current_format_live"
DEFAULT_LEGACY_LIVE_DUMP_ROOT = Path(__file__).resolve().parent.parent / "data" / "dump" / "2_legacy_format_live"
DEFAULT_MATERIALIZED_ROOT = Path(__file__).resolve().parent.parent / "data" / "dump" / "3_materialized"
DEFAULT_LEGACY_ARCHIVE_DUMP_ROOT = Path(__file__).resolve().parent.parent / "data" / "dump" / "4_legacy_format_archive"
DEFAULT_MATERIALIZE_SRC_DIR = Path(__file__).resolve().parent.parent / "data" / "dump" / "_materialize_src"
DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "dump" / "_backfill_snapshots"
DEFAULT_CONTENT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "dump" / "_backfill_content_cache"
DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "docs" / "data" / "changelog.json"

# The current live docs/data/tree.json already IS this month's processed
# snapshot (see docs/VERSION's __content_version__) -- reuse it rather than
# re-fetching/re-processing a month we already have, as long as its
# __content_version__ actually matches. Falls back to a normal fetch+process
# if VERSION is missing/stale/doesn't match.
LIVE_TREE_JSON = Path(__file__).resolve().parent.parent / "docs" / "data" / "tree.json"
LIVE_VERSION_FILE = Path(__file__).resolve().parent.parent / "docs" / "VERSION"


def _live_content_version() -> str | None:
    if not LIVE_VERSION_FILE.exists():
        return None
    for line in LIVE_VERSION_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("__content_version__"):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


class RootCategoryMissing(Exception):
    """Raised by process_dump when the Atlas's organizing root category
    (वर्गसर्वस्वम्) isn't present in a dump at all -- not a parse error, but a
    real historical fact about sa.wikisource: this whole Atlas's tree model
    depends on that category existing, and it didn't yet in the site's
    earliest days (confirmed on the 2011-10-13 Internet Archive dump: only 3
    categories existed on the entire site at that point, none of them
    वर्गसर्वस्वम्). Callers should treat this as "too early to build a
    snapshot for," skipping the month rather than crashing the whole run."""


def process_dump(xml_path: Path, workers: int | None = None) -> tuple[dict, dict]:
    """Same sequence as process.py's main(), minus writing docs/tree.json or
    stamping docs/VERSION -- returns (tree, content_cache) in memory instead.
    content_cache is the small cache of build_tree_json's inputs (see
    pipeline.content_cache) that lets a future build_tree_json-only logic fix
    skip re-running the expensive steps below (parse_dump, compute_all_content_sizes)."""
    print(f"parsing {xml_path}", file=sys.stderr)
    dump_index = parse_dump(xml_path)
    cat_ns_name = dump_index.namespaces[dump_index.category_ns_id()]

    print("building Main-namespace tree...", file=sys.stderr)
    main_records = dump_index.pages_by_ns[0]
    main_nodes = build_main_tree(main_records)

    print("building category graph...", file=sys.stderr)
    category_records = dump_index.pages_by_ns[14]
    graph = build_category_graph(category_records, cat_ns_name)
    if graph.root_title not in graph.nodes:
        raise RootCategoryMissing(
            f"root category '{graph.root_title}' not found in {xml_path} -- "
            f"too early in sa.wikisource's history for this Atlas's tree model"
        )

    refile_category(graph, "धर्मशास्त्रम्", new_parent_title="ग्रन्थाः", old_parent_title=graph.root_title)

    print("building transclusion map...", file=sys.stderr)
    transclusion_map = build_transclusion_map(main_records)
    reverse_transclusion_map = build_reverse_transclusion_map(main_records)

    print("computing content sizes (this is the slow step)...", file=sys.stderr)
    content_index = compute_all_content_sizes(
        dump_index, transliterate=True, transclusion_map=transclusion_map, workers=workers,
    )

    print("assembling tree...", file=sys.stderr)
    tree = build_tree_json(dump_index, graph, main_nodes, transclusion_map, content_index, reverse_transclusion_map)
    content_cache = build_content_cache(dump_index, content_index, main_records, category_records)
    return tree, content_cache


def default_months() -> list[str]:
    """Every month from MATERIALIZED_FLOOR up to the current era, oldest
    first -- the full available range absent an explicit --months override.

    A plain calendar enumeration, with no network query: every month older
    than LEGACY_CUTOVER is materialized on demand (see ensure_month), so
    what's fetchable no longer depends on any source's listing and coverage
    is gap-free by construction. Only the tail (current_era_months()) is
    queried live, since the current-format rolling window's contents shift
    forward over time.

    The floor drops months that predate वर्गसर्वस्वम् entirely, on which
    process_dump can only raise RootCategoryMissing (see that exception's
    docstring) -- after a full materialization and parse spent to rediscover
    a fact the floor already encodes.

    NOTE: source selection has been removed -- every month from
    MATERIALIZED_FLOOR up to the current era is materialized, so this is now
    a plain calendar enumeration rather than a query of what Internet Archive
    happens to hold."""
    current = current_era_months()
    first_current = current[0] if current else None

    months: list[str] = []
    year, month = (int(p) for p in MATERIALIZED_FLOOR.split("-")[:2])
    while True:
        date_str = f"{year:04d}-{month:02d}-01"
        if first_current is not None and date_str >= first_current:
            break
        if date_str >= _today_month_start():
            break
        months.append(date_str)
        month += 1
        if month > 12:
            month, year = 1, year + 1

    return months + current


def _today_month_start() -> str:
    from datetime import date as _date
    today = _date.today()
    return f"{today.year:04d}-{today.month:02d}-01"


def ensure_month(
    date_str: str,
    dump_root: Path,
    legacy_live_dump_root: Path,
    legacy_archive_dump_root: Path,
    materialized_root: Path = DEFAULT_MATERIALIZED_ROOT,
    materialize_src_dir: Path = DEFAULT_MATERIALIZE_SRC_DIR,
) -> Path:
    """Fetch+decompress one month's export if not already there, returning the
    path to its uncompressed XML.

    TWO SOURCES ONLY (the legacy/Internet-Archive era selection has been
    removed on purpose):

    - Current era (>= LEGACY_CUTOVER): pipeline.fetch, the live
      mediawiki_content_current export, into dump_root/<date>/.
    - Everything older: ALWAYS materialized from the pages-meta-history dump,
      regardless of whether Internet Archive or the legacy rolling window
      happens to have a real dump for that month.

    This makes every historical month share one reconstruction method, so the
    series can't step at a source boundary. The trade is materialization's own
    deviations (see materialize_snapshots.py) applied uniformly rather than
    only in the gaps."""
    if date_str >= LEGACY_CUTOVER:
        out_dir = dump_root / date_str
        existing = sorted(out_dir.glob("sawikisource-*.xml")) if out_dir.exists() else []
        if existing:
            print(f"{date_str}: already fetched -> {existing[0]}", file=sys.stderr)
            return existing[0]
        paths = fetch_mod.fetch(out_dir=out_dir, date=date_str)
        xml_paths = [p for p in paths if p.suffix == ".xml"]
        if not xml_paths:
            raise RuntimeError(f"no .xml produced for {date_str}")
        return xml_paths[0]

    return _ensure_materialized_month(date_str, materialized_root, materialize_src_dir)


def _ensure_legacy_month(date_str: str, legacy_live_dump_root: Path, legacy_archive_dump_root: Path) -> Path:
    """date_str is the requested YYYY-MM-01 (the calendar month, used as this
    entry's identity throughout backfill/changelog). The actual underlying
    snapshot within that month can fall on any day and come from either
    source (e.g. 2022-01-20 from Internet Archive, or 2026-04-01 from the
    live rolling window -- see fetch_legacy.list_available_months), which
    also determines which of the two era-specific roots it belongs under --
    fetch_legacy.fetch_snapshot writes into a directory named after that real
    day, so this looks inside <root>/<ym>-*/ (a glob on the month prefix)
    rather than assuming day 01. Checks both roots for an already-fetched
    month, since which source serves a given date can shift over time as the
    live window's floor drifts forward (see module docstring)."""
    ym = date_str[:7]
    for root in (legacy_live_dump_root, legacy_archive_dump_root):
        existing = sorted(root.glob(f"{ym}-*/sawikisource-*.xml"))
        if existing:
            print(f"{date_str}: already fetched (legacy) -> {existing[0]}", file=sys.stderr)
            return existing[0]

    by_month = fetch_legacy.list_available_months()
    dump = by_month.get(ym)
    if dump is None:
        raise RuntimeError(f"no legacy snapshot found for month {ym} (date {date_str})")
    out_dir = legacy_live_dump_root if dump.source == "live" else legacy_archive_dump_root
    return fetch_legacy.fetch_snapshot(dump, out_dir=out_dir)


def _ensure_materialize_source(materialize_src_dir: Path) -> Path:
    """Download sawikisource-latest-pages-meta-history.xml.bz2 to
    materialize_src_dir once (skipped if already present) -- this is the raw
    material every materialized month is generated from, so
    it's the one thing in the materialized era worth caching indefinitely
    rather than deleting after use (see cleanup_raw_dump, which no longer
    touches this file). Reuses pipeline.fetch_legacy's session/User-Agent
    since both hit dumps.wikimedia.org under the same bot-etiquette
    contract (maxlag / compliant UA -- see CLAUDE.md's rate-limiting notes)."""
    materialize_src_dir.mkdir(parents=True, exist_ok=True)
    dest = materialize_src_dir / "sawikisource-latest-pages-meta-history.xml.bz2"
    if dest.exists():
        return dest
    print(f"downloading {MATERIALIZE_SOURCE_URL} (~530MB, one-time)...", file=sys.stderr)
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    with fetch_legacy.session.get(MATERIALIZE_SOURCE_URL, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(tmp_dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                fh.write(chunk)
    tmp_dest.rename(dest)
    print(f"downloaded: {dest}", file=sys.stderr)
    return dest


def _is_complete_materialized_xml(path: Path) -> bool:
    """A materialize_snapshots.py output is only trustworthy if it was fully
    written -- SnapshotWriter.close() now writes to the real path via an
    atomic rename (see that class), but a file materialized before that fix
    landed could still be sitting on disk, truncated (interrupted mid-write,
    file present but incomplete). Cheap check: the closing </mediawiki> tag
    is only ever written by close(), right before the rename, so its
    presence at the tail of the file is a reliable completeness signal
    without re-parsing the whole XML."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 64))
            tail = f.read()
        return b"</mediawiki>" in tail
    except OSError:
        return False


def _ensure_materialized_month(
    date_str: str, materialized_dump_root: Path, materialize_src_dir: Path
) -> Path:
    """Reconstruct date_str's snapshot XML on demand via
    pipeline/materialize_snapshots.py, one month at a time -- never all of
    all materialized months up front, since each output runs 1-2GB and
    cleanup_raw_dump deletes it right after its tree.json snapshot is
    written (see module docstring). Returns the existing output directly if
    this date was already materialized and not yet cleaned up (e.g. a
    resumed run) AND it looks complete (see _is_complete_materialized_xml)
    -- an incomplete file found on disk (e.g. from before
    SnapshotWriter's atomic-rename fix) is deleted and regenerated rather
    than silently reused, since a truncated file merely being present used
    to pass this check and crash pipeline.parse_dump much later, deep into
    a backfill sequence."""
    day8 = date_str.replace("-", "")
    item_dir = materialized_dump_root / date_str
    existing = sorted(item_dir.glob(f"sawikisource-{day8}-pages-articles.synth.xml")) if item_dir.exists() else []
    if existing:
        if _is_complete_materialized_xml(existing[0]):
            print(f"{date_str}: already materialized -> {existing[0]}", file=sys.stderr)
            return existing[0]
        print(f"{date_str}: found incomplete materialized XML at {existing[0]} -- deleting and regenerating",
              file=sys.stderr)
        existing[0].unlink()

    src_bz2 = _ensure_materialize_source(materialize_src_dir)
    item_dir.mkdir(parents=True, exist_ok=True)
    print(f"{date_str}: materializing from {src_bz2}...", file=sys.stderr)
    subprocess.run(
        [
            sys.executable, str(Path(__file__).resolve().parent / "materialize_snapshots.py"),
            str(src_bz2), "--dates", date_str, "--outdir", str(item_dir),
        ],
        check=True,
    )
    xml_path = item_dir / f"sawikisource-{day8}-pages-articles.synth.xml"
    if not xml_path.exists():
        raise RuntimeError(f"materialize_snapshots.py did not produce {xml_path}")
    print(f"{date_str}: materialized -> {xml_path}", file=sys.stderr)
    return xml_path


def cleanup_raw_dump(
    date_str: str,
    dump_root: Path,
    legacy_live_dump_root: Path,
    legacy_archive_dump_root: Path,
    materialized_root: Path = DEFAULT_MATERIALIZED_ROOT,
) -> None:
    """Delete the raw dump (.xml.bz2 + decompressed .xml, and their parent
    dated directory) for one month, once its snapshot is confirmed written --
    the snapshot is all that pipeline.compare or a resumed backfill run ever
    reads afterward (see ensure_snapshot's existence check), so keeping the
    multi-GB raw export around after that point is pure disk waste. Never
    touches dump_root's own top-level loose files (the live current-month
    dump used by routine `make process`) -- only the dated subdirectories
    this module itself creates via ensure_month.

    A materialized month's XML is cheaply regenerable on demand from the
    cached meta-history dump (see _ensure_materialize_source), so there's no
    reason to keep it around after its snapshot exists -- unlike the cached
    meta-history .bz2 itself, which this function never touches.

    The branch here MUST mirror ensure_month's routing exactly, or dumps leak.
    It used to test `date_str in materialized_months()` -- the old
    hole-detection list -- which silently stopped matching once every
    pre-cutover month became materialized: months outside that stale list fell
    through to the legacy branch, which globs era folders nothing writes to
    any more, so their multi-GB XMLs were never deleted (17GB leaked before
    this was caught)."""
    if date_str < LEGACY_CUTOVER:
        d = materialized_root / date_str
        if d.is_dir():
            shutil.rmtree(d)
            print(f"{date_str}: deleted materialized dump -> {d}", file=sys.stderr)
        # Legacy-era folders are dead (see ensure_month), but sweep any dated
        # dirs an older run left behind so this stays a complete cleanup.
        ym = date_str[:7]
        for root in (legacy_live_dump_root, legacy_archive_dump_root):
            for stale in sorted(root.glob(f"{ym}-*")):
                if stale.is_dir():
                    shutil.rmtree(stale)
                    print(f"{date_str}: deleted raw dump -> {stale}", file=sys.stderr)
        return
    d = dump_root / date_str
    if d.is_dir():
        shutil.rmtree(d)
        print(f"{date_str}: deleted raw dump -> {d}", file=sys.stderr)


def _existing_snapshot_path(date_str: str, snapshot_dir: Path) -> Path | None:
    """A month's snapshot may exist as either tree-<date>.json.gz (current
    default) or the older, uncompressed tree-<date>.json (written before
    gzip-by-default landed) -- either counts as "already built" for resume
    purposes. Prefers the gzipped path if somehow both exist."""
    gz_path = snapshot_dir / f"tree-{date_str}.json.gz"
    if gz_path.exists():
        return gz_path
    plain_path = snapshot_dir / f"tree-{date_str}.json"
    if plain_path.exists():
        return plain_path
    return None


def rebuild_tree_from_cache(date_str: str, snapshot_dir: Path, content_cache_dir: Path) -> Path:
    """Rebuilds tree-<date>.json.gz from its cached content-<date>.json.gz,
    skipping parse_dump and compute_all_content_sizes entirely -- for
    propagating a build_tree_json/build_category_graph-level logic fix into
    already-backfilled months without a full slow rebuild. Overwrites
    whatever snapshot already exists (that's the point). Raises FileNotFoundError
    if no content cache exists for this month (never backfilled since the
    cache was introduced -- needs a full `ensure_snapshot` run instead)."""
    content_cache_path = content_cache_dir / f"content-{date_str}.json.gz"
    if not content_cache_path.exists():
        raise FileNotFoundError(
            f"no content cache for {date_str} at {content_cache_path} -- "
            f"run a full backfill for this month first"
        )
    cache = load_content_cache(content_cache_path)
    inputs = rebuild_inputs_from_cache(cache)
    refile_category(inputs.graph, "धर्मशास्त्रम्", new_parent_title="ग्रन्थाः", old_parent_title=inputs.graph.root_title)

    tree = build_tree_json(
        inputs.dump_index, inputs.graph, inputs.main_nodes,
        inputs.transclusion_map, inputs.content_index, inputs.reverse_transclusion_map,
    )
    snapshot_path = snapshot_dir / f"tree-{date_str}.json.gz"
    write_json_gz(snapshot_path, tree)
    print(f"{date_str}: rebuilt snapshot from content cache -> {snapshot_path}", file=sys.stderr)
    print(f"{date_str}: root stats: {tree['root']['stats']}", file=sys.stderr)
    return snapshot_path


def ensure_snapshot(
    date_str: str,
    get_xml_path: Callable[[], Path],
    snapshot_dir: Path,
    workers: int | None,
    content_cache_dir: Path = DEFAULT_CONTENT_CACHE_DIR,
    force_reprocess: bool = False,
) -> Path:
    """Resolves a month's snapshot by the cheapest route that works, trying
    in order:

    1. An existing tree-<date>.json.gz -- reused as-is.
    2. The live docs/data/tree.json, when date_str is the current month
       (matches docs/VERSION's __content_version__) -- copied into a snapshot.
    3. The cached content-<date>.json.gz -- reassembled via
       rebuild_tree_from_cache (build_tree_json only, no network, seconds).
    4. A full fetch + process_dump -- the slow path (download, parse_dump,
       compute_all_content_sizes), the only one that needs the raw dump.

    Step 3 is what makes deleting a snapshot cheap: a tree-assembly fix
    (build_tree_json/build_category_graph/rollup/dedup) can be propagated
    across every already-backfilled month by deleting the snapshots and
    rerunning, without re-downloading or re-running the slow content-size
    computation. Pass force_reprocess=True to skip steps 2 and 3 and go
    straight to the dump -- for when the cached inputs themselves are
    suspect, not just the assembly logic built from them.

    get_xml_path is called (triggering ensure_month's fetch/decompress) only
    if every cheaper route above is unavailable -- so an already-completed
    month's raw dump, which cleanup_raw_dump deletes right after its snapshot
    is written, is never re-fetched on a resumed run just to be thrown away
    again.

    Writes both tree-<date>.json.gz (the assembled tree, what pipeline.compare
    diffs) and content-<date>.json.gz (see pipeline.content_cache -- the
    small cache of build_tree_json's inputs). Both are gzipped by default
    since 153 months of either adds up at full size and neither is ever read
    partially."""
    existing = _existing_snapshot_path(date_str, snapshot_dir)
    if existing is not None:
        print(f"{date_str}: snapshot already built -> {existing}", file=sys.stderr)
        return existing

    snapshot_path = snapshot_dir / f"tree-{date_str}.json.gz"
    content_cache_path = content_cache_dir / f"content-{date_str}.json.gz"

    # Each cheap route degrades independently into the next -- a missing
    # content cache must not force a full re-download when the live
    # tree.json for this exact month is sitting right there, and vice versa.
    if not force_reprocess and date_str == _live_content_version() and LIVE_TREE_JSON.exists():
        print(f"{date_str}: matches live docs/data/tree.json's __content_version__, "
              f"reusing it instead of reprocessing", file=sys.stderr)
        tree = json.loads(LIVE_TREE_JSON.read_text(encoding="utf-8"))
        write_json_gz(snapshot_path, tree)
        return snapshot_path

    if not force_reprocess and content_cache_path.exists():
        return rebuild_tree_from_cache(date_str, snapshot_dir, content_cache_dir)

    xml_path = get_xml_path()
    tree, content_cache = process_dump(xml_path, workers=workers)
    write_json_gz(snapshot_path, tree)
    print(f"{date_str}: wrote snapshot -> {snapshot_path}", file=sys.stderr)
    print(f"{date_str}: root stats: {tree['root']['stats']}", file=sys.stderr)

    write_content_cache(content_cache_path, content_cache)
    print(f"{date_str}: wrote content cache -> {content_cache_path}", file=sys.stderr)
    return snapshot_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", nargs="+", default=None,
                     help="months to backfill, oldest first, as YYYY-MM-01 (default: full available "
                          "range -- every Internet Archive month, every materialized gap month "
                          "(reconstructed on demand), plus the 3 current-era months, queried live)")
    ap.add_argument("--dump-root", type=Path, default=DEFAULT_DUMP_ROOT,
                     help="directory under which each current-era month gets its own <date>/ subdir")
    ap.add_argument("--legacy-live-dump-root", type=Path, default=DEFAULT_LEGACY_LIVE_DUMP_ROOT,
                     help="directory under which each legacy-era month served by the live rolling window "
                          "gets its own subdir")
    ap.add_argument("--legacy-archive-dump-root", type=Path, default=DEFAULT_LEGACY_ARCHIVE_DUMP_ROOT,
                     help="directory under which each legacy-era month served by Internet Archive "
                          "gets its own subdir")
    ap.add_argument("--materialized-root", type=Path, default=DEFAULT_MATERIALIZED_ROOT,
                     help="directory where each materialized month (every month older than "
                          "LEGACY_CUTOVER -- see ensure_month) "
                          "gets its own subdir, generated on demand one month at a time and deleted again "
                          "once its snapshot is written (see _ensure_materialized_month)")
    ap.add_argument("--materialize-src-dir", type=Path, default=DEFAULT_MATERIALIZE_SRC_DIR,
                     help="directory to cache the ~530MB sawikisource-latest-pages-meta-history.xml.bz2 "
                          "in, auto-downloaded once on first need and reused for every materialized month")
    ap.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR,
                     help="where to write per-month tree.json-shaped snapshots (gitignored, throwaway)")
    ap.add_argument("--content-cache-dir", type=Path, default=DEFAULT_CONTENT_CACHE_DIR,
                     help="where to write/read per-month content caches (see pipeline.content_cache) -- "
                          "gitignored, lets a build_tree_json-only fix skip re-parsing the dump and "
                          "re-running the slow content-size computation")
    ap.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG,
                     help="changelog.json to append pairwise diffs to")
    ap.add_argument("--workers", type=int, default=None, help="worker processes for content-size computation")
    ap.add_argument("--keep-raw-dumps", action="store_true",
                     help="don't delete each month's raw dump (.xml/.bz2) after its snapshot is written -- "
                          "by default, raw dumps are deleted immediately since the snapshot is all that's "
                          "ever needed afterward (see cleanup_raw_dump)")
    ap.add_argument("--force-reprocess", action="store_true",
                     help="rebuild every requested month's snapshot from its raw dump (re-fetching and "
                          "re-running the slow content-size computation) instead of reassembling it from "
                          "the cached content-<date>.json.gz -- for when the cached inputs themselves are "
                          "suspect, not just the tree-assembly logic built from them. Months whose snapshot "
                          "already exists are still reused; delete those first to force a full redo.")
    args = ap.parse_args()

    months = args.months if args.months is not None else default_months()

    # Fetch/snapshot newest-first, regardless of the order months was given
    # in -- e.g. `--months OLDER NEWER` must not process OLDER before NEWER.
    # This matters because ensure_month/ensure_snapshot are the only steps
    # that do real work (network fetch, dump parsing, content-size
    # computation); the pairwise comparison below is comparatively instant
    # once every snapshot already exists, so it's fine to do in whatever
    # order months was given, and preserving that original order is what
    # existing_transitions/changelog entries below expect.
    snapshots_by_date = {}
    for date_str in sorted(months, reverse=True):
        get_xml_path = lambda d=date_str: ensure_month(
            d, args.dump_root, args.legacy_live_dump_root, args.legacy_archive_dump_root,
            args.materialized_root, args.materialize_src_dir,
        )
        try:
            snapshot_path = ensure_snapshot(date_str, get_xml_path, args.snapshot_dir, args.workers,
                                             args.content_cache_dir, args.force_reprocess)
        except RootCategoryMissing as e:
            # Too early in sa.wikisource's history for this Atlas's tree
            # model (see RootCategoryMissing) -- skip this month rather than
            # aborting the whole run; any comparison pairs involving it are
            # dropped below.
            print(f"{date_str}: {e} -- skipping this month", file=sys.stderr)
            if not args.keep_raw_dumps:
                cleanup_raw_dump(date_str, args.dump_root, args.legacy_live_dump_root, args.legacy_archive_dump_root,
                                  args.materialized_root)
            continue
        snapshots_by_date[date_str] = snapshot_path
        if not args.keep_raw_dumps:
            cleanup_raw_dump(date_str, args.dump_root, args.legacy_live_dump_root, args.legacy_archive_dump_root,
                              args.materialized_root)

    snapshots = [(date_str, snapshots_by_date[date_str]) for date_str in months if date_str in snapshots_by_date]

    if args.changelog.exists():
        log = json.loads(args.changelog.read_text())
    else:
        log = []
    entries_by_pair = {(e.get("old_date"), e.get("date")): e for e in log}

    for (old_date, old_snap), (new_date, new_snap) in zip(snapshots, snapshots[1:]):
        old_iso, new_iso = f"{old_date}T00:00:00Z", f"{new_date}T00:00:00Z"
        pair = (old_iso, new_iso)

        print(f"\n=== comparing {old_date} -> {new_date} ===", file=sys.stderr)
        report = build_report(old_snap, new_snap)
        print_summary(report)

        existing_entry = entries_by_pair.get(pair)
        if existing_entry is not None:
            # Overwrite in place, same id -- both snapshots are cheap to
            # rebuild/reuse (see ensure_snapshot's reuse-if-present check
            # above), so re-diffing an already-logged transition is trivial
            # and the point is always picking up corrected stats, not
            # skipping work.
            existing_entry.update(report)
            print(f"updated changelog entry #{existing_entry.get('id')}", file=sys.stderr)
        else:
            next_id = max((e.get("id", 0) for e in log), default=0) + 1
            entry = {
                "id": next_id,
                "date": new_iso,
                "old_date": old_iso,
                **report,
            }
            log.append(entry)
            entries_by_pair[pair] = entry
            print(f"appended changelog entry #{next_id}", file=sys.stderr)

        # Sort by date and write on every entry (not just at the end) --
        # entries are computed in whatever order --months was given, and a
        # backfill run mixing legacy and current-era months would otherwise
        # leave the file (and about.js's newest-first reversal of it) out
        # of chronological order. `id` stays a stable identifier, untouched
        # by this re-sort.
        log.sort(key=lambda e: e["date"])
        args.changelog.parent.mkdir(parents=True, exist_ok=True)
        args.changelog.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
