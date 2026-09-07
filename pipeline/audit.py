"""
Audit stage: surfaces likely breadcrumb and category structural problems on
the live wiki for a human to review and fix directly on sa.wikisource.org --
this tool never mutates wiki content, the dump, or docs/data/tree.json, and
never applies any inference/correction of its own. See
notes/wikisource-editing-plan.md for the editing campaign this feeds.

Nine independent checks, run over a single dump:

1. Breadcrumb-gap candidates: a top-level Main page T ("तन्त्रालोकः") that
   has other top-level pages sharing its title as a space-separated prefix
   ("तन्त्रालोकः अष्टममाह्निकम्") instead of a real "/"-delimited MediaWiki
   subpage title ("तन्त्रालोकः/अष्टममाह्निकम्"). Requiring the prefix ITSELF
   to be a real page title (not just any shared leading words) keeps the
   false-positive rate low -- this is meant to catch "should have used
   subpage syntax but didn't", not coincidental title overlap. Candidates
   are grouped and counted per work, never auto-renamed.

2. Root category-inference candidates: pipeline.transclusion.infer_root_categories,
   wired up here as a detector (not silent inference -- see
   notes/wikisource-editing-plan.md, "A related, adjacent finding: dead
   category-inference code"). Flags a top-level page whose real "/" subpages
   ALL share a category tag the top-level page itself lacks, for a human to
   add the tag upstream on the wiki.

3. Orphaned categories: a real Category-namespace page with zero parent tags
   of its own (other than the root itself) -- see
   pipeline.build_tree.orphaned_category_titles. The category-graph-level
   sibling of the Main-namespace orphan bucket (असम्बद्धवर्गीकृतम्): the
   category page exists, it's just never filed under anything, so nothing
   tagged into it is reachable from वर्गसर्वस्वम् either.

4. Red-link categories: a category named as a parent/child via [[वर्गः:X]]
   somewhere (pipeline.build_tree.CategoryGraph node with record=None), OR
   tagged directly onto a Main/Index-namespace page, where X was never
   itself created as a real Category-namespace page -- see
   notes/orphan-bucket-vs-orphaned-categories.md for why the direct-tag case
   needs its own separate scan (it never becomes a CategoryGraph node at
   all, so it's invisible to the graph-only check). Not fixable by
   re-tagging -- the category page itself needs to be created on-wiki.

5. Category cycles: the About page (docs/about.html, "Categories: Another
   Graph") explicitly warns categorization is manual and "there can even be
   cycles" -- a category graph is supposed to be a tree, so a directed cycle
   here is always a mis-tagging. Detected via standard DFS
   white/gray/black coloring over CategoryGraph.

6. Page-namespace (पृष्ठम्) items carrying their own direct category tag --
   against the site's stated convention (docs/about.html, "OCR
   'Proofreading' Pipeline Types": "Index items can be labeled with
   Categories, but Pages should not be", since Pages are numerous enough per
   book that showing them all under a Category would be unusable).

7. Multi-parented categories: a real Category-namespace page filed under
   more than one parent via [[वर्गः:X]]. Not a defect in the Atlas -- the
   frontend already handles this correctly, rendering every occurrence as
   independently selectable with a "see also" pointer to its siblings (see
   docs/about.html, "Cross-tree Connections") -- but it usually reflects
   imprecise categorization upstream on the wiki (the category arguably
   belongs under only one of its listed parents), worth surfacing here for
   the same editing campaign as the other checks.

8. External bulk-import candidates: a Main-namespace page whose external
   links are dominated by (or which carries an explicit source-attribution
   header pointing to) a known digitized-Sanskrit-text repository (GRETIL,
   sanskritdocuments.org, muktalib7.com, SARIT, etc.) -- i.e. likely
   copy-pasted wholesale from elsewhere rather than transcribed/composed on
   sa.wikisource itself. Not a defect to fix in the usual sense (attribution
   is good practice, not a bug), but worth surfacing since it's part of
   understanding how much of the corpus is original-to-the-wiki vs. an
   unreviewed import -- see notes/wikisource-editing-plan.md. Deliberately
   excludes domains found to be inline citation/cross-reference targets
   scattered across many otherwise-original pages (e.g. avg-sanskrit.org
   Panini-sutra links, or the puranastudy.*/vedastudy.* footnote-mirror
   farm) -- those aren't "this whole page came from there" signals.

9. Broken Commons transclusions: an Index item transcluded into Main content
   with real पृष्ठम्:Title/N leaf content in the dump, whose backing scan
   file has been deleted/lost from Commons -- so the live wiki page renders
   completely empty (ProofreadPage's rendering depends on the file, not the
   already-stored leaf wikitext) even though the Atlas shows real content,
   since it reads leaf wikitext directly from the dump. See
   notes/broken-transclusion-audit-research.md for the discovery (जातकपद्धतिः)
   and why this is NOT detectable from the dump alone -- the file's absence
   has zero footprint in stored wikitext, and MediaWiki's own live
   maintenance category for it is injected at render time. The only check
   in this module that hits the network: one batched Commons `action=query`
   call per <=50 candidate Index titles, with retry/backoff since Commons
   rate-limits back-to-back requests (confirmed live: HTTP 429).

Usage:
    python -m pipeline.audit [xml_path]
    (defaults to the newest dump/sawikisource-*.xml, same convention as
    pipeline.process)
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from pipeline.build_tree import (
    FLAT_FAMILY_PATTERNS,
    CategoryGraph,
    MainPageNode,
    build_category_graph,
    build_main_tree,
    orphaned_category_titles,
)
from pipeline.parse_dump import PageRecord, is_excluded_category, parse_dump
from pipeline.process import _owning_index_title
from pipeline.progress import to_iast
from pipeline.transclusion import build_transclusion_map, direct_categories, infer_root_categories, is_transcluded

# Domains confirmed (by manual spot-check against the 2026-07-01 and 2026-08-01
# dumps) to host machine-readable digitized Sanskrit text corpora, as opposed to
# sites that merely get cited inline (avg-sanskrit.org's per-sutra
# cross-references) or rehosted across many free-hosting mirrors as footnote
# targets (the puranastudy.*/vedastudy.* cluster) -- neither of those is a
# "whole page copy-pasted from here" signal, so they're deliberately excluded.
#
# This is a hardcoded allowlist, so it is only ever as good as its last audit.
# Re-derive it, don't extend it from memory: collect every domain appearing in a
# Main-namespace page that has a _SOURCE_HEADER_RE section, rank by page count,
# and inspect the wikitext around the header before adding a row. The bar is
# that the link resolves to the *text itself* (a .txt/.htm/.xml full text or a
# corpus entry page), not to a scan, a tool, or companion media.
#
# Deliberately excluded after inspection against the 2026-08-01 dump, each of
# which a naive "appears under a source header" rule would wrongly admit:
#   sanskrit.github.io (531 pages) -- by far the most frequent domain on the
#     wiki, and not a source at all: every hit is the same Ramayana *audio
#     recording* index, credited to its reciters, pasted into each sarga.
#     Admitting it would bury the real signal under 2x its volume in noise.
#   scriptoq.com (11) -- the diCrunch IAST->Devanagari conversion *tool*. The
#     pages say so outright; their actual source is GRETIL, already covered.
#   archive.org / ia*.us.archive.org (13) -- scan and PDF hosting, cited as
#     bibliography rather than copy-pasted as machine-readable text.
#   vishvasa.github.io (2) -- genuinely ambiguous: one page sources from
#     sanskritworld.in and only lists vishvasa under "see also". Left out to
#     keep the bar at "confirmed", not "plausible".
BULK_TEXT_REPO_DOMAINS = {
    "gretil.sub.uni-goettingen.de", "sub.uni-goettingen.de", "detu.sub.uni-goettingen.de",
    "sanskritdocuments.org", "sarit.indology.info",
    "kjc-fs-cluster.kjc.uni-heidelberg.de",
    "muktalib7.com", "tipitakapali.org", "glossaries.dila.edu.tw",
    "titus.uni-frankfurt.de",
    # Added 2026-08-07 from the survey described above; each verified to link
    # directly at full text rather than at a scan/tool/media page.
    "granthamandira.net",       # Gaudiya Grantha Mandira, ?show=entry&e_no=NNN
    "peterffreund.com",         # Vedic Literature collection, direct .txt/.htm
    "sanskritworld.in",         # direct .txt book downloads
    "sanskrit.uohyd.ac.in",     # UoH CIIL corpus, direct .txt/.html
    "guruguha.org",             # music-theory texts, *_roman.txt/*_devnag.txt
}

_URL_RE = re.compile(r"https?://[^\s\]\|\}\)]+")
# Section headers used on-wiki to attribute a page's source. The spelling is not
# standardized, so this matches the real variants rather than one citation form:
# स्रोतः is much the most common (716 sitewide), but स्रोत (25), स्रोतम् (20) and
# Sources (2) are all in genuine use and were silently missed while this matched
# only the visarga form -- which is how the Gaudiya Grantha Mandira page
# पातञ्जलयोगदर्शनम् ... (==स्रोतम्==) escaped the audit entirely. The heading level
# is `={2,}` rather than a literal `==` for the same reason: `=== ... ===` and
# padded forms are both attested.
_SOURCE_HEADER_RE = re.compile(
    r"={2,}\s*(स्रोतः|स्रोतम्|स्रोतस्|स्रोत|मूलपाठः|आधारः|Sources?)\s*={2,}",
    re.IGNORECASE,
)


def find_breadcrumb_gap_candidates(
    main_nodes: dict[str, MainPageNode],
) -> dict[str, list[str]]:
    """Returns {top-level title: [other top-level titles sharing it as a
    space-separated prefix]}, for top-level titles that have at least one
    such candidate. Redirects are excluded on both sides -- a redirect
    sharing a prefix is noise (an alternate name/typo target), not a
    mis-titled chapter."""
    top_level_titles = sorted(
        title for title, node in main_nodes.items()
        if node.parent_title is None and node.record.redirect_target is None
    )

    candidates: dict[str, list[str]] = defaultdict(list)
    for prefix in top_level_titles:
        prefix_space = prefix + " "
        for other in top_level_titles:
            if other != prefix and other.startswith(prefix_space):
                candidates[prefix].append(other)

    return {k: v for k, v in candidates.items() if v}


def find_unresolvable_slash_paths(
    main_nodes: dict[str, MainPageNode],
) -> dict[str, list[str]]:
    """Returns {first path segment: [full titles]} for non-redirect pages that
    DO use "/" subpage syntax but whose breadcrumb reaches no existing page at
    any level -- not the immediate parent, not any higher ancestor, not via
    any redirect, not after whitespace normalization (see
    build_tree._resolve_ancestor, which tries all of those before giving up).

    These are the residue after real parenting: unlike a missing intermediate
    level, there is nothing on-wiki to nest under, so the page stays top-level
    and counts as its own text. The fix is upstream -- create the missing
    ancestor page, or retitle the page under one that exists. Grouped by first
    segment because a single missing root usually strands a whole run of
    chapters at once."""
    stranded: dict[str, list[str]] = defaultdict(list)
    for title, node in main_nodes.items():
        if node.parent_title is not None or "/" not in title:
            continue
        if node.record.redirect_target is not None:
            continue
        stranded[title.split("/", 1)[0].strip()].append(title)
    return {k: sorted(v) for k, v in stranded.items()}


# A flat title split by a non-"/" separator: "स्तेम-०१-भागः", "Work.12".
# The separator must sit between non-empty text on both sides.
_SEPARATOR_FAMILY_RE = re.compile(r"^(?P<stem>.+?)\s*[-–.]\s*(?P<rest>\S.*)$")


def find_separator_family_candidates(
    main_nodes: dict[str, MainPageNode],
) -> dict[str, list[str]]:
    """Returns {stem title: [full titles]} for flat (no "/") top-level pages
    that look like subpages of an existing work but use a hyphen or dot where
    "/" belongs -- e.g. पञ्चतन्त्रम् ०१, whose stem पञ्चतन्त्रम् is a real
    page. Requires the stem to resolve to an actual page and the family to
    have at least 2 members, so an ordinary hyphenated title isn't dragged in
    on its own.

    Complements find_breadcrumb_gap_candidates, which only detects the
    SPACE-separated form -- between them they cover the separator conventions
    actually in use. Reported only: unlike a real "/" breadcrumb, a hyphen
    carries no structural meaning on MediaWiki, so inferring nesting from it
    would be a naming-convention guess. These pages stay flat in tree.json and
    keep counting as separate texts until they're moved on-wiki (see
    notes/wikisource-editing-plan.md -- a page MOVE, not a redirect).

    The two families the pipeline DOES nest (महाभारतम्, ऋग्वेदः सूक्तं -- see
    build_tree.FLAT_FAMILY_PATTERNS) drop out here for free: they now have a
    parent_title, so the top-level filter below already skips them. Their own
    liveness is checked separately by check_flat_family_allowlist."""
    families: dict[str, list[str]] = defaultdict(list)
    for title, node in main_nodes.items():
        if node.parent_title is not None or "/" in title:
            continue
        if node.record.redirect_target is not None:
            continue
        m = _SEPARATOR_FAMILY_RE.match(title)
        if not m:
            continue
        stem = m.group("stem").strip()
        stem_node = main_nodes.get(stem)
        if stem_node is None or stem_node.record.redirect_target is not None:
            continue
        families[stem].append(title)
    return {k: sorted(v) for k, v in families.items() if len(v) >= 2}


def check_flat_family_allowlist(
    main_nodes: dict[str, MainPageNode],
) -> list[str]:
    """Returns a list of human-readable problems with
    build_tree.FLAT_FAMILY_PATTERNS, empty when every row is healthy.

    The allowlist is the pipeline's one deliberate departure from
    breadcrumb-first parenting, so it needs to fail LOUDLY rather than
    silently, in both directions:

    - A row matching ZERO pages means the family was renamed or cleaned up
      on-wiki and the row is now dead weight -- remove it.
    - A row whose matches don't all land on a real destination means pages
      are quietly falling back to top-level (see _resolve_flat_family, which
      refuses to synthesize a parent), inflating text_count again with no
      other signal that anything changed.

    Checked against the dump being audited, so it tracks the wiki rather than
    whatever was true when the row was written."""
    problems: list[str] = []
    for pattern, to_parent, label in FLAT_FAMILY_PATTERNS:
        matched = 0
        unparented: list[str] = []
        destinations: set[str] = set()
        for title, node in main_nodes.items():
            if "/" in title or not pattern.match(title):
                continue
            matched += 1
            if node.parent_title is None:
                unparented.append(title)
            else:
                destinations.add(node.parent_title)
        if matched == 0:
            problems.append(f"{label}: matches 0 pages -- row is dead, remove it")
            continue
        if unparented:
            sample = ", ".join(sorted(unparented)[:3])
            problems.append(
                f"{label}: {len(unparented)} of {matched} matched pages have no real "
                f"destination page and fell back to top-level (e.g. {sample})"
            )
        for dest in sorted(destinations):
            dest_node = main_nodes.get(dest)
            if dest_node is None:
                problems.append(f"{label}: destination {dest!r} is not a Main page")
            elif dest_node.record.redirect_target is not None:
                problems.append(
                    f"{label}: destination {dest!r} is a redirect -> "
                    f"{dest_node.record.redirect_target!r}"
                )
    return problems


def find_root_inference_candidates(
    main_nodes: dict[str, MainPageNode],
    direct_cats_by_title: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Returns {top-level title with real "/" subpages: set of categories
    every one of its subpages shares that the top-level page itself lacks}."""
    candidates: dict[str, set[str]] = {}
    for title, node in main_nodes.items():
        if node.parent_title is not None or not node.children:
            continue
        subpage_titles = [child.title for child in node.children]
        inferred = infer_root_categories(title, subpage_titles, direct_cats_by_title)
        if inferred:
            candidates[title] = inferred
    return candidates


def find_orphaned_categories(graph: CategoryGraph) -> list[str]:
    """Real, non-redirect Category-namespace pages with zero parent tags of
    their own (excluding the root) -- thin wrapper over
    pipeline.build_tree.orphaned_category_titles, restricted to categories
    that actually have their own page (red-link categories are reported
    separately, see find_redlink_categories, since the fix is different:
    create the page vs. add a parent tag). A Category-namespace #REDIRECT
    stub legitimately carries no [[वर्गः:...]] tag of its own -- that's not
    a mis-filed category, it's an alias pointing at (usually) a properly
    parented target, so it would be a false positive here."""
    return [
        t for t in orphaned_category_titles(graph)
        if graph.nodes[t].record is not None
        and graph.nodes[t].record.redirect_target is None
    ]


def find_redlink_categories(
    graph: CategoryGraph,
    direct_cats_by_title: dict[str, set[str]] | None = None,
) -> list[str]:
    """Categories named as a parent/child via [[वर्गः:X]] somewhere in another
    category's own wikitext, OR tagged directly onto a Main/Index-namespace
    page via direct_cats_by_title, where X itself was never created as a real
    Category-namespace page. See
    notes/orphan-bucket-vs-orphaned-categories.md: a category tag that
    appears ONLY on a Main/Index page (never named by another category's own
    [[वर्गः:...]] link) never becomes a CategoryGraph node at all, so relying
    on graph.nodes alone misses it entirely -- it's invisible to this check
    even though it's the same underlying defect. direct_cats_by_title is
    optional (defaults to catching only the graph-node case) so existing
    callers/tests that only have a CategoryGraph keep working."""
    redlinks = {t for t, n in graph.nodes.items() if n.record is None}
    if direct_cats_by_title is not None:
        for cats in direct_cats_by_title.values():
            for cat in cats:
                node = graph.nodes.get(cat)
                if node is None or node.record is None:
                    redlinks.add(cat)
    return sorted(redlinks)


def find_multi_parented_categories(graph: CategoryGraph) -> dict[str, list[str]]:
    """Real Category-namespace pages filed under more than one parent via
    [[वर्गः:X]]. Not a defect in the Atlas itself -- the frontend already
    renders every occurrence independently with a "see also" pointer (see
    docs/about.html, "Cross-tree Connections") -- but usually reflects
    imprecise categorization upstream that's worth surfacing for the
    editing campaign, same as the other checks here."""
    return {
        title: sorted(node.parents)
        for title, node in graph.nodes.items()
        if len(node.parents) > 1 and title != graph.root_title
    }


def find_category_cycles(graph: CategoryGraph) -> list[list[str]]:
    """Standard DFS cycle detection (white/gray/black coloring) over the
    category digraph's parent -> child edges. Returns one path per detected
    back-edge, from the cycle's start back to itself -- a category graph is
    supposed to be a tree (see docs/about.html), so any cycle here is always
    a mis-tagging, not a modeling choice."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t: WHITE for t in graph.nodes}
    cycles: list[list[str]] = []

    def dfs(title: str, path: list[str]) -> None:
        color[title] = GRAY
        path.append(title)
        node = graph.nodes.get(title)
        if node is not None:
            for child in sorted(node.children):
                if color.get(child, WHITE) == WHITE:
                    dfs(child, path)
                elif color.get(child) == GRAY:
                    idx = path.index(child)
                    cycles.append(path[idx:] + [child])
        path.pop()
        color[title] = BLACK

    for title in sorted(graph.nodes):
        if color[title] == WHITE:
            dfs(title, [])

    return cycles


def find_tagged_page_ns_items(
    page_records: list,
    cat_ns_name: str,
) -> dict[str, set[str]]:
    """Page-namespace (पृष्ठम्) records carrying their own direct category
    tag -- against the site's convention that only Index items, not
    individual scanned Pages, should be categorized."""
    return {
        rec.title: cats
        for rec in page_records
        if (cats := direct_categories(rec, cat_ns_name))
    }


def find_bulk_import_candidates(
    main_records: list[PageRecord],
) -> dict[str, set[str]]:
    """Returns {page title: {repo domains}} for Main-namespace pages whose
    external links are dominated by a known bulk-text-repo domain (nearly
    all of the page's external links go to it) or which carry an explicit
    source-attribution section header -- either signal on its own indicates
    the page content likely originates wholesale from that repository rather
    than from transcription/composition on sa.wikisource. A repo domain
    appearing only as one citation among many unrelated external links
    (footnote/cross-reference style) does not qualify."""
    candidates: dict[str, set[str]] = {}
    for rec in main_records:
        urls = _URL_RE.findall(rec.text)
        if not urls:
            continue
        domains = [
            re.sub(r"^www\.", "", urlparse(u.rstrip("].,")).netloc.lower())
            for u in urls
        ]
        repo_domains = {d for d in domains if d in BULK_TEXT_REPO_DOMAINS}
        if not repo_domains:
            continue
        repo_hits = sum(1 for d in domains if d in repo_domains)
        dominant = repo_hits >= max(1, len(domains) - 1)
        if dominant or _SOURCE_HEADER_RE.search(rec.text):
            candidates[rec.title] = repo_domains
    return candidates


def find_broken_commons_transclusions(
    dump_index,
    main_records: list[PageRecord],
) -> dict[str, tuple[int, str, str]]:
    """Detects the "missing Commons file" pattern from
    notes/broken-transclusion-audit-research.md: an Index item that IS
    transcluded into Main-namespace content (so the Atlas already folds its
    leaves' real text into that Main page, per
    process._augment_main_sizes_with_transclusion) and DOES have real
    पृष्ठम्:Title/N leaf pages in the dump, but whose backing scan file no
    longer exists on Commons -- so the live wiki renders the page as
    completely empty (ProofreadPage's rendering depends on the file, not the
    already-stored leaf wikitext) even though the Atlas shows real content.

    Confirmed (see the note) that this is NOT detectable from the dump alone:
    resolve_transcluded_leaves() resolves such an Index's leaves just fine
    (the file's absence has zero footprint in stored wikitext), and
    MediaWiki's own live maintenance category for this is injected at render
    time, invisible to a static XML dump. So this is the one check in this
    module that hits the network: one batched Commons `action=query` call
    per <=50 candidate Index titles (326 candidates on the 2026-07 dump ->
    ~7 calls total), checking `missing` on the File: page, with retry/backoff
    for Commons' rate limiting (confirmed live: HTTP 429 under back-to-back
    requests).

    Returns {index bare title: (leaf_count, first_leaf_title, last_leaf_title)}
    for Index items confirmed to have a missing backing file. first ==
    last when leaf_count == 1.
    """
    import time
    import urllib.error
    import urllib.parse
    import urllib.request
    import json as _json

    page_ns_id = dump_index.page_ns_id()
    if page_ns_id is None:
        return {}
    page_ns_name = dump_index.namespaces[page_ns_id]
    all_page_records = dump_index.pages_by_ns.get(page_ns_id, [])

    leaves_by_index: dict[str, list[str]] = {}
    for rec in all_page_records:
        owner = _owning_index_title(rec.title, page_ns_name)
        if owner is not None:
            leaves_by_index.setdefault(owner, []).append(rec.title)

    transclusion_map = build_transclusion_map(main_records)
    candidates = sorted(
        idx for idx, leaves in leaves_by_index.items()
        if leaves and is_transcluded(idx, transclusion_map)
    )
    if not candidates:
        return {}

    def leaf_sort_key(leaf_title: str) -> int:
        suffix = leaf_title.rsplit("/", 1)[1] if "/" in leaf_title else ""
        from pipeline.transclusion import leaf_number
        n = leaf_number(suffix)
        return n if n is not None else 0

    missing_titles: set[str] = set()
    batch_size = 50
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        titles_param = "|".join(f"File:{t}" for t in batch)
        # POST, not GET: Devanagari titles URL-encode to ~3x their length, and
        # a 50-title batch (this module's convention, matching the MediaWiki
        # API's max titles-per-query) reliably blows past GET's URI-length
        # limit (confirmed: HTTP 414 on real batches) -- POST puts the title
        # list in the body instead, where there's no such limit.
        body = urllib.parse.urlencode({
            "action": "query",
            "titles": titles_param,
            "format": "json",
            "formatversion": "2",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://commons.wikimedia.org/w/api.php",
            data=body,
            headers={"User-Agent": (
                "sanskrit-wikisource-atlas/2.0 "
                "(https://github.com/tylergneill/sanskrit-wikisource-atlas; polite; research use)"
            )},
            method="POST",
        )
        # Commons rate-limits this endpoint (confirmed live: HTTP 429 under
        # back-to-back batches with no delay) -- retried with backoff rather
        # than silently skipped, since a skipped batch would silently
        # undercount real hits (candidates in that batch never get checked
        # at all) rather than just being slow.
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = _json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                print(f"warning: Commons API request failed, skipping batch: {e}", file=sys.stderr)
                break
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"warning: Commons API request failed, skipping batch: {e}", file=sys.stderr)
                break
        if data is None:
            continue
        time.sleep(0.5)  # be polite to a shared public API even on success
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                title = page["title"]
                if title.startswith("File:"):
                    missing_titles.add(title[len("File:"):])

    result = {}
    for idx in missing_titles:
        if idx not in leaves_by_index:
            continue
        ordered = sorted(leaves_by_index[idx], key=leaf_sort_key)
        result[idx] = (len(ordered), ordered[0], ordered[-1])
    return result


def print_report(
    breadcrumb_candidates: dict[str, list[str]],
    separator_families: dict[str, list[str]],
    unresolvable_slash_paths: dict[str, list[str]],
    inference_candidates: dict[str, set[str]],
    orphaned_categories: list[str],
    redlink_categories: list[str],
    category_cycles: list[list[str]],
    tagged_page_ns_items: dict[str, set[str]],
    multi_parented_categories: dict[str, list[str]],
    bulk_import_candidates: dict[str, set[str]],
    broken_commons_transclusions: dict[str, tuple[int, str, str]],
) -> None:
    """All Devanagari data values (titles, category names) are transliterated
    to IAST before printing -- terminal-output-only, same convention as
    pipeline.progress.to_iast's docstring: never touches persisted data, only
    what's shown while a human is reading this report."""
    total_pages = sum(len(v) for v in breadcrumb_candidates.values())
    print("=" * 70)
    print(f"BREADCRUMB-GAP CANDIDATES: {len(breadcrumb_candidates)} texts, "
          f"{total_pages} pages implicated")
    print("=" * 70)
    print("(top-level page titles with other top-level pages sharing them as")
    print(" a space-separated prefix -- candidates for '/' subpage syntax.")
    print(" Review each on-wiki before moving; this tool does not rename pages.)")
    print()
    for title in sorted(breadcrumb_candidates, key=lambda t: -len(breadcrumb_candidates[t])):
        others = breadcrumb_candidates[title]
        print(f"  {to_iast(title)}  ({len(others)} candidate pages)")
        for other in others[:5]:
            print(f"      {to_iast(other)}")
        if len(others) > 5:
            print(f"      ... and {len(others) - 5} more")
    print()

    sep_pages = sum(len(v) for v in separator_families.values())
    print("=" * 70)
    print(f"SEPARATOR-FAMILY CANDIDATES: {len(separator_families)} texts, "
          f"{sep_pages} pages implicated")
    print("=" * 70)
    print("(flat pages using a hyphen/dot where '/' belongs, whose stem IS a")
    print(" real page -- e.g. mahabharatam-03-aranyakaparva-001. Unlike a real")
    print(" '/', a hyphen carries no structural meaning, so these stay flat in")
    print(" tree.json and keep counting as separate texts until moved on-wiki.)")
    print()
    for title in sorted(separator_families, key=lambda t: -len(separator_families[t])):
        members = separator_families[title]
        print(f"  {to_iast(title)}  ({len(members)} pages)")
        for other in members[:5]:
            print(f"      {to_iast(other)}")
        if len(members) > 5:
            print(f"      ... and {len(members) - 5} more")
    print()

    unres_pages = sum(len(v) for v in unresolvable_slash_paths.values())
    print("=" * 70)
    print(f"UNRESOLVABLE SUBPAGE PATHS: {len(unresolvable_slash_paths)} roots, "
          f"{unres_pages} pages implicated")
    print("=" * 70)
    print("(pages that DO use '/' subpage syntax but whose breadcrumb reaches")
    print(" no existing page at any level -- not the immediate parent, not any")
    print(" higher ancestor, not via a redirect. Fix upstream: create the")
    print(" missing ancestor, or retitle under one that exists.)")
    print()
    for root in sorted(unresolvable_slash_paths, key=lambda t: -len(unresolvable_slash_paths[t])):
        members = unresolvable_slash_paths[root]
        print(f"  {to_iast(root)}  ({len(members)} pages)")
        for other in members[:5]:
            print(f"      {to_iast(other)}")
        if len(members) > 5:
            print(f"      ... and {len(members) - 5} more")
    print()

    print("=" * 70)
    print(f"ROOT CATEGORY-INFERENCE CANDIDATES: {len(inference_candidates)} texts")
    print("=" * 70)
    print("(top-level pages whose real '/' subpages ALL share a category tag")
    print(" the top-level page itself lacks -- candidate for adding that tag")
    print(" directly to the top-level page on-wiki. This tool does not infer")
    print(" or apply the tag.)")
    print()
    for title in sorted(inference_candidates):
        cats = inference_candidates[title]
        print(f"  {to_iast(title)}  -> missing: {', '.join(to_iast(c) for c in sorted(cats))}")
    print()

    print("=" * 70)
    print(f"ORPHANED CATEGORIES: {len(orphaned_categories)}")
    print("=" * 70)
    print("(real Category pages with no parent tag of their own -- add a")
    print(" [[category:Parent]] tag on-wiki to file them under the real tree.)")
    print()
    for title in sorted(orphaned_categories):
        print(f"  {to_iast(title)}")
    print()

    print("=" * 70)
    print(f"RED-LINK CATEGORIES: {len(redlink_categories)}")
    print("=" * 70)
    print("(named as a parent/child via [[category:X]] somewhere, but X was never")
    print(" created as its own Category page on-wiki.)")
    print()
    for title in sorted(redlink_categories):
        print(f"  {to_iast(title)}")
    print()

    print("=" * 70)
    print(f"CATEGORY CYCLES: {len(category_cycles)}")
    print("=" * 70)
    print("(a category graph should be a tree -- any cycle here is a")
    print(" mis-tagging on-wiki, not a modeling choice.)")
    print()
    for cycle in category_cycles:
        print(f"  {' -> '.join(to_iast(t) for t in cycle)}")
    print()

    print("=" * 70)
    print(f"CATEGORY-TAGGED PAGE-NAMESPACE ITEMS: {len(tagged_page_ns_items)}")
    print("=" * 70)
    print("(Page: scanned-leaf items should not carry their own Category tag")
    print(" by site convention -- move the tag to the owning Index item instead.)")
    print()
    for title in sorted(tagged_page_ns_items):
        cats = tagged_page_ns_items[title]
        print(f"  {to_iast(title)}  -> {', '.join(to_iast(c) for c in sorted(cats))}")
    print()

    print("=" * 70)
    print(f"MULTI-PARENTED CATEGORIES: {len(multi_parented_categories)}")
    print("=" * 70)
    print("(filed under more than one parent Category -- not an Atlas defect,")
    print(" the frontend already handles this, but usually reflects imprecise")
    print(" categorization upstream worth reviewing on-wiki.)")
    print()
    for title in sorted(multi_parented_categories):
        parents = multi_parented_categories[title]
        print(f"  {to_iast(title)}  -> {', '.join(to_iast(p) for p in parents)}")
    print()

    print("=" * 70)
    print(f"EXTERNAL BULK-IMPORT CANDIDATES: {len(bulk_import_candidates)}")
    print("=" * 70)
    print("(page content likely copy-pasted wholesale from a known digitized-text")
    print(" repository -- not a defect, but worth knowing for corpus provenance.")
    print(" Not renamed/tagged/altered by this tool.)")
    print()
    for title in sorted(bulk_import_candidates):
        domains = bulk_import_candidates[title]
        print(f"  {to_iast(title)}  -> {', '.join(sorted(domains))}")

    print()
    print("=" * 70)
    print(f"BROKEN COMMONS TRANSCLUSIONS: {len(broken_commons_transclusions)}")
    print("=" * 70)
    print("(Index item is transcluded and has real leaf content in the dump,")
    print(" but its backing scan file no longer exists on Commons -- the live")
    print(" wiki page renders empty even though the Atlas shows real content.)")
    print()
    for idx_title in sorted(broken_commons_transclusions):
        leaf_count, first_leaf, last_leaf = broken_commons_transclusions[idx_title]
        if leaf_count == 1:
            print(f"  {to_iast(idx_title)}  -> 1 hidden leaf page, "
                  f"{_wiki_url(first_leaf)}")
        else:
            print(f"  {to_iast(idx_title)}  -> {leaf_count} hidden leaf pages, "
                  f"first: {_wiki_url(first_leaf)}, last: {_wiki_url(last_leaf)}")


ABOUT_HTML_PATH = Path("docs/about.html")
AUDIT_START_MARKER = "<!-- AUDIT:START -- generated by `python -m pipeline.audit --update-about`; do not hand-edit between these markers -->"
AUDIT_END_MARKER = "<!-- AUDIT:END -->"


def _esc(s: str) -> str:
    return html.escape(to_iast(s))


def _wiki_url(title: str) -> str:
    """Same convention as process.py's page_url/category_url/index_url --
    titles here already carry their own namespace prefix where relevant
    (Category-namespace titles get one added since callers pass bare titles;
    Main/Page-namespace titles from the dump already have any prefix baked
    in), so this is the one shared quote-and-prefix step."""
    from urllib.parse import quote
    return "https://sa.wikisource.org/wiki/" + quote(title.replace(" ", "_"))


def _link(title: str, url_title: str | None = None) -> str:
    """Escaped/transliterated display text wrapped in a link to the live
    wiki page, so a reader can jump straight from a finding to fixing it
    on-wiki. url_title defaults to title itself; pass a separate one when
    the display title needs a namespace prefix added for the URL (Category
    names are stored bare) that shouldn't show in the display text."""
    href = _wiki_url(url_title if url_title is not None else title)
    return f'<a href="{href}" target="_blank">{_esc(title)}</a>'


def _cat_link(title: str, cat_ns_name: str) -> str:
    return _link(title, f"{cat_ns_name}:{title}")


def _findings_list(items: list[str]) -> str:
    """items are already-escaped/transliterated HTML fragments (built via
    _esc at the call site) -- this only wraps them in <li>/<ul>, it must not
    re-escape or re-transliterate. Indented past its own <summary>'s
    triangle+text start (docs/about.html's global .content ul reset only
    gives 12px, not enough to read as nested under the header above it)."""
    lis = "\n".join(f"            <li>{item}</li>" for item in items)
    return f'          <ul style="margin-left: 1.6em;">\n{lis}\n          </ul>'


def _summary_text(description: str, label: str) -> str:
    """The summary's own content, wrapped in a single span.

    `.audit-summary` is `display: flex` (so the disclosure triangle can be a
    flex-aligned ::before), which makes every child a flex item -- and flex
    DISCARDS the whitespace between items. Every description here that ends in
    an element rather than a text node -- any `_link()`, i.e. most of them --
    therefore rendered as "tantralokah(27 candidate pages)", with the space
    silently eaten. Wrapping the whole label makes it one flex item, and the
    space inside it survives.

    Found via the e-bharatisampat sibling, which hit the same thing the moment
    a summary started with a <span>-wrapped Devanagari term.
    """
    return f"<span>{description} ({label})</span>"


def _bullet(description: str, count: int, inner_html: str, unit: str | None = None) -> str:
    """`unit` spells out what the number actually counts, for checks where a
    bare count would be ambiguous or misleading -- e.g. the separator-family
    check groups 2,338 pages under only 10 texts, so "(10)" alone reads as the
    smallest finding when it's the largest. Every caller should pass a `unit`
    naming its noun ("pages", "categories", ...) unless the description itself
    already makes the unit unambiguous. Falls back to the bare count."""
    if count == 0:
        # Same one-flex-item wrapper as _summary_text, for the same reason.
        return f'          <li class="audit-item"><div class="audit-summary audit-summary-empty"><span>{description}: none found</span></div></li>'
    label = unit if unit is not None else str(count)
    return (
        f'          <li class="audit-item"><details>\n'
        f'            <summary class="audit-summary">{_summary_text(description, label)}</summary>\n'
        f"{inner_html}\n"
        f"          </details></li>"
    )


def _sub_bullet(description: str, count: int, inner_html: str, unit: str | None = None) -> str:
    """Same disclosure-triangle construction as _bullet, for a nested
    <details> one level inside an outer audit finding (grouping a flat
    candidate dump into collapsible sub-groups, e.g. by shared title prefix
    or by external-repo domain). Reuses .audit-item/.audit-summary styling
    so the triangle and spacing match the top-level findings exactly.
    `unit` spells out what the number counts, same convention as _bullet."""
    label = unit if unit is not None else str(count)
    return (
        f'<li class="audit-item"><details>\n'
        f'<summary class="audit-summary">{_summary_text(description, label)}</summary>\n'
        f"{inner_html}\n"
        f"</details></li>"
    )


def _group_by_next_token(prefix: str, others: list[str]) -> tuple[str, str]:
    """Renders a breadcrumb parent's flat candidate list as nested
    collapsible groups instead of one long flat <ul> -- candidates sharing
    the same next space-separated token after the parent prefix (e.g.
    "ṛgvedaḥ maṇḍala 1" / "ṛgvedaḥ maṇḍala 10" both share "maṇḍala") are
    grouped under a sub-<details> keyed by that token, recursing while a
    group still has >1 further token to split on. Singleton groups (no
    other candidate shares that next token) are left as plain <li> entries
    rather than a pointless one-item dropdown. When an entire group shares
    ANOTHER token beyond the one that formed it (e.g. all 10 members of
    "uttarasthānam" also share "adhyāya" as their very next token), that
    next level wouldn't split anything -- it would just repeat the same
    member list one level deeper -- so the prefix is extended before
    grouping. Returns (effective_prefix, inner_ul_html) so a caller
    labeling the enclosing <summary> always names the level actually being
    displayed, not the single token that triggered the recursive call.

    A member can be exactly equal to the (possibly already-extended) prefix
    -- nothing left to split on ("" remainder) -- if e.g. a redirect or
    duplicate title collapses onto its own parent grouping token. That
    member can never contribute a further shared token, so it must stop the
    collapse loop rather than let a group of all-"" tokens look like a
    single shared token and grow the prefix forever (infinite loop)."""
    while all(other[len(prefix) + 1:] for other in others):
        prefix_len = len(prefix) + 1
        tokens = {other[prefix_len:].split(" ", 1)[0] for other in others}
        if len(tokens) != 1:
            break
        prefix = f"{prefix} {next(iter(tokens))}"

    prefix_len = len(prefix) + 1
    groups: dict[str, list[str]] = defaultdict(list)
    for other in others:
        rest = other[prefix_len:]
        token = rest.split(" ", 1)[0]
        groups[token].append(other)

    parts = []
    for token in sorted(groups):
        members = groups[token]
        if len(members) == 1:
            parts.append(f"<li>{_link(members[0])}</li>")
            continue
        effective_prefix, inner = _group_by_next_token(f"{prefix} {token}", members)
        parts.append(_sub_bullet(_esc(effective_prefix), len(members), inner))
    html_out = f'<ul style="margin-left: 1.6em; list-style: none; padding-left: 0;">{"".join(parts)}</ul>'
    return prefix, html_out


def render_audit_html(
    breadcrumb_candidates: dict[str, list[str]],
    separator_families: dict[str, list[str]],
    unresolvable_slash_paths: dict[str, list[str]],
    inference_candidates: dict[str, set[str]],
    multi_parented_categories: dict[str, list[str]],
    orphaned_categories: list[str],
    redlink_categories: list[str],
    category_cycles: list[list[str]],
    tagged_page_ns_items: dict[str, set[str]],
    bulk_import_candidates: dict[str, set[str]],
    broken_commons_transclusions: dict[str, tuple[int, str, str]],
    cat_ns_name: str,
    index_ns_name: str,
) -> str:
    """Renders the <ul> of dropdown bullets shown between the AUDIT markers
    in docs/about.html, one per pipeline.audit check, each populated with
    that run's actual findings. All titles/category names are transliterated
    to IAST, same convention as print_report, and linked out to the live
    wiki page so a reader can jump straight from a finding to fixing it."""
    bullets = []

    breadcrumb_items = []
    for title in sorted(breadcrumb_candidates, key=lambda t: -len(breadcrumb_candidates[t])):
        others = breadcrumb_candidates[title]
        _effective_prefix, sub = _group_by_next_token(title, others)
        breadcrumb_items.append(
            f'<li class="audit-item"><details>\n'
            f'<summary class="audit-summary">{_summary_text(_link(title), f"{len(others)} candidate pages")}</summary>\n'
            f"{sub}\n"
            f"</details></li>"
        )
    separator_items = []
    for title in sorted(separator_families, key=lambda t: -len(separator_families[t])):
        members = separator_families[title]
        _effective_prefix, sub = _group_by_next_token(title, members)
        separator_items.append(
            f'<li class="audit-item"><details>\n'
            f'<summary class="audit-summary">{_summary_text(_link(title), f"{len(members)} pages")}</summary>\n'
            f"{sub}\n"
            f"</details></li>"
        )

    unresolvable_items = []
    for root in sorted(unresolvable_slash_paths, key=lambda t: -len(unresolvable_slash_paths[t])):
        members = unresolvable_slash_paths[root]
        unresolvable_items.append(_sub_bullet(
            _link(root),
            len(members),
            _findings_list([_link(m) for m in members]),
            unit=f"{len(members)} pages",
        ))

    # All three are the same underlying defect -- a work whose parts don't roll
    # up into one text -- so they sit under one bullet, split by what's actually
    # wrong (and therefore by what the on-wiki fix is: move the pages, vs.
    # create/retitle the missing ancestor). See notes/wikisource-editing-plan.md.
    breadcrumb_pages = sum(len(v) for v in breadcrumb_candidates.values())
    separator_pages = sum(len(v) for v in separator_families.values())
    unresolvable_pages = sum(len(v) for v in unresolvable_slash_paths.values())

    def _group_ul(items: list[str]) -> str:
        return (
            f'          <ul style="margin-left: 1.6em; list-style: none; padding-left: 0;">\n'
            + "\n".join(f"            {item}" for item in items)
            + "\n          </ul>"
        )

    breadcrumb_groups = [
        _sub_bullet(
            'Space-separated title instead of a "/"',
            len(breadcrumb_candidates),
            _group_ul(breadcrumb_items),
            unit=f"{breadcrumb_pages} pages under {len(breadcrumb_candidates)} texts",
        ),
        _sub_bullet(
            'Hyphen or dot instead of a "/"',
            len(separator_families),
            _group_ul(separator_items),
            unit=f"{separator_pages} pages under {len(separator_families)} texts",
        ),
        _sub_bullet(
            'Has a "/" breadcrumb, but it points at a page that does not exist',
            len(unresolvable_slash_paths),
            _group_ul(unresolvable_items),
            unit=f"{unresolvable_pages} pages under {len(unresolvable_slash_paths)} missing roots",
        ),
    ]
    bullets.append(_bullet(
        "Pages that look like missed breadcrumb subpages",
        breadcrumb_pages + separator_pages + unresolvable_pages,
        _group_ul(breadcrumb_groups),
        unit=f"{breadcrumb_pages + separator_pages + unresolvable_pages} pages",
    ))

    inference_items = [
        f"{_link(title)} &rarr; missing: {', '.join(_cat_link(c, cat_ns_name) for c in sorted(cats))}"
        for title, cats in sorted(inference_candidates.items())
    ]
    bullets.append(_bullet(
        "Parent pages lacking a Category shared uniformly by all subpages",
        len(inference_candidates),
        _findings_list(inference_items),
        unit=f"{len(inference_candidates)} pages",
    ))

    multi_parent_items = [
        f"{_cat_link(title, cat_ns_name)} &rarr; {', '.join(_cat_link(p, cat_ns_name) for p in parents)}"
        for title, parents in sorted(multi_parented_categories.items())
    ]
    bullets.append(_bullet(
        "Categories with multiple parents",
        len(multi_parented_categories),
        _findings_list(multi_parent_items),
        unit=f"{len(multi_parented_categories)} categories",
    ))

    bullets.append(_bullet(
        "Categories never filed under any parent (orphaned)",
        len(orphaned_categories),
        _findings_list([_cat_link(t, cat_ns_name) for t in sorted(orphaned_categories)]),
        unit=f"{len(orphaned_categories)} categories",
    ))

    bullets.append(_bullet(
        "Categories referenced but never created (red links)",
        len(redlink_categories),
        _findings_list([_cat_link(t, cat_ns_name) for t in sorted(redlink_categories)]),
        unit=f"{len(redlink_categories)} categories",
    ))

    cycle_items = [" &rarr; ".join(_cat_link(t, cat_ns_name) for t in cycle) for cycle in category_cycles]
    bullets.append(_bullet(
        "Cycles in the Category graph",
        len(category_cycles),
        _findings_list(cycle_items),
        unit=f"{len(category_cycles)} cycles",
    ))

    tagged_items = [
        f"{_link(title)} &rarr; {', '.join(_cat_link(c, cat_ns_name) for c in sorted(cats))}"
        for title, cats in sorted(tagged_page_ns_items.items())
    ]
    bullets.append(_bullet(
        "Page-namespace (पृष्ठम्) items wrongly carrying their own Category tag",
        len(tagged_page_ns_items),
        _findings_list(tagged_items),
        unit=f"{len(tagged_page_ns_items)} items",
    ))

    def _broken_commons_item(idx_title: str, leaf_count: int, first_leaf: str, last_leaf: str) -> str:
        idx_link = _link(idx_title, f"{index_ns_name}:{idx_title}")
        if leaf_count == 1:
            leaf_link = f'<a href="{_wiki_url(first_leaf)}" target="_blank">1 hidden leaf page</a>'
            return f"{idx_link} &rarr; {leaf_link}"
        first_link = f'<a href="{_wiki_url(first_leaf)}" target="_blank">first</a>'
        last_link = f'<a href="{_wiki_url(last_leaf)}" target="_blank">last</a>'
        return f"{idx_link} &rarr; {leaf_count} hidden leaf pages, {first_link} and {last_link}"

    broken_commons_items = [
        _broken_commons_item(idx_title, leaf_count, first_leaf, last_leaf)
        for idx_title, (leaf_count, first_leaf, last_leaf) in sorted(broken_commons_transclusions.items())
    ]
    bullets.append(_bullet(
        "Transclusions broken due to removal of image file from Commons",
        len(broken_commons_transclusions),
        _findings_list(broken_commons_items),
        unit=f"{len(broken_commons_transclusions)} Index items",
    ))

    body = "\n".join(bullets)
    correction_list = f'        <ul style="list-style: none; padding-left: 0;">\n{body}\n        </ul>'

    by_domain: dict[str, list[str]] = defaultdict(list)
    for title, domains in bulk_import_candidates.items():
        for domain in domains:
            by_domain[domain].append(title)

    domain_items = []
    for domain in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d)):
        titles = sorted(by_domain[domain])
        sub = _findings_list([_link(t) for t in titles])
        domain_items.append(_sub_bullet(html.escape(domain), len(titles), sub))
    bulk_bullet = _bullet(
        "Pages likely imported from other online text collections",
        len(bulk_import_candidates),
        f'          <ul style="margin-left: 1.6em; list-style: none; padding-left: 0;">\n'
        + "\n".join(f"            {item}" for item in domain_items)
        + "\n          </ul>",
    )
    bulk_section = (
        '        <h3 id="provenance-insights">Provenance Insights</h3>\n'
        '        <p>\n'
        "          The same audit pipeline also makes note of source links:\n"
        "        </p>\n"
        '        <ul style="list-style: none; padding-left: 0;">\n'
        f"{bulk_bullet}\n"
        "        </ul>"
    )

    return correction_list + "\n\n" + bulk_section


def update_about_html(new_ul_html: str, path: Path = ABOUT_HTML_PATH) -> None:
    """Replaces the <ul> block between the AUDIT markers in docs/about.html
    with freshly generated content -- a targeted string replace, not a full
    HTML parse/rewrite, so the rest of the file (including the markers
    themselves) is left untouched."""
    text = path.read_text(encoding="utf-8")
    start = text.index(AUDIT_START_MARKER) + len(AUDIT_START_MARKER)
    end = text.index(AUDIT_END_MARKER)
    path.write_text(text[:start] + "\n" + new_ul_html + "\n        " + text[end:], encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xml_path", type=Path, nargs="?", help="path to the uncompressed dump XML")
    parser.add_argument(
        "--update-about", action="store_true",
        help="also regenerate the audit bullet list in docs/about.html (between the AUDIT markers)",
    )
    args = parser.parse_args()

    xml_path = args.xml_path
    if xml_path is None:
        candidates = sorted(Path("data/dump/1_current_format_live").glob("sawikisource-*.xml"))
        if not candidates:
            print("no dump/1_current_format_live/*.xml found", file=sys.stderr)
            sys.exit(1)
        # Newest, not oldest -- same rationale as pipeline.process.main().
        if len(candidates) > 1:
            print(f"{len(candidates)} dumps present in dump/1_current_format_live; "
                  f"using newest: {candidates[-1].name}", file=sys.stderr)
        xml_path = candidates[-1]

    print(f"parsing {xml_path}", file=sys.stderr)
    # Page (104) is the ProofreadPage extension -- absent from early dumps,
    # same caveat as elsewhere in this pipeline (see parse_dump.DumpIndex).
    # namespaces_of_interest is resolved AFTER siteinfo is read, so page_ns_id()
    # can't be called until parse_dump has already run once; instead pass None
    # (default: every namespace this pipeline ever cares about) since Main +
    # Category + Page is most of the dump anyway and Index/Template add little
    # extra cost here without content-size computation.
    dump_index = parse_dump(xml_path, namespaces_of_interest=None)

    cat_ns_name = dump_index.namespaces[dump_index.category_ns_id()]
    main_records = dump_index.pages_by_ns[0]
    page_ns_id = dump_index.page_ns_id()
    page_records = dump_index.pages_by_ns.get(page_ns_id, []) if page_ns_id is not None else []
    index_ns_id = dump_index.index_ns_id()
    index_records = dump_index.pages_by_ns.get(index_ns_id, []) if index_ns_id is not None else []
    index_ns_name = dump_index.namespaces[index_ns_id] if index_ns_id is not None else ""

    main_nodes = build_main_tree(main_records)

    print("building category graph...", file=sys.stderr)
    graph = build_category_graph(dump_index.pages_by_ns[14], cat_ns_name)

    direct_cats_by_title = {
        rec.title: {c for c in direct_categories(rec, cat_ns_name) if not is_excluded_category(c)}
        for rec in main_records
    }
    # Main + Index (not Page -- those are covered separately by
    # find_tagged_page_ns_items, since a Page carrying its own tag is itself
    # a distinct finding, not a source to trust for redlink detection here).
    content_direct_cats_by_title = dict(direct_cats_by_title)
    content_direct_cats_by_title.update({
        rec.title: {c for c in direct_categories(rec, cat_ns_name) if not is_excluded_category(c)}
        for rec in index_records
    })

    allowlist_problems = check_flat_family_allowlist(main_nodes)
    if allowlist_problems:
        print("\n!!! FLAT_FAMILY_PATTERNS allowlist problems "
              "(pipeline health, not a wiki finding):", file=sys.stderr)
        for problem in allowlist_problems:
            print(f"  - {problem}", file=sys.stderr)
        print(file=sys.stderr)
    else:
        print(f"flat-family allowlist: all {len(FLAT_FAMILY_PATTERNS)} rows healthy",
              file=sys.stderr)

    breadcrumb_candidates = find_breadcrumb_gap_candidates(main_nodes)
    separator_families = find_separator_family_candidates(main_nodes)
    unresolvable_slash_paths = find_unresolvable_slash_paths(main_nodes)
    inference_candidates = find_root_inference_candidates(main_nodes, direct_cats_by_title)
    orphaned_categories = find_orphaned_categories(graph)
    redlink_categories = find_redlink_categories(graph, content_direct_cats_by_title)
    category_cycles = find_category_cycles(graph)
    tagged_page_ns_items = find_tagged_page_ns_items(page_records, cat_ns_name)
    multi_parented_categories = find_multi_parented_categories(graph)
    bulk_import_candidates = find_bulk_import_candidates(main_records)

    print("checking Commons for missing transcluded files...", file=sys.stderr)
    broken_commons_transclusions = find_broken_commons_transclusions(dump_index, main_records)

    print_report(
        breadcrumb_candidates, separator_families, unresolvable_slash_paths,
        inference_candidates,
        orphaned_categories, redlink_categories, category_cycles, tagged_page_ns_items,
        multi_parented_categories, bulk_import_candidates,
        broken_commons_transclusions,
    )

    if args.update_about:
        new_ul_html = render_audit_html(
            breadcrumb_candidates, separator_families, unresolvable_slash_paths,
            inference_candidates, multi_parented_categories,
            orphaned_categories, redlink_categories, category_cycles, tagged_page_ns_items,
            bulk_import_candidates, broken_commons_transclusions, cat_ns_name, index_ns_name,
        )
        update_about_html(new_ul_html)
        print(f"updated {ABOUT_HTML_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
