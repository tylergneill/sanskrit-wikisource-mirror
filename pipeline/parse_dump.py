"""
Build stage, part 1: stream-parse the MediaWiki Content File Export XML into
per-namespace page records.

The dump is one large XML file (~1.8GB uncompressed for sawikisource) with a
flat sequence of <page> elements -- no nesting between them -- so this uses
iterparse to process one <page> at a time and discard it, rather than
building an in-memory tree of the whole file.

See notes/sawikisource-scraper-spec.md for the pipeline design and
notes/wikisource-ontology.md for what is/isn't stored vs. derived.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

DEFAULT_DUMP_GLOB = "sawikisource-*.xml"

# Same maintenance/junk categories excluded in scrape.py -- these are
# Wikisource housekeeping categories, not real subject classification.
# ग्रन्थकर्तारः (author index), वर्गनिर्वहणम् (category-maintenance meta-tree), and
# विकिस्रोतः (Wikisource-project meta pages) are top-level siblings of ग्रन्थाः
# under root but carry no actual Sanskrit-text content -- excluded per explicit
# user direction (this Atlas favors a useful interface over slavish adherence
# to Wikisource's own category structure). Formatting templates/Index
# Validated/Works/Works by year (English-named ProofreadPage/import
# housekeeping categories) and टेम्पलेट लूप वाले पेज / अनुक्रमणिका - Unknown
# progress (Hindi/Devanagari MediaWiki-software maintenance categories) are
# the same kind of junk, just not Sanskrit-subject-classification-shaped
# enough to have been caught by the original list.
EXCLUDED_CATEGORIES = {
    "अवैध एचटीएमएल टैग का उपयोग कर रहे पृष्ठ",
    "अनिर्दिष्टानि पुटानि",
    "निष्कासनाय",
    "वर्गनिर्वहणम्",
    "विकिस्रोतः",
    "Formatting templates",
    "Index Validated",
    "Works",
    "Works by year",
    "टेम्पलेट लूप वाले पेज",
    "अनुक्रमणिका - Unknown progress",
    "Candidates for speedy deletion",
    "Delete",
    "PD-old",
    "अनुक्रमणिका फलकानि",
    "असूचकांकितानि पृष्ठानि",
    "निर्वाचितलेखाः",
    "विकिपीडियालेखः",
}

# Substring-matched exclusion: any category whose title contains one of
# these anywhere (not just an exact/prefix match) is treated as the same
# kind of Wikisource-project housekeeping as the exact-match entries above.
# विकिस्रोत catches variants like विकिस्रोतसः निर्वहणम्, विकिस्रोतःकार्यक्रमः, and
# भारतीय-विकिस्रोतः-पाठशुद्धिस्पर्धा (the marker appears mid-title, joined by
# hyphens) that an exact-match set would miss. ग्रन्थकर्तारः (also listed
# exactly above, since it's excluded on its own) additionally catches its
# alphabetical-index children (ग्रन्थकर्तारः-क, etc.). अनुसरणवर्गाः is a
# MediaWiki tracking-category tree (पुटानुसरणवर्गाः, प्राकृतानुसरणवर्गाः, ...),
# not real subject classification -- the substring form excludes the whole
# subtree at once. पाठशुद्धिस्पर्धा (proofreading-contest event categories,
# e.g. "पाठशुद्धिस्पर्धा आगस्ट् 2021") recur under several
# hyphenated/differently-spelled event-name prefixes (भारतीय-विकिसोर्स्-...,
# भारतीय-विकिस्रोतः-...) -- catching the shared word is more robust than
# listing every year's event title exactly.
EXCLUDED_CATEGORY_SUBSTRINGS = (
    "विकिस्रोत",
    "ग्रन्थकर्तारः",
    "अनुसरणवर्गाः",
    "पाठशुद्धिस्पर्धा",
)

# Matches [[वर्गः:Title]] or [[वर्गः:Title|sortkey]] category links in wikitext.
# The namespace-local category prefix is read from siteinfo (see NamespaceMap),
# not hardcoded, since it varies (here "वर्गः") -- this pattern is built per-dump.


@dataclass
class PageRecord:
    pageid: int
    ns: int
    title: str
    redirect_target: str | None
    text: str
    timestamp: str
    revid: int


@dataclass
class DumpIndex:
    site_name: str
    namespaces: dict[int, str]  # ns id -> localized namespace name (e.g. 14 -> "वर्गः")
    pages_by_ns: dict[int, list[PageRecord]] = field(default_factory=dict)

    def category_ns_id(self) -> int:
        return self._find_ns_id("वर्गः", fallback=14)

    def index_ns_id(self) -> int | None:
        # Unlike category_ns_id/template_ns_id (core MediaWiki namespaces
        # present since forever), Index (106) is the ProofreadPage extension
        # -- genuinely absent from early sawikisource dumps (e.g. 2011-10),
        # since the extension wasn't enabled yet. Returns None rather than
        # raising in that case; callers must treat None as "no such
        # namespace, zero records" rather than an error.
        return self._find_ns_id_optional("अनुक्रमणिका", fallback=106)

    def page_ns_id(self) -> int | None:
        # Same ProofreadPage-extension caveat as index_ns_id (Page, 104).
        return self._find_ns_id_optional("पृष्ठम्", fallback=104)

    def template_ns_id(self) -> int:
        return self._find_ns_id("फलकम्", fallback=10)

    def author_ns_id(self) -> int | None:
        for ns_id, name in self.namespaces.items():
            if name == "लेखकः":
                return ns_id
        return None

    def _find_ns_id(self, localized_name: str, fallback: int) -> int:
        for ns_id, name in self.namespaces.items():
            if name == localized_name:
                return ns_id
        if fallback in self.namespaces:
            print(
                f"warning: namespace '{localized_name}' not found by name, "
                f"falling back to id {fallback}",
                file=sys.stderr,
            )
            return fallback
        raise ValueError(f"could not resolve namespace '{localized_name}' and fallback id {fallback} absent from siteinfo")

    def _find_ns_id_optional(self, localized_name: str, fallback: int) -> int | None:
        for ns_id, name in self.namespaces.items():
            if name == localized_name:
                return ns_id
        if fallback in self.namespaces:
            print(
                f"warning: namespace '{localized_name}' not found by name, "
                f"falling back to id {fallback}",
                file=sys.stderr,
            )
            return fallback
        print(
            f"note: namespace '{localized_name}' (fallback id {fallback}) not present in this "
            f"dump's siteinfo -- treating as genuinely absent (0 records), not an error",
            file=sys.stderr,
        )
        return None


def _localname(tag: str) -> str:
    """Strip the XML namespace URI prefix ET leaves on tag names, e.g.
    '{http://www.mediawiki.org/xml/export-0.11/}page' -> 'page'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_dump(xml_path: Path, namespaces_of_interest: set[int] | None = None) -> DumpIndex:
    """Stream-parse the dump XML. If namespaces_of_interest is given, pages
    outside that set are counted but not retained (keeps memory bounded when
    only a subset -- e.g. Main + Category + Index -- is needed)."""
    context = ET.iterparse(str(xml_path), events=("start", "end"))
    _, root = next(context)  # root <mediawiki> element, "start" event

    from pipeline.progress import LiveCounter, to_iast

    site_name: str | None = None
    namespaces: dict[int, str] = {}
    dump_index: DumpIndex | None = None
    skipped = 0
    kept = 0
    # No fixed total is known up front (the dump's page count isn't in
    # siteinfo) -- LiveCounter falls back to a rate/elapsed-only display,
    # same as scrape.py's SimpleStatusLine for non-category-tree phases.
    counter = LiveCounter("parsing dump")

    current_page: dict | None = None
    current_revision: dict | None = None
    in_siteinfo = False
    in_namespaces = False

    for event, elem in context:
        tag = _localname(elem.tag)

        if event == "start":
            if tag == "siteinfo":
                in_siteinfo = True
            elif tag == "namespaces" and in_siteinfo:
                in_namespaces = True
            elif tag == "page":
                current_page = {"redirect_target": None}
            elif tag == "revision" and current_page is not None:
                current_revision = {}
            continue

        # event == "end"
        if tag == "sitename" and in_siteinfo:
            site_name = elem.text or ""
        elif tag == "namespace" and in_namespaces:
            ns_id = int(elem.get("key"))
            namespaces[ns_id] = elem.text or ""
        elif tag == "namespaces":
            in_namespaces = False
        elif tag == "siteinfo":
            in_siteinfo = False
            dump_index = DumpIndex(site_name=site_name or "", namespaces=namespaces)
            if namespaces_of_interest is None:
                namespaces_of_interest = {
                    ns_id for ns_id in (
                        dump_index.category_ns_id(),
                        dump_index.index_ns_id(),
                        dump_index.template_ns_id(),
                        dump_index.page_ns_id(),
                        0,  # Main
                    ) if ns_id is not None
                }
            for ns_id in namespaces_of_interest:
                dump_index.pages_by_ns.setdefault(ns_id, [])
        elif tag == "redirect" and current_page is not None:
            current_page["redirect_target"] = elem.get("title")
        elif tag == "title" and current_page is not None and "title" not in current_page:
            current_page["title"] = elem.text or ""
        elif tag == "ns" and current_page is not None and "ns" not in current_page:
            current_page["ns"] = int(elem.text)
        elif tag == "id" and current_page is not None and current_revision is None and "pageid" not in current_page:
            current_page["pageid"] = int(elem.text)
        elif current_revision is not None:
            if tag == "id" and "revid" not in current_revision:
                current_revision["revid"] = int(elem.text)
            elif tag == "timestamp":
                current_revision["timestamp"] = elem.text or ""
            elif tag == "text":
                current_revision["text"] = elem.text or ""
            elif tag == "revision":
                current_page["revision"] = current_revision
                current_revision = None
        elif tag == "page":
            ns = current_page.get("ns", 0)
            if dump_index is not None and ns in namespaces_of_interest:
                rev = current_page.get("revision", {})
                record = PageRecord(
                    pageid=current_page.get("pageid", 0),
                    ns=ns,
                    title=current_page.get("title", ""),
                    redirect_target=current_page.get("redirect_target"),
                    text=rev.get("text", ""),
                    timestamp=rev.get("timestamp", ""),
                    revid=rev.get("revid", 0),
                )
                dump_index.pages_by_ns[ns].append(record)
                kept += 1
                counter.update(detail=to_iast(record.title))
            else:
                skipped += 1
                counter.update()
            current_page = None
            root.clear()

    counter.close()

    if dump_index is None:
        raise ValueError("dump had no <siteinfo>; cannot resolve namespaces")

    print(f"parsed dump: {kept} pages kept, {skipped} pages skipped (other namespaces)", file=sys.stderr)
    return dump_index


def category_links(text: str, category_ns_name: str) -> list[str]:
    """Extract category titles from [[<category_ns_name>:Title]] or
    [[<category_ns_name>:Title|sortkey]] wikilinks in raw wikitext.
    Case of the namespace prefix's first letter is not sensitive (MediaWiki
    namespaces are first-letter-case-insensitive), but this project's data
    is consistently Devanagari so no ASCII case-folding is needed here.
    """
    pattern = re.compile(
        r"\[\[\s*" + re.escape(category_ns_name) + r"\s*:\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]"
    )
    return [m.group(1).strip() for m in pattern.finditer(text)]


def is_excluded_category(title: str) -> bool:
    """title may be a bare category title (from a [[वर्गः:Title]] link) or a
    full page title still carrying its namespace prefix (e.g. 'वर्गः:Title',
    as found on Category-namespace PageRecord.title) -- strip the prefix
    before comparing either way."""
    bare = title.strip()
    if ":" in bare:
        bare = bare.split(":", 1)[1].strip()
    if bare in EXCLUDED_CATEGORIES:
        return True
    return any(s in bare for s in EXCLUDED_CATEGORY_SUBSTRINGS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, nargs="?",
                         help="path to the uncompressed dump XML (default: autodetect in dump/)")
    args = parser.parse_args()

    xml_path = args.xml_path
    if xml_path is None:
        # rglob, not glob: the exports live one level down, in the era folder
        # `1_current_format_live/`. A flat glob on data/dump/ matched nothing
        # and this fallback never fired.
        candidates = sorted(Path("data/dump").rglob(DEFAULT_DUMP_GLOB))
        candidates = [p for p in candidates if p.suffix == ".xml"]
        if not candidates:
            print("no data/dump/**/*.xml found; run `make refresh-dump`, "
                  "or pass a path explicitly", file=sys.stderr)
            sys.exit(1)
        xml_path = candidates[0]

    print(f"parsing {xml_path}", file=sys.stderr)
    dump_index = parse_dump(xml_path)
    print(f"site: {dump_index.site_name}", file=sys.stderr)
    print(f"category ns: {dump_index.category_ns_id()} ({dump_index.namespaces[dump_index.category_ns_id()]})", file=sys.stderr)
    index_ns_id = dump_index.index_ns_id()
    index_ns_label = dump_index.namespaces[index_ns_id] if index_ns_id is not None else "absent from this dump"
    print(f"index ns: {index_ns_id} ({index_ns_label})", file=sys.stderr)
    for ns_id, records in dump_index.pages_by_ns.items():
        print(f"  ns {ns_id}: {len(records)} pages", file=sys.stderr)


if __name__ == "__main__":
    main()
