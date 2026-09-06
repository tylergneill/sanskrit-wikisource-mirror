"""Compare two docs/data/tree.json snapshots' root.stats, plus item-level detail.

Adapted to pipeline/process.py's schema: stats are {raw_bytes, content_bytes,
transliterated_bytes, count, last_changed}, and content nodes are "page"
(with nested "subpages") and "index-item". A page/index-item can legitimately
be a real, fully-populated node under more than one category -- so walking
dedupes by id, first-occurrence-wins, matching how process.py's
recompute_stats_dedup already treats root.stats itself.

Usage:
    python -m pipeline.compare OLD.json NEW.json
    python -m pipeline.compare OLD.json NEW.json --append --label "..." --notes "..."
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

from pipeline.snapshot_io import read_json_maybe_gz

STAT_KEYS = ("raw_bytes", "content_bytes", "transliterated_bytes")

ORPHAN_BUCKET_TITLE = "असम्बद्धवर्गीकृतम्"


def collect_items_detailed(root: dict) -> Dict[str, dict]:
    """Walk a tree.json root, return {item_id: record} for every page/index-item
    anywhere in the tree -- the orphan bucket included.

    Unlike the plain first-occurrence-wins roster, this keeps the two facts an
    item's *placement* consists of, which is what distinguishes a real corpus
    change from a filing change:

      stats      first occurrence's stats (identical at every genuine filing)
      categories set of category node ids the item is filed under
      orphaned   True if EVERY filing is inside असम्बद्धवर्गीकृतम्

    `orphaned` is deliberately all-filings-are-orphan rather than any: an item
    filed both in a real category and in the bucket is centrally reachable, so
    it is not orphaned. The bucket only ever holds items with no reachable
    filing at all (see process.py's build_tree_json), so the two readings agree
    in practice -- but the strict one keeps this correct if that ever changes."""
    items: Dict[str, dict] = {}

    def record(node: dict, cat_id, orphan: bool) -> None:
        rec = items.get(node["id"])
        if rec is None:
            rec = items[node["id"]] = {
                "stats": node["stats"],
                "categories": set(),
                "orphaned": True,
            }
        if cat_id is not None:
            rec["categories"].add(cat_id)
        if not orphan:
            rec["orphaned"] = False

    def walk_page(node: dict, cat_id, orphan: bool) -> None:
        if node["type"] == "page-pointer":
            return
        record(node, cat_id, orphan)
        for sub in node.get("subpages", []):
            # A subpage is filed wherever its top-level parent is; it carries
            # no category tag of its own in the tree.
            walk_page(sub, cat_id, orphan)

    def walk(node: dict, orphan: bool) -> None:
        if node["type"] == "category-pointer":
            return
        orphan = orphan or node.get("title") == ORPHAN_BUCKET_TITLE
        for child in node.get("children", []):
            walk(child, orphan)
        for page in node.get("pages", []):
            walk_page(page, node["id"], orphan)
        for idx in node.get("index_items", []):
            if idx["type"] == "index-item-pointer":
                continue
            record(idx, node["id"], orphan)

    walk(root, False)
    return items


def collect_items(root: dict, include_orphan_bucket: bool = False) -> Dict[str, dict]:
    """{item_id: stats} for every page/index-item, deduped by first occurrence.

    root.stats deliberately excludes the orphan bucket (असम्बद्धवर्गीकृतम्, a
    direct child of root -- see process.py's build_tree_json), so by default
    this skips it too, keeping item-level counts consistent with the size/count
    deltas read from root.stats. Pass include_orphan_bucket=True to walk it
    anyway. Derived from collect_items_detailed so there is one walk to keep
    correct, not two."""
    detailed = collect_items_detailed(root)
    return {
        iid: rec["stats"]
        for iid, rec in detailed.items()
        if include_orphan_bucket or not rec["orphaned"]
    }


def pct(delta: float, base: float):
    if base == 0:
        return None
    return 100.0 * delta / base


def diff_timestamps(old_items: Dict[str, dict], new_items: Dict[str, dict]) -> list:
    """Items present in both snapshots whose last_changed differs. Carries
    each item's transliterated_bytes (the Atlas's meaningful "how much real
    text" figure -- see about.html's "Calculating Size") old/new for display
    as a size delta alongside the timestamp change."""
    changed = []
    for iid in sorted(set(old_items) & set(new_items)):
        old_ts = old_items[iid].get("last_changed")
        new_ts = new_items[iid].get("last_changed")
        if old_ts and new_ts and old_ts != new_ts:
            changed.append({
                "id": iid,
                "old": old_ts,
                "new": new_ts,
                "old_bytes": old_items[iid].get("transliterated_bytes", 0) or 0,
                "new_bytes": new_items[iid].get("transliterated_bytes", 0) or 0,
            })
    return changed


def added_removed(old_items: Dict[str, dict], new_items: Dict[str, dict]) -> Tuple[list, list]:
    """Items only in one snapshot. Added items carry new_bytes (old is
    implicitly 0); removed items carry old_bytes (new is implicitly 0) --
    same transliterated_bytes figure as diff_timestamps, for a consistent
    size-delta display across all three lists."""
    added = [
        {
            "id": iid,
            "date": new_items[iid].get("last_changed"),
            "new_bytes": new_items[iid].get("transliterated_bytes", 0) or 0,
        }
        for iid in sorted(set(new_items) - set(old_items))
    ]
    removed = [
        {
            "id": iid,
            "old_bytes": old_items[iid].get("transliterated_bytes", 0) or 0,
        }
        for iid in sorted(set(old_items) - set(new_items))
    ]
    return added, removed


def classify_changes(old: Dict[str, dict], new: Dict[str, dict]) -> dict:
    """Partition every item across two detailed rosters into what actually
    happened to it. Takes collect_items_detailed output (orphan bucket
    included), so an item is present in a roster whenever the wiki had it at
    all -- reachability is a property recorded on the item, not a reason to
    omit it.

    The partition is total over set(old) | set(new): each id lands in exactly
    one of added / removed / (present in both), and the crossings and
    recategorizations subdivide that last group. Everything left over is a
    plain modification, which diff_timestamps reports separately.

      added         id absent from old, present in new -- a real new page
      removed       id present in old, absent from new -- really deleted
      categorized   orphaned in old, centrally reachable in new: someone
                    filed it into the category tree. Curation, not growth.
      orphaned      the reverse -- fell out of the reachable tree
      recategorized centrally reachable in both, but under a different set
                    of categories

    Why this exists: `added`/`removed` alone conflate all five. Before this
    split, 488 शिवपुराणम् pages being categorized in 2026-09 read as 504
    "added" and, the month before, 486 falling out read as 487 "removed" --
    a -487/+504 round trip in the trend charts where the wiki gained and lost
    nothing. See notes on the About page's Deltas section.

    NOT detected: page moves/renames. The item id is the page's full title
    (process.py's `page:{title}`), so a rename is a different id and shows up
    as one `added` plus one `removed`. The dump carries no move log, so there
    is no signal to detect it with -- only a heuristic pairing on identical
    bytes, which is not attempted here."""
    old_ids, new_ids = set(old), set(new)

    def size(rec: dict) -> int:
        return rec["stats"].get("transliterated_bytes", 0) or 0

    added = [
        {"id": i, "date": new[i]["stats"].get("last_changed"), "new_bytes": size(new[i])}
        for i in sorted(new_ids - old_ids)
    ]
    removed = [{"id": i, "old_bytes": size(old[i])} for i in sorted(old_ids - new_ids)]

    categorized, orphaned, recategorized = [], [], []
    for i in sorted(old_ids & new_ids):
        was, now = old[i]["orphaned"], new[i]["orphaned"]
        if was and not now:
            categorized.append({
                "id": i,
                "date": new[i]["stats"].get("last_changed"),
                "new_bytes": size(new[i]),
                "to": sorted(new[i]["categories"]),
            })
        elif now and not was:
            orphaned.append({
                "id": i,
                "old_bytes": size(old[i]),
                "from": sorted(old[i]["categories"]),
            })
        elif not was and not now and old[i]["categories"] != new[i]["categories"]:
            recategorized.append({
                "id": i,
                "from": sorted(old[i]["categories"]),
                "to": sorted(new[i]["categories"]),
                "new_bytes": size(new[i]),
            })

    return {
        "added": added,
        "removed": removed,
        "categorized": categorized,
        "orphaned": orphaned,
        "recategorized": recategorized,
    }


def _stats_report(old_stats: dict, new_stats: dict) -> dict:
    """Builds the old/new/sizes/delta portion of a report for one stats pair
    (either root.stats -- central/ग्रन्थाः-only -- or the true, orphan-bucket-
    inclusive total). Shared so build_report can compute both without
    duplicating the count/text_count/size-delta arithmetic."""
    old_count = old_stats.get("count", 0) or 0
    new_count = new_stats.get("count", 0) or 0
    delta_count = new_count - old_count

    old_text_count = old_stats.get("text_count", 0) or 0
    new_text_count = new_stats.get("text_count", 0) or 0
    delta_text_count = new_text_count - old_text_count

    size_report = {}
    for key in STAT_KEYS:
        old_v = old_stats.get(key, 0) or 0
        new_v = new_stats.get(key, 0) or 0
        delta_v = new_v - old_v
        size_report[key] = {"old": old_v, "new": new_v, "delta": delta_v, "delta_pct": pct(delta_v, old_v)}

    return {
        "old": {**{k: old_stats.get(k) for k in STAT_KEYS}, "count": old_stats.get("count"),
                 "text_count": old_stats.get("text_count"),
                 "last_changed": old_stats.get("last_changed")},
        "new": {**{k: new_stats.get(k) for k in STAT_KEYS}, "count": new_stats.get("count"),
                 "text_count": new_stats.get("text_count"),
                 "last_changed": new_stats.get("last_changed")},
        "sizes": size_report,
        "delta": {
            "count": delta_count,
            "count_pct": pct(delta_count, old_count),
            "text_count": delta_text_count,
            "text_count_pct": pct(delta_text_count, old_text_count),
        },
    }


def build_report(old_path: Path, new_path: Path) -> dict:
    old_data = read_json_maybe_gz(old_path)
    new_data = read_json_maybe_gz(new_path)

    old_stats = old_data["root"].get("stats", {}) or {}
    new_stats = new_data["root"].get("stats", {}) or {}

    # all_stats (root + orphan bucket, the true total) is only present on
    # tree.json snapshots built after its introduction -- older cached
    # snapshots fall back to root.stats so pre-existing changelog entries
    # still get an "all" section, just identical to their central-only one.
    old_all_stats = old_data.get("all_stats") or old_stats
    new_all_stats = new_data.get("all_stats") or new_stats

    # One orphan-inclusive roster per side. classify_changes then splits every
    # item by what actually happened to it, so a page merely crossing the
    # orphan-bucket boundary is reported as categorized/orphaned rather than
    # as a spurious add/remove -- see its docstring.
    old_items = collect_items_detailed(old_data["root"])
    new_items = collect_items_detailed(new_data["root"])

    changes = classify_changes(old_items, new_items)

    # Timestamp diffs stay central-only: an item's last_changed is the same
    # wherever it sits, and the central roster is what the size/count deltas
    # above are drawn from.
    old_central = {i: r["stats"] for i, r in old_items.items() if not r["orphaned"]}
    new_central = {i: r["stats"] for i, r in new_items.items() if not r["orphaned"]}
    changed_ts = diff_timestamps(old_central, new_central)

    old_count = old_stats.get("count", 0) or 0

    added = changes["added"]
    removed = changes["removed"]
    categorized = changes["categorized"]
    orphaned = changes["orphaned"]
    recategorized = changes["recategorized"]

    report = _stats_report(old_stats, new_stats)
    report["all"] = _stats_report(old_all_stats, new_all_stats)
    report.update({
        "items_added": added,
        "items_removed": removed,
        "items_categorized": categorized,
        "items_orphaned": orphaned,
        "items_recategorized": recategorized,
        "items_with_changed_timestamp": changed_ts,
        "items_added_count": len(added),
        "items_removed_count": len(removed),
        "items_categorized_count": len(categorized),
        "items_orphaned_count": len(orphaned),
        "items_recategorized_count": len(recategorized),
        "items_changed_count": len(changed_ts),
        "items_added_pct": pct(len(added), old_count),
        "items_removed_pct": pct(len(removed), old_count),
        # Retained under their old names: these were already the
        # orphan-inclusive add/remove counts, which is exactly what `added`
        # and `removed` now mean.
        "items_added_count_all": len(added),
        "items_removed_count_all": len(removed),
    })
    return report


def print_summary(report: dict) -> None:
    o, n, d, sizes = report["old"], report["new"], report["delta"], report["sizes"]

    def fmt_pct(v):
        return "n/a" if v is None else f"{v:+.1f}%"

    print(f"old: count={o['count']!r} text_count={o['text_count']!r} last_changed={o['last_changed']!r}")
    print(f"new: count={n['count']!r} text_count={n['text_count']!r} last_changed={n['last_changed']!r}")
    print()
    for key in STAT_KEYS:
        s = sizes[key]
        print(f"{key}: {s['old']:,} -> {s['new']:,}  ({s['delta']:+,}, {fmt_pct(s['delta_pct'])})")
    print(f"count: {o['count']:,} -> {n['count']:,}  ({d['count']:+,}, {fmt_pct(d['count_pct'])})")
    if o['text_count'] is None or n['text_count'] is None:
        print("text_count: n/a (not tracked for one or both snapshots)")
    else:
        print(f"text_count: {o['text_count']:,} -> {n['text_count']:,}  ({d['text_count']:+,}, {fmt_pct(d['text_count_pct'])})")
    print()
    print(f"items added: {report['items_added_count']} ({fmt_pct(report['items_added_pct'])} of old total)")
    print(f"items removed: {report['items_removed_count']} ({fmt_pct(report['items_removed_pct'])} of old total)")
    print(f"items categorized (orphan bucket -> category tree): {report['items_categorized_count']}")
    print(f"items orphaned (category tree -> orphan bucket): {report['items_orphaned_count']}")
    print(f"items recategorized (within the category tree): {report['items_recategorized_count']}")
    print(f"items with changed last_changed timestamp: {report['items_changed_count']}")
    for entry in report["items_with_changed_timestamp"][:20]:
        print(f"  {entry['id']}: {entry['old']} -> {entry['new']}")
    if report["items_changed_count"] > 20:
        print(f"  ... and {report['items_changed_count'] - 20} more")

    all_report = report["all"]
    ao, an, ad = all_report["old"], all_report["new"], all_report["delta"]
    print()
    print("--- all (including असम्बद्धवर्गीकृतम्, the orphan bucket) ---")
    for key in STAT_KEYS:
        s = all_report["sizes"][key]
        print(f"{key}: {s['old']:,} -> {s['new']:,}  ({s['delta']:+,}, {fmt_pct(s['delta_pct'])})")
    print(f"count: {ao['count']:,} -> {an['count']:,}  ({ad['count']:+,}, {fmt_pct(ad['count_pct'])})")
    if ao['text_count'] is not None and an['text_count'] is not None:
        print(f"text_count: {ao['text_count']:,} -> {an['text_count']:,}  ({ad['text_count']:+,}, {fmt_pct(ad['text_count_pct'])})")
    print("(items added/removed above are already orphan-inclusive)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", type=Path, help="older tree.json snapshot")
    ap.add_argument("new", type=Path, help="newer tree.json snapshot")
    ap.add_argument("--append", action="store_true", help="append this comparison as an entry to the changelog")
    ap.add_argument("--changelog", type=Path, default=Path("docs/data/changelog.json"),
                     help="changelog path (default: docs/data/changelog.json)")
    ap.add_argument("--label", default="", help="short human label for this comparison")
    ap.add_argument("--notes", default="", help="free-text note describing what this comparison represents")
    args = ap.parse_args()

    report = build_report(args.old, args.new)
    print_summary(report)

    if args.append:
        if args.changelog.exists():
            log = json.loads(args.changelog.read_text())
        else:
            log = []
        next_id = max((e.get("id", 0) for e in log), default=0) + 1
        entry = {
            "id": next_id,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": args.label,
            "notes": args.notes,
            **report,
        }
        log.append(entry)
        args.changelog.parent.mkdir(parents=True, exist_ok=True)
        args.changelog.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
        print()
        print(f"appended entry to {args.changelog}")


if __name__ == "__main__":
    main()
