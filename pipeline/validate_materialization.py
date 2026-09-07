"""Validate pipeline/materialize_snapshots.py's reconstruction accuracy by
materializing one or more months for which a real, full-dump-derived
snapshot already exists on disk (e.g. 2022-05-01 from Internet Archive,
2025-11-01 from the live rolling window -- the two boundary months flanking
MATERIALIZED_MONTHS), then diffing materialized-vs-real via
pipeline.compare.build_report. Confirmed on 2026-07-21: both boundary
months came within ~0.5-0.6% on every count/size metric, well within the
known deviations documented in materialize_snapshots.py's docstring (pages
deleted/renamed after the meta-history dump was taken, revision-cutoff
boundary effects).

Kept in the repo (not run automatically by anything) in case the
materialization method, the meta-history dump, or this project's parsing
pipeline ever changes enough to warrant re-checking.

Usage:
    python -m pipeline.validate_materialization --dates 2022-05-01,2025-11-01
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipeline.backfill import (
    DEFAULT_MATERIALIZE_SRC_DIR,
    DEFAULT_SNAPSHOT_DIR,
    _existing_snapshot_path,
    _ensure_materialized_month,
    process_dump,
)
from pipeline.compare import build_report, print_summary

DEFAULT_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "data" / "dump" / "_materialize_validation"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dates", required=True,
                     help="comma-separated YYYY-MM-01 dates to validate, each of which must already "
                          "have a real snapshot at --snapshot-dir/tree-<date>.json[.gz] to diff against")
    ap.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR,
                     help="where real (non-materialized) tree-<date>.json[.gz] snapshots already live")
    ap.add_argument("--validation-dir", type=Path, default=DEFAULT_VALIDATION_DIR,
                     help="scratch directory for materialized XML, processed snapshots, and reports "
                          "(gitignored; not cleaned up automatically, since these runs are infrequent "
                          "and it's useful to inspect the reports afterward)")
    ap.add_argument("--materialize-src-dir", type=Path, default=DEFAULT_MATERIALIZE_SRC_DIR,
                     help="where the cached meta-history dump lives / gets downloaded to")
    ap.add_argument("--workers", type=int, default=None, help="worker processes for content-size computation")
    args = ap.parse_args()

    dates = [d.strip() for d in args.dates.split(",")]
    args.validation_dir.mkdir(parents=True, exist_ok=True)

    # Materialize each date into its own subdir of validation_dir (not
    # backfill.py's own DEFAULT_MATERIALIZED_ROOT) so a validation run never
    # collides with, or gets cleaned up by, a real backfill run using the
    # same date.
    for date_str in dates:
        print(f"\n=== {date_str} ===")
        materialized_path = args.validation_dir / f"tree-{date_str}.materialized.json"
        if materialized_path.exists():
            print(f"reusing already-processed {materialized_path}")
        else:
            xml_path = _ensure_materialized_month(date_str, args.validation_dir, args.materialize_src_dir)
            print(f"processing materialized dump: {xml_path}")
            tree_json, _content_cache = process_dump(xml_path, workers=args.workers)  # already {"root": ...}
            materialized_path.write_text(json.dumps(tree_json), encoding="utf-8")
            print(f"wrote {materialized_path}")

        real_path = _existing_snapshot_path(date_str, args.snapshot_dir)
        if real_path is None:
            print(f"!! no real snapshot found for {date_str} under {args.snapshot_dir} -- skipping comparison",
                  file=sys.stderr)
            continue

        print(f"comparing materialized vs real ({real_path})...")
        report = build_report(real_path, materialized_path)
        print_summary(report)

        out_report_path = args.validation_dir / f"report-{date_str}.json"
        out_report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {out_report_path}")


if __name__ == "__main__":
    main()
