"""
Fetch stage: locate, download, verify, and decompress the monthly MediaWiki
Content File Export for sa.wikisource.org.

Source: https://dumps.wikimedia.org/other/mediawiki_content_current/sawikisource/<date>/xml/bzip2/
Generation starts monthly on the 1st; a run is complete once SHA256SUMS
(and _SUCCESS) exist at that date's directory. Discovery walks backward
from the current month since a run in progress won't have SHA256SUMS yet.

See notes/sawikisource-scraper-spec.md for the full pipeline design.
"""

from __future__ import annotations

import argparse
import bz2
import datetime
import hashlib
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

WIKI_ID = "sawikisource"
DATASET = "mediawiki_content_current"
BASE_URL = "https://dumps.wikimedia.org/other"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "dump" / "1_current_format_live"

USER_AGENT = (
    "sanskrit-wikisource-atlas/2.0 "
    "(https://github.com/tylergneill/sanskrit-wikisource-atlas; polite; research use)"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


@dataclass
class DumpExport:
    date: str  # ISO date string, e.g. "2026-07-01"
    dir_url: str  # directory URL for this export's xml/bzip2 listing
    part_files: dict[str, str]  # filename -> expected sha256 hex digest


def _dir_url(date_str: str) -> str:
    return f"{BASE_URL}/{DATASET}/{WIKI_ID}/{date_str}/xml/bzip2"


def find_export(date_str: str) -> DumpExport | None:
    """Check one exact month (e.g. "2026-05-01") for a complete export. Returns
    None if SHA256SUMS isn't present there (run incomplete or date has no export)."""
    dir_url = _dir_url(date_str)
    sums_url = f"{dir_url}/SHA256SUMS"
    resp = session.get(sums_url, timeout=30)
    if resp.ok:
        part_files = _parse_sha256sums(resp.text)
        if part_files:
            return DumpExport(date=date_str, dir_url=dir_url, part_files=part_files)
    return None


def find_latest_export(max_months_back: int = 6) -> DumpExport | None:
    """Walk backward month-by-month from today, returning the first complete export found."""
    today = datetime.date.today()
    d = today.replace(day=1)
    for _ in range(max_months_back):
        export = find_export(d.isoformat())
        if export is not None:
            return export
        d = _prev_month(d)
    return None


def _prev_month(d: datetime.date) -> datetime.date:
    return (d.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


def _parse_sha256sums(text: str) -> dict[str, str]:
    """Parse standard `sha256sum` output: '<hex digest>  <filename>' per line."""
    part_files = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, filename = line.partition("  ")
        if not digest or not filename:
            continue
        part_files[filename.strip()] = digest.strip()
    return part_files


def _sha256sum(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_stale_files(export: DumpExport, out_dir: Path) -> None:
    """Delete any file in out_dir left over from a prior (now-superseded) export,
    i.e. anything not among the current export's expected part files or their
    decompressed .xml counterparts."""
    if not out_dir.exists():
        return
    expected = set(export.part_files) | {Path(f).with_suffix("").name for f in export.part_files}
    for path in out_dir.iterdir():
        if path.is_file() and path.name not in expected:
            print(f"removing stale file from prior export: {path.name}", file=sys.stderr)
            path.unlink()


def download_and_verify(export: DumpExport, out_dir: Path, force: bool = False) -> list[Path]:
    """Download every part file listed in export.part_files into out_dir, verifying
    each against its published sha256 digest. Returns the list of verified paths.
    Skips re-downloading a file that's already present and already verifies correctly,
    unless force is set.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_files(export, out_dir)
    verified_paths = []
    for filename, expected_digest in export.part_files.items():
        dest = out_dir / filename
        if dest.exists():
            if _sha256sum(dest) == expected_digest:
                if force:
                    print(f"already present and verified, but --force set: re-downloading: {filename}",
                          file=sys.stderr)
                else:
                    print(f"already present, verified: {filename}", file=sys.stderr)
                    verified_paths.append(dest)
                    continue
            else:
                print(f"present but hash mismatch, re-downloading: {filename}", file=sys.stderr)

        url = f"{export.dir_url}/{filename}"
        print(f"downloading: {url}", file=sys.stderr)
        with session.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            written = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = 100 * written / total
                        print(f"\r  {filename}: {written / 1e6:.1f}/{total / 1e6:.1f} MB ({pct:.0f}%)",
                              end="", file=sys.stderr)
            print(file=sys.stderr)

        actual_digest = _sha256sum(dest)
        if actual_digest != expected_digest:
            raise ValueError(
                f"sha256 mismatch for {filename}: expected {expected_digest}, got {actual_digest}"
            )
        print(f"verified: {filename}", file=sys.stderr)
        verified_paths.append(dest)

    return verified_paths


def _decompress(bz2_path: Path, force: bool = False) -> Path:
    """Decompress a .bz2 dump part into a sibling .xml file. Skips re-decompressing
    if the .xml already exists and is at least as new as the .bz2, unless force is set."""
    xml_path = bz2_path.with_suffix("")
    if xml_path.exists() and not force:
        if xml_path.stat().st_mtime >= bz2_path.stat().st_mtime:
            print(f"already decompressed: {xml_path.name}", file=sys.stderr)
            return xml_path
    print(f"decompressing: {bz2_path.name}", file=sys.stderr)
    tmp_path = xml_path.with_suffix(".xml.tmp")
    with bz2.open(bz2_path, "rb") as src, open(tmp_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp_path.rename(xml_path)
    print(f"decompressed: {xml_path.name}", file=sys.stderr)
    return xml_path


def fetch(
    out_dir: Path = DEFAULT_OUT_DIR,
    max_months_back: int = 6,
    force: bool = False,
    date: str | None = None,
) -> list[Path]:
    """Resolve an available export online (the latest, or a specific month if `date`
    is given), compare it against what's already verified in out_dir, download/verify
    whatever's missing or stale, and decompress each verified part into a sibling .xml
    file. Deletes any leftover files from a prior, now-superseded export in the same
    out_dir. If everything is already present, verified, and decompressed, this is a
    no-op unless force is set.
    """
    if date is not None:
        export = find_export(date)
        if export is None:
            raise RuntimeError(f"no complete {DATASET} export found for {WIKI_ID} at {date}")
    else:
        export = find_latest_export(max_months_back=max_months_back)
        if export is None:
            raise RuntimeError(
                f"no complete {DATASET} export found for {WIKI_ID} in the last {max_months_back} months"
            )
    print(f"export: {export.date} ({len(export.part_files)} part file(s))", file=sys.stderr)
    bz2_paths = download_and_verify(export, out_dir, force=force)
    return [_decompress(p, force=force) for p in bz2_paths]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"directory to download into (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--max-months-back", type=int, default=6,
                         help="how many months back to search for a complete export (default: 6)")
    parser.add_argument("--force", action="store_true",
                         help="re-download and re-verify every part file even if already present "
                              "and verified locally")
    parser.add_argument("--date", type=str, default=None,
                         help="fetch one specific month's export (e.g. 2026-05-01) instead of "
                              "the latest available; errors if that month's export isn't complete")
    args = parser.parse_args()

    paths = fetch(out_dir=args.out_dir, max_months_back=args.max_months_back, force=args.force, date=args.date)
    for p in paths:
        print(p)


if __name__ == "__main__":
    main()
