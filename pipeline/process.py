"""
Process stage: runs parse_dump/build_tree/transclusion/content_size in
sequence and assembles their outputs (namespace records, Main subpage tree,
Category digraph, transclusion map, content-size stats) into a single JSON
tree for the frontend.

New schema (deliberately not identical to the old scraper's tree.json --
see notes/sawikisource-scraper-spec.md and the 2026-07 branch decision to
adapt the frontend rather than force new concepts into the old shape):

Node (category):
  { id, type: "category", title, children: [Node],
    pages: [PageNode], index_items: [IndexItemNode], stats }

Node (category-pointer): a second+ filing of a category already emitted
elsewhere in the tree (multi-parent category, see build_tree.CategoryGraph).
Appears inline among its parent's own `children`, alongside real category
nodes -- there is no separate list of pointers.
  { id, type: "category-pointer", title, points_to: <id>, stats }

PageNode (Main-namespace page, filed into this category via its own direct
[[वर्गः:...]] tag):
  { id, type: "page", title, url, stats, subpages: [PageNode],
    source_indexes?: [{title, url}] }
  subpages come from the Main-namespace tree (build_tree.MainPageNode),
  nested the same way the old schema nested them. A breadcrumb-subpage
  (title split on "/") normally only appears nested inside its parent's
  PageNode, but if it carries its own direct category tag NOT also carried
  by its immediate parent, it additionally gets its own full, independently
  reachable PageNode under that category too -- same "no pointer/skip logic,
  dedup happens in stats rollup" treatment build_category already gives
  multi-tagged top-level pages (see build_category's page_jsons loop).
  source_indexes is present (non-empty) only when this page transcludes a
  range of पृष्ठम्:Title/N leaves belonging to one or more Index items, via
  <pages index="..." /> -- a link back to the source scan, since the Atlas
  otherwise drops a transcluded Index item from display entirely in favor
  of this page (see build_reverse_transclusion_map).

Node (page-pointer): a second+ filing of a page already emitted elsewhere in
the tree (a page tagged with >1 category directly -- same multi-filing
concept as category-pointer, just for pages instead of categories).
  { id, type: "page-pointer", title, url, points_to: <id> }
  No stats/subpages of its own; resolve via points_to to the occurrence that
  has them (mirrors category-pointer's resolution, see docs/app.js's
  resolveContent).

IndexItemNode (Index-namespace item with ZERO transclusion anywhere in
Main-namespace content -- i.e. raw/unpublished OCR, per
transclusion.is_transcluded):
  { id, type: "index-item", title, url, stats }
  Never expandable into individual पृष्ठम्:Title/N (scanned-leaf) rows --
  those are only ever summed into this node's own stats, never listed (see
  compute_page_ns_rollup below and notes/sawikisource-scraper-spec.md,
  "Untranscluded Index items").

Node (index-item-pointer): a second+ filing of an Index item already emitted
elsewhere in the tree. Same shape/resolution as page-pointer.
  { id, type: "index-item-pointer", title, url, points_to: <id> }

stats: { raw_bytes, content_bytes, transliterated_bytes, count, last_changed }
  count = number of distinct Main pages + Index items reachable from this
  node. Dedup is enforced at build time by build_category(): the first DFS
  occurrence of a category/page/Index-item builds real content and folds its
  stats into every ancestor's rollup; every later occurrence anywhere else in
  the tree is emitted as a *-pointer node instead and is skipped when summing
  ancestor stats -- so a page/category reachable via two paths is still
  counted exactly once, at whichever ancestor its two paths first converge
  (not only at root).

Orphan bucket (असम्बद्धवर्गीकृतम्, see ORPHAN_BUCKET_TITLE): an ordinary
category node, appended as a sibling of the real category tree under root,
holding every Main page and untranscluded Index item unreachable from
वर्गसर्वस्वम् by category descent -- either zero category tags, or tags that
only point to categories themselves never filed under any reachable parent
(orphaned category roots, walked in as real subtrees the same way the main
tree is). Root's own headline stats deliberately exclude this bucket's
totals (matches scrape.py's अवर्गीकृतम्/OCR-bucket convention) -- it's
listed and browsable, just not counted as part of the "central" corpus
size. See build_tree_json for the mechanics.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.build_tree import (
    CategoryGraph,
    MainPageNode,
    build_category_graph,
    build_main_tree,
    refile_category,
)
from pipeline.content_size import (
    ContentSizeResult,
    build_template_index,
    compute_content_sizes_parallel,
)
from pipeline.parse_dump import DumpIndex, PageRecord, category_links, is_excluded_category, parse_dump
from pipeline.transclusion import (
    build_reverse_transclusion_map,
    build_transclusion_map,
    direct_categories,
    is_transcluded,
    resolve_transcluded_leaves,
    transclusion_ranges,
)

Stats = dict  # {raw_bytes, content_bytes, transliterated_bytes, count, last_changed}

ROOT_CATEGORY_TITLE = "वर्गसर्वस्वम्"


def page_url(title: str) -> str:
    from urllib.parse import quote
    return "https://sa.wikisource.org/wiki/" + quote(title.replace(" ", "_"))


def index_url(title: str, index_ns_name: str) -> str:
    from urllib.parse import quote
    return "https://sa.wikisource.org/wiki/" + quote((index_ns_name + ":" + title).replace(" ", "_"))


def category_url(title: str, category_ns_name: str) -> str:
    from urllib.parse import quote
    return "https://sa.wikisource.org/wiki/" + quote((category_ns_name + ":" + title).replace(" ", "_"))


@dataclass
class ContentIndex:
    """Precomputed per-page/per-index-item content-size results, keyed by
    title, so the tree-assembly walk below doesn't recompute expansion for
    a page it visits more than once (e.g. via category-pointer dedup)."""
    main_sizes: dict[str, ContentSizeResult]
    index_sizes: dict[str, ContentSizeResult]
    main_categories: dict[str, set[str]]  # Main page title -> its own direct category tags
    index_categories: dict[str, set[str]]  # Index item bare title -> its own direct category tags
    index_timestamps: dict[str, str]  # Index item bare title -> its own revision timestamp
    index_page_rollup: dict[str, Stats]  # Index item bare title -> summed stats over its untranscluded पृष्ठम्:Title/N children
    # Only --extract-text reads these three; nothing in tree assembly does.
    # They carry the scan-leaf TEXT, which is otherwise discarded once counted,
    # and the Index -> leaves mapping needed to tell which leaves a
    # transclusion stub actually publishes.
    page_sizes: dict[str, ContentSizeResult] = field(default_factory=dict)
    page_records: list[PageRecord] = field(default_factory=list)
    leaves_by_index: dict[str, list[str]] = field(default_factory=dict)
    index_records: list[PageRecord] = field(default_factory=list)
    untranscluded_leaves_by_index: dict[str, list[str]] = field(default_factory=dict)


def _owning_index_title(page_title: str, page_ns_name: str) -> str | None:
    """पृष्ठम्:<IndexBareTitle>/<N> -> <IndexBareTitle>, or None if the title
    doesn't match the Title/N convention at all (rare malformed case)."""
    prefix = page_ns_name + ":"
    if not page_title.startswith(prefix):
        return None
    bare = page_title[len(prefix):]
    if "/" not in bare:
        return None
    return bare.rsplit("/", 1)[0]


@dataclass
class LeafSizeIndex:
    """All पृष्ठम्:Title/N scanned-leaf content sizes, computed once and shared
    by two consumers: the untranscluded-Index rollup (folded onto the Index
    item's own node stats) and the transcluded-Main augmentation (folded onto
    the Main page that publishes those leaves via <pages .../>). See
    compute_page_ns_sizes."""
    untranscluded_index_rollup: dict[str, Stats]  # Index bare title -> summed stats over its leaves (untranscluded items only)
    leaves_by_index: dict[str, list[str]]  # Index bare title -> its leaf full-titles (पृष्ठम्:Title/N), transcluded indexes only
    leaf_sizes: dict[str, ContentSizeResult]  # leaf full-title -> its content size (transcluded-index leaves only)
    all_leaf_sizes: dict[str, ContentSizeResult]  # every leaf, transcluded or not -- for --extract-text, which writes text the rollup only counts
    all_leaf_records: list[PageRecord]  # the records behind all_leaf_sizes, in dump order
    # Defaulted, so it must follow every non-default field above.
    untranscluded_leaves_by_index: dict[str, list[str]] = field(default_factory=dict)  # the same leaves as titles, for --extract-text


def compute_page_ns_sizes(
    dump_index: DumpIndex,
    template_index: dict[str, str],
    known_titles: set[str] | None,
    cat_ns_name: str,
    transliterate: bool,
    workers: int | None,
    transclusion_map: dict[str, set[str]],
) -> LeafSizeIndex:
    """Computes content size for every पृष्ठम्:Title/N (scanned-leaf) record,
    then splits the results by whether the leaf's owning Index item is
    transcluded into Main-namespace content:

    - Untranscluded Index items are the organizing principle pre-transclusion,
      so their leaves are never listed/browsed, only summed into one stat on
      the Index item itself (see notes/sawikisource-scraper-spec.md,
      "Untranscluded Index items"). Returned pre-summed in
      untranscluded_index_rollup.

    - Transcluded Index items are dropped from display in favor of the Main
      page that publishes them -- but that Main page's own wikitext is often
      just the <pages index="..." from=A to=B /> tag, so its content_bytes
      reads near-zero unless the transcluded leaves' real text is folded in
      (see notes/proofreadpage-transclusion-undercount-fix.md). Their per-leaf
      sizes and index->leaf mapping are returned raw (leaf_sizes /
      leaves_by_index) for process.py to slice per Main page's own range.

    Sizing all leaves (not just the untranscluded ones, as before) is the cost
    of fixing the transclusion undercount -- ~90k additional leaves on the live
    dump. Parallelized like the Main/Index passes.
    """
    page_ns_id = dump_index.page_ns_id()
    # Page (104) is the ProofreadPage extension -- absent from early dumps
    # (e.g. 2011-10, before the extension was enabled on sawikisource). No
    # namespace means no scanned-leaf records to roll up, not an error.
    page_ns_name = dump_index.namespaces[page_ns_id] if page_ns_id is not None else ""
    all_page_records = dump_index.pages_by_ns.get(page_ns_id, []) if page_ns_id is not None else []

    owned_records = [
        rec for rec in all_page_records
        if _owning_index_title(rec.title, page_ns_name) is not None
    ]

    sizes = compute_content_sizes_parallel(
        owned_records, template_index, known_titles, cat_ns_name,
        transliterate=transliterate, workers=workers, progress_label="content size: Page (scan) leaves",
    )

    untranscluded_index_rollup: dict[str, Stats] = {}
    leaves_by_index: dict[str, list[str]] = {}
    # The same leaves, kept as titles: --extract-text folds them into the Index
    # item's own file so a scan nobody has assembled is still openable.
    untranscluded_leaves_by_index: dict[str, list[str]] = {}
    leaf_sizes: dict[str, ContentSizeResult] = {}
    for rec in owned_records:
        owner = _owning_index_title(rec.title, page_ns_name)
        size = sizes[rec.title]
        if is_transcluded(owner, transclusion_map):
            leaves_by_index.setdefault(owner, []).append(rec.title)
            leaf_sizes[rec.title] = size
        else:
            untranscluded_leaves_by_index.setdefault(owner, []).append(rec.title)
            current = untranscluded_index_rollup.get(owner) or _empty_stats()
            untranscluded_index_rollup[owner] = _merge_stats(current, _stats_dict(
                size.raw_wikitext_bytes, size.content_bytes, size.transliterated_bytes,
                0,  # leaves don't each count as a separate "work" -- only the Index item itself does
                rec.timestamp,
            ))
    return LeafSizeIndex(
        untranscluded_leaves_by_index=untranscluded_leaves_by_index,
        untranscluded_index_rollup=untranscluded_index_rollup,
        leaves_by_index=leaves_by_index,
        leaf_sizes=leaf_sizes,
        # Every leaf, not just the transcluded ones: `leaf_sizes` is deliberately
        # partial (untranscluded leaves are summed into the rollup and their
        # text dropped), but --extract-text wants the text of all of them.
        all_leaf_sizes=sizes,
        all_leaf_records=owned_records,
    )


def compute_all_content_sizes(
    dump_index: DumpIndex,
    transliterate: bool,
    transclusion_map: dict[str, set[str]],
    workers: int | None = None,
) -> ContentIndex:
    template_ns_name = dump_index.namespaces[dump_index.template_ns_id()]
    template_records = dump_index.pages_by_ns.get(dump_index.template_ns_id(), [])
    template_index = build_template_index(template_records, template_ns_name)

    cat_ns_name = dump_index.namespaces[dump_index.category_ns_id()]
    known_titles = {r.title for r in dump_index.pages_by_ns[0]}

    main_records = dump_index.pages_by_ns[0]
    main_sizes = compute_content_sizes_parallel(
        main_records, template_index, known_titles, cat_ns_name,
        transliterate=transliterate, workers=workers, progress_label="content size: Main pages",
    )
    main_categories = {rec.title: direct_categories(rec, cat_ns_name) for rec in main_records}

    index_ns_id = dump_index.index_ns_id()
    # Index (106) is the ProofreadPage extension -- absent from early dumps
    # (e.g. 2011-10, before the extension was enabled on sawikisource). No
    # namespace means no Index items, not an error.
    index_records = dump_index.pages_by_ns.get(index_ns_id, []) if index_ns_id is not None else []
    # keyed by bare title (namespace prefix stripped) below, but the pool
    # itself keys by record.title (the full "अनुक्रमणिका:..." title) --
    # remap after the fact.
    index_sizes_by_full_title = compute_content_sizes_parallel(
        index_records, template_index, known_titles, cat_ns_name,
        transliterate=transliterate, workers=workers, progress_label="content size: Index items",
    )

    def bare(title: str) -> str:
        return title.split(":", 1)[1].strip() if ":" in title else title.strip()

    index_sizes = {bare(rec.title): index_sizes_by_full_title[rec.title] for rec in index_records}
    index_categories = {bare(rec.title): direct_categories(rec, cat_ns_name) for rec in index_records}
    index_timestamps = {bare(rec.title): rec.timestamp for rec in index_records}

    leaf_size_index = compute_page_ns_sizes(
        dump_index, template_index, known_titles, cat_ns_name,
        transliterate, workers, transclusion_map,
    )

    # Fold each transcluded Main page's own <pages .../> leaf range into its
    # content size -- a Main page built by transcluding a scan (the standard
    # way large OCR'd works reach Mainspace) otherwise measures near-zero,
    # since its wikitext is literally just the <pages .../> tag (see
    # notes/proofreadpage-transclusion-undercount-fix.md).
    _augment_main_sizes_with_transclusion(main_records, main_sizes, leaf_size_index)

    return ContentIndex(
        main_sizes=main_sizes,
        main_categories=main_categories,
        index_sizes=index_sizes,
        index_categories=index_categories,
        index_timestamps=index_timestamps,
        index_page_rollup=leaf_size_index.untranscluded_index_rollup,
        page_sizes=leaf_size_index.all_leaf_sizes,
        page_records=leaf_size_index.all_leaf_records,
        leaves_by_index=leaf_size_index.leaves_by_index,
        index_records=index_records,
        untranscluded_leaves_by_index=leaf_size_index.untranscluded_leaves_by_index,
    )


def _augment_main_sizes_with_transclusion(
    main_records: list[PageRecord],
    main_sizes: dict[str, ContentSizeResult],
    leaf_size_index: LeafSizeIndex,
) -> None:
    """Adds the real content of each Main page's transcluded scan leaves into
    its own ContentSizeResult, in place. For every <pages index="X" from=A
    to=B /> tag the page carries, the matching X/N leaves (A<=N<=B) have their
    raw/content/transliterated bytes summed onto the Main page's own size --
    which for a pure-transclusion page (its wikitext is just the tag) is nearly
    all of the page's real byte count.

    Each Main page's own from=/to= range is sliced out of the shared Index's
    leaves, so sibling pages transcluding different sub-ranges of one Index
    each get only their slice, never the Index's full content
    (notes/proofreadpage-transclusion-undercount-fix.md, gotcha 4)."""
    if not leaf_size_index.leaves_by_index:
        return  # no transcluded scans in this dump (e.g. pre-ProofreadPage era)
    for rec in main_records:
        ranges = transclusion_ranges(rec.text)
        if not ranges:
            continue
        leaf_titles = resolve_transcluded_leaves(ranges, leaf_size_index.leaves_by_index)
        if not leaf_titles:
            continue
        size = main_sizes.get(rec.title)
        if size is None:
            continue
        for leaf_title in leaf_titles:
            leaf = leaf_size_index.leaf_sizes.get(leaf_title)
            if leaf is None:
                continue
            size.raw_wikitext_bytes += leaf.raw_wikitext_bytes
            size.content_bytes += leaf.content_bytes
            size.transliterated_bytes += leaf.transliterated_bytes


def count_scanned_works(root: dict) -> int:
    """How many distinct scanned works the collection has -- its `pdf_count`.

    A scan reaches the tree two ways, and the same work can arrive by both:

      - `source_indexes` on a Main page, one entry per Index the page
        transcludes (a page can cite several, and several pages can cite one)
      - an `index-item` node, an Index nobody transcludes, standing on its own

    So this counts the UNION of Index titles, not links or nodes: 755 links
    across 856 nodes resolve to 543 distinct works. Subpages cite scans too
    and are walked for it, though 36 of their 48 are already cited higher up
    -- which is the point of taking a union. One work, counted once, however
    it is reached.

    "PDF" is the wiki's own idiom here -- the destination is an Index: page,
    and its underlying media is .djvu about a fifth of the time -- but an
    Index page IS the scan from a reader's point of view, which is what the
    figure is for.
    """
    titles: set[str] = set()

    def walk(node: dict) -> None:
        if node.get("type") == "index-item":
            titles.add(node.get("title", ""))
        for src in node.get("source_indexes") or []:
            titles.add(src.get("title", ""))
        for field in ("children", "pages", "index_items", "subpages"):
            for child in node.get(field) or []:
                walk(child)

    walk(root)
    titles.discard("")
    return len(titles)


def _stats_dict(raw: int, content: int, translit: int, count: int, last_changed: str, text_count: int = 0) -> dict:
    return {
        "raw_bytes": raw,
        "content_bytes": content,
        "transliterated_bytes": translit,
        "count": count,
        "text_count": text_count,
        "last_changed": last_changed,
    }


def _empty_stats() -> dict:
    return _stats_dict(0, 0, 0, 0, "")


def _merge_stats(a: dict, b: dict) -> dict:
    return _stats_dict(
        a["raw_bytes"] + b["raw_bytes"],
        a["content_bytes"] + b["content_bytes"],
        a["transliterated_bytes"] + b["transliterated_bytes"],
        a["count"] + b["count"],
        max(a["last_changed"], b["last_changed"]) if a["last_changed"] or b["last_changed"] else "",
        a.get("text_count", 0) + b.get("text_count", 0),
    )


# Pageids whose extracted text is on THIS machine, or None if unknowable.
# Module-level rather than threaded through build_page_node/build_tree_json:
# it is a fact about the local filesystem, not about the corpus, and the tree
# builders take enough parameters already.
#
# None means "no extract directory" -- the flag is then omitted from every page
# rather than published as False everywhere, because those are different
# claims. A public checkout with no data/ is the former, and must not be read
# as an authoritative "no text exists".
_HAS_TEXT: set[int] | None = None


def load_has_text(extract_dir: Path) -> set[int] | None:
    """Pageids present in a `text_extract/` tree, read from its index.jsonl.

    The index is the only reliable link: filenames embed a lossy, sanitized
    title (`<pageid> - <Title>.txt`) that nothing downstream reconstructs.

    **Index items count too.** An untranscluded scan is a browsable item whose
    text is its leaves; the extractor folds those into a file for it, so it is
    openable and belongs here. Keyed by pageid, so the namespace prefix on its
    title (`अनुक्रमणिका:`) never has to be matched.

    **Rollup parents count as having text.** A work that is a container plus
    chapters holds no text of its own and gets no file, but it does get an
    index entry carrying `parts` -- and asking for it returns the whole work.
    So the test is presence in the index, not presence of a file: the question
    this answers is "can a reader open this", and for those 191 works the
    answer is yes.
    """
    index = extract_dir / "index.jsonl"
    if not index.exists():
        return None
    present: set[int] = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("ns") in ("main", "index") and row.get("pageid") is not None:
            present.add(row["pageid"])
    return present


def build_page_node(
    main_node: MainPageNode,
    owning_cat_id: str,
    content_index: ContentIndex,
    reverse_transclusion_map: dict[str, set[str]] | None = None,
    index_ns_name: str = "",
) -> tuple[dict, dict]:
    """Returns (json_node, own_stats) where own_stats covers this page alone
    (not its subpages). `stats` on the returned node is set to own_stats too,
    NOT a subpage-inclusive rollup -- a subpage can now also be independently
    filed under a category tag of its own (see build_category's page_jsons
    loop, "Silent subpage category divergence"), so a single rolled total
    computed here would double-count that subpage wherever both its nested
    and independently-filed occurrences are reachable from a common
    ancestor. recompute_stats_dedup (below) walks `subpages` itself, dedups
    every page id (at any depth) against every OTHER occurrence anywhere in
    the tree, and overwrites `stats` with the real rolled total -- same
    bottom-up, dedup-by-id treatment categories already get."""
    size = content_index.main_sizes.get(main_node.title)
    last_changed = main_node.record.timestamp
    own_stats = _stats_dict(
        size.raw_wikitext_bytes if size else 0,
        size.content_bytes if size else 0,
        size.transliterated_bytes if size else 0,
        1,
        last_changed,
        # A subpage (breadcrumb title, e.g. "टीका/१") isn't its own separate
        # "text" for browsing purposes -- it's part of its top-level parent's
        # work, even when it also gets independently filed here under its own
        # category tag (see "Silent subpage category divergence" above). Only
        # a true top-level title (no "/" parent) counts toward text_count.
        text_count=1 if main_node.parent_title is None else 0,
    )

    subpage_jsons = []
    for child in main_node.children:
        if child.record.redirect_target is not None:
            # A redirect stub isn't real content -- skip listing it, but its
            # own real descendants (nested past it via "/", resolved through
            # it by _resolve_redirect) still belong in the listing.
            for grandchild in child.children:
                grandchild_json, _ = build_page_node(
                    grandchild, owning_cat_id, content_index, reverse_transclusion_map, index_ns_name,
                )
                subpage_jsons.append(grandchild_json)
            continue
        child_json, _ = build_page_node(child, owning_cat_id, content_index, reverse_transclusion_map, index_ns_name)
        subpage_jsons.append(child_json)

    node = {
        "id": f"page:{main_node.title}",
        "type": "page",
        # Set only when this build could see the extracted text on disk; see
        # _HAS_TEXT. Lets a local --fulltext server offer a link without the
        # frontend or Sagarasangama reading data/ themselves.
        **({"has_text": True}
           if _HAS_TEXT is not None and main_node.record.pageid in _HAS_TEXT
           else {}),
        "title": main_node.title,
        "url": page_url(main_node.title),
        "stats": own_stats,
        # Preserved separately from "stats" (which recompute_page_dedup below
        # overwrites with the subpage-inclusive rollup) so the frontend can
        # still show this page's own un-rolled-up size once its subpages are
        # disclosed and no longer need summarizing into the parent row -- see
        # docs/app.js's renderPageLi.
        "own_stats": own_stats,
        "subpages": subpage_jsons,
    }
    # Surfaces a link back to the source scan for a reader who wants it, even
    # though the Atlas otherwise drops a transcluded Index item from display
    # entirely in favor of this Main page (see docs/about.html,
    # "Transclusion"). A Main page never transcludes an Index item directly --
    # the <pages index="..." /> tag names the Index but what's actually
    # injected is a range of that Index's own पृष्ठम्:Title/N leaf pages.
    # Sorted for determinism -- a page transcluding leaves from more than one
    # Index is rare but not prevented by ProofreadPage.
    source_titles = (reverse_transclusion_map or {}).get(main_node.title)
    if source_titles:
        node["source_indexes"] = [
            {"title": t, "url": index_url(t, index_ns_name)}
            for t in sorted(source_titles)
        ]
    return node, own_stats


def build_index_item_node(bare_title: str, content_index: ContentIndex, index_ns_name: str) -> dict:
    size = content_index.index_sizes.get(bare_title)
    rec_timestamp = content_index.index_timestamps.get(bare_title, "")
    # Its pageid, so the has_text lookup needs no namespace-prefix matching.
    pageid = next((r.pageid for r in content_index.index_records
                   if r.title.split(":", 1)[-1] == bare_title), None)
    has_text = _HAS_TEXT is not None and pageid in _HAS_TEXT
    own_stats = _stats_dict(
        size.raw_wikitext_bytes if size else 0,
        size.content_bytes if size else 0,
        size.transliterated_bytes if size else 0,
        1,
        rec_timestamp,
        # **A scan whose pages were never populated is not a text.** 76 Index
        # items here have no content anywhere -- nothing transcluded, no
        # proofread leaves, only an uploaded file -- so counting them made
        # `text_count` a count of things-we-list rather than of texts. They
        # stay listed and browsable; they simply stop claiming to be texts.
        #
        # Same judgment e-bhāratīsampat makes about its scan-only works, which
        # it excludes from text_count and hides behind "also show PDF-only".
        #
        # This moved the published figure 3805 -> 3729 on 2026-08-29.
        text_count=1 if has_text else 0,
    )
    # The Index page's own wikitext is just proofreading-status scaffolding
    # (near-zero content by design) -- the real scanned/proofread text lives
    # on its पृष्ठम्:Title/N children, rolled up separately (see
    # compute_page_ns_rollup). Merge that in so stats reflect the actual
    # scan, not just the Index page shell.
    page_rollup = content_index.index_page_rollup.get(bare_title)
    stats = _merge_stats(own_stats, page_rollup) if page_rollup else own_stats
    return {
        "id": f"index-item:{bare_title}",
        "type": "index-item",
        "title": bare_title,
        "url": index_url(bare_title, index_ns_name),
        # An untranscluded scan whose leaves were folded into a file for it.
        **({"has_text": True} if has_text else {}),
        "stats": stats,
    }


def build_category_membership_maps(content_index: ContentIndex) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """category title -> list of Main page titles / Index bare titles directly
    tagged with that category. Excluded/maintenance categories are dropped
    from the tag lists themselves (a page tagged [[वर्गः:निष्कासनाय]] doesn't
    file it under a node that will never exist in the graph)."""
    pages_by_cat: dict[str, list[str]] = {}
    for title, cats in content_index.main_categories.items():
        for cat in cats:
            if is_excluded_category(cat):
                continue
            pages_by_cat.setdefault(cat, []).append(title)

    index_items_by_cat: dict[str, list[str]] = {}
    for title, cats in content_index.index_categories.items():
        for cat in cats:
            if is_excluded_category(cat):
                continue
            index_items_by_cat.setdefault(cat, []).append(title)

    return pages_by_cat, index_items_by_cat


ORPHAN_BUCKET_TITLE = "असम्बद्धवर्गीकृतम्"


def build_tree_json(
    dump_index: DumpIndex,
    graph: CategoryGraph,
    main_nodes: dict[str, MainPageNode],
    transclusion_map: dict[str, set[str]],
    content_index: ContentIndex,
    reverse_transclusion_map: dict[str, set[str]] | None = None,
) -> dict:
    index_ns_id = dump_index.index_ns_id()
    # Same ProofreadPage-extension caveat as above: only used to build
    # index_url() links, and only ever dereferenced against real Index
    # items, which won't exist if the namespace itself doesn't.
    index_ns_name = dump_index.namespaces[index_ns_id] if index_ns_id is not None else ""
    category_ns_name = dump_index.namespaces[dump_index.category_ns_id()]
    pages_by_cat, index_items_by_cat = build_category_membership_maps(content_index)

    emitted_ids: dict[str, str] = {}  # category title -> id of its first (real) emission

    # Categories reachable downward from वर्गसर्वस्वम् by category descent
    # alone -- the "central" corpus, as opposed to the orphan-only clusters
    # swept up later into असम्बद्धवर्गीकृतम्. Computed here as a pure graph
    # traversal rather than read off emitted_ids, because emitted_ids only
    # becomes the reachable set *after* build_category(root) returns -- and
    # the breadcrumb-subpage suppression below needs the answer during that
    # same pass. Memoized and cycle-safe (see CategoryGraph).
    central_categories = graph.reachable_descendants(graph.root_title)

    # Per-title memo for _work_root: the topmost ancestor of a breadcrumb
    # subpage, i.e. the page node the whole work hangs off. Filled lazily,
    # since this is asked once per (category, page) pair.
    work_root_memo: dict[str, str] = {}

    def _work_root(title: str) -> str:
        """Walk main_nodes' parent_title chain to the top and return the work
        root's title. build_main_tree guarantees no cycles by construction
        (a candidate resolving to the title itself is rejected), but guard
        anyway, matching _resolve_ancestor's defensiveness -- on a cycle,
        stop and report the last title seen."""
        cached = work_root_memo.get(title)
        if cached is not None:
            return cached
        chain: list[str] = []
        seen: set[str] = set()
        cur = title
        while True:
            if cur in work_root_memo:
                root_title = work_root_memo[cur]
                break
            if cur in seen:
                root_title = cur  # cycle guard -- shouldn't happen
                break
            seen.add(cur)
            chain.append(cur)
            node = main_nodes.get(cur)
            if node is None or node.parent_title is None:
                root_title = cur
                break
            cur = node.parent_title
        for t in chain:
            work_root_memo[t] = root_title
        return root_title

    def _root_is_central(title: str) -> bool:
        """True when this page's work root is reachable from वर्गसर्वस्वम् --
        i.e. the properly-nested version of this page is browsable from the
        central category tree, so a flat duplicate listing adds nothing."""
        root_title = _work_root(title)
        return any(
            c in central_categories
            for c in content_index.main_categories.get(root_title, set())
        )

    def cat_id(title: str) -> str:
        return "cat:" + title

    # Categories whose first (real) emission was pruned as suppression-emptied.
    # A later filing of the same category must be pruned too, not emitted as a
    # category-pointer -- its points_to would dangle at an id no longer in the
    # tree, which recompute_stats_dedup silently scores as empty stats and the
    # frontend renders as a broken "see also".
    pruned_titles: set[str] = set()

    def build_category(title: str) -> dict | None:
        node_id = cat_id(title)
        if title in pruned_titles:
            return None
        if title in emitted_ids:
            return {
                "id": node_id + ":pointer",
                "type": "category-pointer",
                "title": title,
                "url": category_url(title, category_ns_name),
                "points_to": emitted_ids[title],
                "stats": None,  # filled in by rollup pass
            }
        emitted_ids[title] = node_id

        cat_node = graph.nodes.get(title)
        children = []
        if cat_node is not None:
            for child_title in sorted(cat_node.children):
                child = build_category(child_title)
                if child is not None:  # None == pruned as suppression-emptied
                    children.append(child)

        # Every category that directly tags a page/Index item builds and shows
        # its own full, real node -- a page tagged into 2+ categories is a
        # genuinely true member of each, not a redundant duplicate (unlike a
        # multi-parented *category*, whose descendant content really is
        # identical everywhere). No pointer/skip logic at this level; dedup
        # for ancestor rollups happens in a separate pass below
        # (recompute_stats_dedup) that sums over the *distinct* set of page/
        # Index-item ids reachable from a node, so a page counted here and
        # again at a sibling category is still only counted once wherever
        # their paths converge.
        #
        # A breadcrumb-subpage (main_node.parent_title is not None) is
        # normally only reachable by nesting inside its parent's own page
        # node (see build_page_node's recursion below), not filed here
        # directly -- its own direct tags would otherwise be silently
        # dropped for filing purposes. Surface it here too, exactly like a
        # top-level multi-tagged page, but only when that flat listing is
        # the *only* way in. Two tests suppress it otherwise:
        #
        #  1. Its immediate parent already carries this same tag, so browsing
        #     the parent's node under this very category already surfaces
        #     this subpage via its nested `subpages` list. Cheap, and catches
        #     cases (2) doesn't subsume.
        #  2. Its work root -- the top of its parent_title chain -- carries at
        #     least one tag that is itself reachable from वर्गसर्वस्वम्. Then
        #     the whole work is browsable in properly nested form somewhere in
        #     the central tree, and this flat dump of every chapter is pure
        #     noise beside it. The Garuḍapurāṇa is the motivating case: the
        #     tag chain is disjoint (गरुडपुराणम् is tagged गरुडपुराणम्, the
        #     आचारकाण्डः subpage is tagged nothing, its 240 chapters are each
        #     tagged आचारकाण्डः), so (1) never fires, yet browsing
        #     पुराणानि > गरुडपुराणम् already reaches every one of them nested.
        #
        # Keying (2) on central *reachability* rather than on tags is what
        # makes it safe: some works' nested form lives only in the orphan
        # bucket (विष्णुपुराणम्, गर्गसंहिता), and for those the flat listing
        # really is the only path in, so it stays. It also self-corrects --
        # tag such a work root into the central tree upstream and its
        # duplicate listings suppress themselves on the next run.
        page_jsons = []
        suppressed_any = False
        for page_title in sorted(pages_by_cat.get(title, [])):
            main_node = main_nodes.get(page_title)
            if main_node is None:
                continue  # not a Main record
            if main_node.record.redirect_target is not None:
                continue  # redirect stub -- a stray/incidental category tag doesn't make it real content
            if main_node.parent_title is not None:
                parent_tags = content_index.main_categories.get(main_node.parent_title, set())
                if title in parent_tags:
                    suppressed_any = True
                    continue  # already discoverable via the parent's own filing under this category
                if _root_is_central(page_title):
                    suppressed_any = True
                    continue  # already discoverable nested under its work root in the central tree
            page_json, _ = build_page_node(main_node, node_id, content_index, reverse_transclusion_map, index_ns_name)
            page_jsons.append(page_json)

        index_jsons = []
        for bare_title in sorted(index_items_by_cat.get(title, [])):
            if is_transcluded(bare_title, transclusion_map):
                continue  # published elsewhere in Main -- drop the raw Index item per spec
            index_jsons.append(build_index_item_node(bare_title, content_index, index_ns_name))

        if suppressed_any and not page_jsons and not index_jsons and not children:
            # Suppression emptied this category outright -- e.g. आचारकाण्डः,
            # whose entire membership is the 240 गरुडपुराणम् chapters now
            # shown nested under their work root instead. Nothing in the
            # frontend filters empties, so keeping it would render a dead-end
            # row. Only prune where suppression is the cause -- a category
            # that's genuinely empty on the wiki keeps behaving exactly as it
            # does today.
            #
            # 46 categories empty out this way in the 2026-07-01 dump, but
            # only the 21 that are centrally reachable actually reach this
            # return: the other 25 are never emitted in the first place
            # (unreachable from root, and the orphan sweep skips them since
            # all their pages are centrally reachable via their work root).
            #
            # emitted_ids keeps its entry (registered above, before this
            # point) on purpose: the orphan sweep below reads emitted_ids as
            # the reachable-category set, and a pruned category is still
            # reachable -- its pages are all emitted elsewhere in the central
            # tree, so dropping the entry would misfile them as orphans.
            pruned_titles.add(title)
            return None

        return {
            "id": node_id,
            "type": "category",
            "title": title,
            "url": category_url(title, category_ns_name),
            "children": children,
            "pages": page_jsons,
            "index_items": index_jsons,
            "stats": None,  # filled in by recompute_stats_dedup below
        }

    root = build_category(graph.root_title)
    if root is None:  # only possible if the entire corpus suppressed away
        raise RuntimeError(f"root category '{graph.root_title}' built empty")

    # Content unreachable from root by category descent: a page/Index item
    # whose only category tag(s) are themselves never filed under any parent
    # reachable from वर्गसर्वस्वम् (an orphaned category -- e.g. a whole
    # sub-tree that exists on the wiki but was never linked in), or that
    # carries no category tag at all. build_category() above already walked
    # every category reachable from root, so emitted_ids now IS the
    # reachable set -- same trick scrape.py's build_orphan_bucket used with
    # its seen_cats dict. Reusing build_category for orphan-category roots
    # means a category shared between two orphan clusters (or one that's
    # simply a deeper, not-yet-visited part of an orphan tree) gets the same
    # category-pointer/dedup handling for free.
    #
    # A page/Index item is reachable iff at least one of its direct tags is
    # itself reachable (in emitted_ids) -- unlike the old page-pointer scheme,
    # there's no "first-claimed-it" bookkeeping to consult here anymore, since
    # every reachable category independently re-emits its own direct tags.
    # Redirect stubs are excluded here too, same reasoning as build_page_node's
    # subpage skip above -- a redirect has no category tags of its own, so
    # without this it would always be "reachable via zero tags" and get
    # dumped into the orphan bucket as if it were real uncategorized content.
    orphan_main_titles = sorted(
        t for t, n in main_nodes.items()
        if n.parent_title is None and n.record.redirect_target is None
    )
    orphan_index_titles = sorted(
        t for t in content_index.index_categories if not is_transcluded(t, transclusion_map)
    )

    orphan_cat_roots: dict[str, None] = {}  # ordered set of category titles to crawl as fresh roots
    orphan_zero_cat_main: list[str] = []
    orphan_zero_cat_index: list[str] = []
    for t in orphan_main_titles:
        cats = [c for c in content_index.main_categories.get(t, set()) if not is_excluded_category(c)]
        if any(c in emitted_ids for c in cats):
            continue  # reachable via at least one real tag -- already emitted above
        if cats:
            for c in cats:
                orphan_cat_roots.setdefault(c, None)
        else:
            orphan_zero_cat_main.append(t)
    for t in orphan_index_titles:
        cats = [c for c in content_index.index_categories.get(t, set()) if not is_excluded_category(c)]
        if any(c in emitted_ids for c in cats):
            continue
        if cats:
            for c in cats:
                orphan_cat_roots.setdefault(c, None)
        else:
            orphan_zero_cat_index.append(t)

    orphan_children = []
    for orphan_root_title in orphan_cat_roots:
        if orphan_root_title in emitted_ids:
            continue  # already swept in by an earlier orphan root this same loop
        orphan_root = build_category(orphan_root_title)
        if orphan_root is not None:  # None == pruned as suppression-emptied
            orphan_children.append(orphan_root)
    orphan_children.sort(key=lambda n: n["title"])

    orphan_page_jsons = []
    for t in orphan_zero_cat_main:
        main_node = main_nodes[t]
        page_json, _ = build_page_node(main_node, cat_id(ORPHAN_BUCKET_TITLE), content_index, reverse_transclusion_map, index_ns_name)
        orphan_page_jsons.append(page_json)

    orphan_index_jsons = [
        build_index_item_node(t, content_index, index_ns_name) for t in orphan_zero_cat_index
    ]

    orphan_bucket = None
    if orphan_children or orphan_page_jsons or orphan_index_jsons:
        orphan_bucket = {
            "id": cat_id(ORPHAN_BUCKET_TITLE),
            "type": "category",
            "title": ORPHAN_BUCKET_TITLE,
            "children": orphan_children,
            "pages": orphan_page_jsons,
            "index_items": orphan_index_jsons,
            "stats": None,  # filled in below
        }
        root["children"].append(orphan_bucket)

    # Compute every category's stats bottom-up as the sum over the *distinct*
    # set of page/Index-item ids reachable from it -- not a naive sum of
    # children's stats, which would double-count a page/Index item filed
    # directly under two categories that both appear beneath this node (see
    # build_category above: every reachable category now independently
    # re-emits its own direct tags in full, on purpose, so this pass is the
    # only place dedup happens). Mirrors old scrape.py's attach_stats.
    # Memoized by node id since a category-pointer's target subtree, or a
    # category shared by two orphan-root sweeps, can be reached more than once.
    by_id: dict[str, dict] = {}

    def index_by_id(node: dict) -> None:
        by_id[node["id"]] = node
        for ch in node.get("children", []):
            index_by_id(ch)

    index_by_id(root)  # orphan_bucket is already among root["children"] by this point

    memo: dict[str, tuple[dict, dict, dict]] = {}

    def rewrite_page_subtree(node: dict) -> None:
        """Write each already-memoized node's correct stats into THIS
        occurrence's own dict object, recursing through `subpages` -- used on
        a cache hit (see recompute_page_dedup) where an entire duplicate
        subtree (e.g. a top-level page independently filed under 2+
        categories, each a fresh build_page_node() call -- see
        build_category's page_jsons loop) needs its stats fields corrected
        without redoing the O(n) computation, which the memo already has."""
        node["stats"] = memo[node["id"]][0]
        for sp in node.get("subpages", []):
            rewrite_page_subtree(sp)

    def recompute_page_dedup(node: dict) -> tuple[dict, dict]:
        """Returns (stats, page_stats_by_id) for the distinct set of page ids
        reachable from page node `node` -- itself plus every subpage, at any
        depth. A subpage can be reached two ways: nested here, or as its own
        independently-filed occurrence elsewhere in the tree (see
        build_category's page_jsons loop) -- memoized by id (shared with
        recompute_stats_dedup's memo) so the O(n) computation only happens
        once per id; every later occurrence (a physically different dict
        object, since build_page_node builds a fresh subtree per call) still
        needs its own stats fields corrected via rewrite_page_subtree, since
        skipping the computation must not mean skipping the write-back."""
        node_id = node["id"]
        if node_id in memo:
            stats, page_stats, _ = memo[node_id]
            rewrite_page_subtree(node)
            return stats, page_stats

        page_stats: dict[str, dict] = {node_id: node["stats"]}
        for sp in node.get("subpages", []):
            _, sp_page_stats = recompute_page_dedup(sp)
            for pid, s in sp_page_stats.items():
                page_stats.setdefault(pid, s)

        stats = _empty_stats()
        for s in page_stats.values():
            stats = _merge_stats(stats, s)

        node["stats"] = stats
        memo[node_id] = (stats, page_stats, {})
        return stats, page_stats

    def recompute_stats_dedup(node: dict) -> tuple[dict, dict, dict]:
        """Returns (stats, page_stats_by_id, index_stats_by_id) for the distinct
        set of page/Index-item ids reachable from `node`, each mapped to its own
        (already-correct, per-page) stats dict."""
        node_id = node["id"]
        if node_id in memo:
            return memo[node_id]

        if node["type"] == "category-pointer":
            target = by_id.get(node["points_to"])
            result = recompute_stats_dedup(target) if target else (_empty_stats(), {}, {})
            node["stats"] = result[0]
            memo[node_id] = result
            return result

        page_stats: dict[str, dict] = {}
        for p in node.get("pages", []):
            _, p_page_stats = recompute_page_dedup(p)
            for pid, s in p_page_stats.items():
                page_stats.setdefault(pid, s)
        index_stats: dict[str, dict] = {it["id"]: it["stats"] for it in node.get("index_items", [])}

        for child in node.get("children", []):
            _, child_page_stats, child_index_stats = recompute_stats_dedup(child)
            for pid, s in child_page_stats.items():
                page_stats.setdefault(pid, s)
            for iid, s in child_index_stats.items():
                index_stats.setdefault(iid, s)

        stats = _empty_stats()
        for s in page_stats.values():
            stats = _merge_stats(stats, s)
        for s in index_stats.values():
            stats = _merge_stats(stats, s)

        node["stats"] = stats
        result = (stats, page_stats, index_stats)
        memo[node_id] = result
        return result

    recompute_stats_dedup(root)  # recurses into orphan_bucket too, as one of root's children

    # वर्गसर्वस्वम् (the literal MediaWiki category root) isn't a useful node to
    # show readers -- once the junk siblings are excluded (see
    # EXCLUDED_CATEGORIES) and धर्मशास्त्रम् is folded into ग्रन्थाः (see
    # refile_category in main()), root's only *category-tree* child is
    # ग्रन्थाः, which just adds an extra meaningless click. Splice ग्रन्थाः's
    # own contents up to root directly, same as scrape.py did (it crawled
    # starting *at* ग्रन्थाः and never emitted it as a node at all) -- but
    # keep any OTHER real root children as siblings of ग्रन्थाः's contents
    # (currently just the orphan bucket, असम्बद्धवर्गीकृतम्, appended above;
    # scrape.py kept its equivalent अवर्गीकृतम्/OCR buckets as siblings of
    # the ग्रन्थाः-rooted tree the same way).
    granth = next((c for c in root["children"] if c["title"] == "ग्रन्थाः" and c["type"] == "category"), None)
    all_stats = root.get("stats") or _empty_stats()  # true total: every root child, orphan bucket included
    if granth is not None:
        other_siblings = [c for c in root["children"] if c is not granth]
        root = {
            "id": "root",
            "type": "category",
            "title": "ग्रन्थाः (धर्मशास्त्राणि च)",
            "children": granth["children"] + other_siblings,
            "pages": granth["pages"],
            "index_items": granth["index_items"],
            # root's own stats intentionally cover only the central,
            # well-categorized tree (ग्रन्थाः) -- same convention scrape.py
            # used (attach_stats ran on the central tree before अवर्गीकृतम्/
            # OCR buckets were appended as siblings). The orphan bucket's
            # own stats are still real and shown on its own node. The true
            # total across every root child (including the orphan bucket)
            # is `all_stats`, computed above before this splice discards it.
            "stats": granth["stats"],
        }

    all_stats = dict(all_stats)
    all_stats["pdf_count"] = count_scanned_works(root)

    return {"root": root, "all_stats": all_stats}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, nargs="?", help="path to the uncompressed dump XML")
    parser.add_argument("--out", type=Path, default=Path("docs/data/tree.json"),
                         help="output path (default: docs/data/tree.json, the frontend's data dir)")
    parser.add_argument("--no-transliterate", action="store_true",
                         help="skip skrutable transliteration (faster, for quick iteration)")
    parser.add_argument("--workers", type=int, default=None,
                         help="worker processes for content-size computation (default: os.cpu_count())")
    parser.add_argument("--extract-text", type=Path, nargs="?",
                         const=Path("data/text_extract"), default=None,
                         help="also write the corpus text to this directory "
                              "(default data/text_extract): deva/ and iast/, "
                              "split into main/ and page/. Costs only the file "
                              "writes -- the text is already computed for the "
                              "size metric and otherwise discarded.")
    args = parser.parse_args()

    run_start = time.time()

    xml_path = args.xml_path
    if xml_path is None:
        candidates = sorted(Path("data/dump/1_current_format_live").glob("sawikisource-*.xml"))
        if not candidates:
            print("no dump/1_current_format_live/*.xml found", file=sys.stderr)
            sys.exit(1)
        # Newest, not oldest: the ISO date in sawikisource-<date>-... sorts
        # lexicographically, so the last entry is the most recent month.
        # pipeline.fetch's _remove_stale_files normally leaves exactly one
        # export here, but if a prior month survives (interrupted fetch, a
        # hand-placed dump), picking candidates[0] would silently rebuild the
        # old month while reporting success.
        if len(candidates) > 1:
            print(f"{len(candidates)} dumps present in dump/1_current_format_live; "
                  f"using newest: {candidates[-1].name}", file=sys.stderr)
        xml_path = candidates[-1]

    print(f"parsing {xml_path}", file=sys.stderr)
    # namespaces_of_interest=None (the default) resolves Main/Category/Index/
    # Template ids from the dump's own siteinfo -- see DumpIndex in parse_dump.py.
    dump_index = parse_dump(xml_path)

    cat_ns_name = dump_index.namespaces[dump_index.category_ns_id()]

    print("building Main-namespace tree...", file=sys.stderr)
    main_nodes = build_main_tree(dump_index.pages_by_ns[0])

    print("building category graph...", file=sys.stderr)
    graph = build_category_graph(dump_index.pages_by_ns[14], cat_ns_name)
    if graph.root_title not in graph.nodes:
        print(f"error: root category '{graph.root_title}' not found", file=sys.stderr)
        sys.exit(1)

    # धर्मशास्त्रम् is filed on the live site as a top-level sibling of ग्रन्थाः
    # under root -- not useful for readers, since it's really a body of
    # ग्रन्थाः-type texts. Fold it in as a subcategory instead (scrape.py made
    # the same call). See refile_category's docstring for details.
    refile_category(graph, "धर्मशास्त्रम्", new_parent_title="ग्रन्थाः", old_parent_title=graph.root_title)

    print("building transclusion map...", file=sys.stderr)
    transclusion_map = build_transclusion_map(dump_index.pages_by_ns[0])
    reverse_transclusion_map = build_reverse_transclusion_map(dump_index.pages_by_ns[0])

    print("computing content sizes (this is the slow step)...", file=sys.stderr)
    content_index = compute_all_content_sizes(
        dump_index, transliterate=not args.no_transliterate,
        transclusion_map=transclusion_map, workers=args.workers,
    )

    if args.extract_text:
        # Before tree assembly, because this is the one consumer of the TEXT
        # rather than the byte counts, and the text is in hand right now --
        # nothing below reads it, and `content_cache` blanks it outright.
        # Lives in the private `rivulet` package; exits 2 if it is absent.
        # See pipeline/fulltext.py -- nothing above this line needs it, so a
        # checkout without rivulet still builds the tree and the sizes.
        from pipeline.fulltext import load_writer
        write_text_extract = load_writer()
        print(f"writing text extract to {args.extract_text}...", file=sys.stderr)
        # title -> the scan leaves it transcludes, resolved exactly as
        # _augment_main_sizes_with_transclusion resolves them for the byte
        # rollup. A page whose text is 205 transcluded leaves must be openable,
        # not just countable -- the Atlas already points at it.
        transcluded_leaves = {}
        if content_index.leaves_by_index:
            for rec in dump_index.pages_by_ns[0]:
                ranges = transclusion_ranges(rec.text)
                if not ranges:
                    continue
                leaves = resolve_transcluded_leaves(
                    ranges, content_index.leaves_by_index)
                if leaves:
                    # Reading order: leaf N sorts numerically, not as a string.
                    transcluded_leaves[rec.title] = sorted(
                        leaves,
                        key=lambda t: (t.rsplit("/", 1)[0],
                                       int(m.group()) if (m := __import__("re").search(
                                           r"\d+$", t)) else 0))

        summary = write_text_extract(
            args.extract_text,
            dump_index.pages_by_ns[0], content_index.main_sizes,
            content_index.page_records, content_index.page_sizes,
            # The redirect-resolved subpage tree, so a work's parts are found
            # the way this Atlas already finds them. Title prefixes disagree:
            # वाल्मीकिरामायणम्'s kandas are titled रामायणम्/... and a prefix
            # match finds none of them.
            main_nodes=main_nodes,
            transcluded_leaves=transcluded_leaves,
            # Scans nobody has assembled into a Main page: the Atlas lists them
            # as items, so they get a fulltext too.
            index_items=content_index.index_records,
            index_leaves={r.title: content_index.untranscluded_leaves_by_index.get(
                              r.title.split(":", 1)[-1], [])
                          for r in content_index.index_records},
        )
        for label, s in summary["per_ns"].items():
            print(f"  {label:5} {s['pages']:7} pages  "
                  f"{s['content_bytes'] / 1e6:8.1f} MB deva  "
                  f"{s['translit_bytes'] / 1e6:7.1f} MB iast", file=sys.stderr)
        print(f"  written {summary['written']}, empty {summary['empty']} "
              f"(redirects, stubs, markup-only)", file=sys.stderr)
        if summary["collisions"]:
            # Every write succeeds on a collision, so nothing else would say a
            # page went missing. Loud by design.
            print(f"  *** {len(summary['collisions'])} PATH COLLISIONS -- "
                  f"pages lost to overwriting ***", file=sys.stderr)

    # Which pages have extracted text on THIS machine. Read even when
    # --extract-text was not passed: the extract may have been written by an
    # earlier run, and the flag describes the disk, not this invocation.
    global _HAS_TEXT
    _HAS_TEXT = load_has_text(args.extract_text or Path("data/text_extract"))
    if _HAS_TEXT is not None:
        print(f"  {len(_HAS_TEXT)} main-namespace pages have extracted text on disk",
              file=sys.stderr)

    print("assembling tree...", file=sys.stderr)
    tree = build_tree_json(dump_index, graph, main_nodes, transclusion_map, content_index, reverse_transclusion_map)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, separators=(",", ":"))

    print(f"wrote {args.out}", file=sys.stderr)
    print(f"root stats: {tree['root']['stats']}", file=sys.stderr)

    dump_date_match = re.search(r"sawikisource-(\d{4}-\d{2}-\d{2})-", xml_path.name)
    dump_date = dump_date_match.group(1) if dump_date_match else ""

    _stamp_data_version(dump_date)

    # Also cache build_tree_json's inputs, same as pipeline.backfill does for
    # every other month -- otherwise pipeline.backfill's "reuse live
    # docs/data/tree.json" shortcut (ensure_snapshot, when the requested date
    # matches this run's __content_version__) has no content-<date>.json.gz
    # to fall back on, and would need to fully reprocess the dump just to
    # get one, defeating the point of the shortcut.
    if dump_date:
        from pipeline.content_cache import build_content_cache, write_content_cache

        content_cache = build_content_cache(dump_index, content_index, dump_index.pages_by_ns[0], dump_index.pages_by_ns[14])
        content_cache_dir = Path("data/dump") / "_backfill_content_cache"
        content_cache_dir.mkdir(parents=True, exist_ok=True)
        content_cache_path = content_cache_dir / f"content-{dump_date}.json.gz"
        write_content_cache(content_cache_path, content_cache)
        print(f"wrote content cache -> {content_cache_path}", file=sys.stderr)

    elapsed = time.time() - run_start
    print(f"total run time: {elapsed:.0f}s ({elapsed / 60:.1f}m)", file=sys.stderr)


def _stamp_data_version(dump_date: str) -> None:
    """Record today's date as __data_version__ (pipeline-run date) and the
    Wikimedia dump export's own date (parsed from the source XML filename,
    e.g. sawikisource-2026-07-01-....xml -> "2026-07-01") as
    __content_version__ in docs/VERSION, alongside __code_version__ (bumped
    manually/separately). __content_version__ is deliberately just the
    dump's snapshot date -- not a rollup over page-edit timestamps, which
    the main panel already surfaces per-item on its own."""
    version_path = Path(__file__).resolve().parent.parent / "docs" / "VERSION"
    today = time.strftime("%Y-%m-%d", time.gmtime())
    lines = version_path.read_text(encoding="utf-8").splitlines() if version_path.exists() else ['__code_version__ = "0.1.0"']
    lines = [
        ln for ln in lines
        if not ln.startswith("__data_version__")
        and not ln.startswith("__content_version__")
    ]
    lines.append(f'__data_version__ = "{today}"')
    if dump_date:
        lines.append(f'__content_version__ = "{dump_date}"')
    version_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
