"""
Build stage, part 2: construct the Main-namespace subpage tree and the
Category digraph from parsed dump records (pipeline.parse_dump.DumpIndex).

Per notes/sawikisource-scraper-spec.md:
- Main-namespace parent/child is a genuine tree, derived purely by splitting
  titles on the last "/" -- no multi-parent, no cycles possible by
  construction, so rollups need no dedup logic.
- The Category graph is a manually-maintained, directed, NOT-guaranteed-
  acyclic graph (edges from [[वर्गः:Parent]] tags on each category's own
  body). Multi-parenting is real (a category can have >1 parent) and not
  everything reaches root (वर्गसर्वस्वम्) -- disconnected components exist.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.parse_dump import DumpIndex, PageRecord, category_links, is_excluded_category, parse_dump

ROOT_CATEGORY_TITLE = "वर्गसर्वस्वम्"


# ---------------------------------------------------------------------------
# Main-namespace subpage tree
# ---------------------------------------------------------------------------

@dataclass
class MainPageNode:
    record: PageRecord
    title: str  # full title, e.g. "Work/Part 1"
    parent_title: str | None  # None for a top-level (no "/") title
    children: list["MainPageNode"] = field(default_factory=list)


def _resolve_redirect(title: str, records_by_title: dict[str, PageRecord]) -> str:
    """Follows a chain of #REDIRECT pages to its final non-redirect target
    title -- e.g. "रामायणम्" (a redirect) -> "वाल्मीकिरामायणम्" (the real
    page), so a breadcrumb like "रामायणम्/बालकाण्डम्" nests under the page
    readers actually land on, not under an invisible redirect stub that
    never itself gets filed/displayed (see notes/wikisource-editing-plan.md
    -- redirect-as-subpage-parent). Stops early (returns the title as-is)
    on a cycle or a target that isn't itself a known Main-namespace record,
    since there's nothing real to resolve to."""
    seen = set()
    current = title
    while current in records_by_title and records_by_title[current].redirect_target is not None:
        if current in seen:
            return title  # cycle -- bail out to the original, unresolved title
        seen.add(current)
        current = records_by_title[current].redirect_target
    return current


def _normalize_path(title: str) -> str:
    """Strips whitespace around each "/"-separated segment, for LOOKUP ONLY.

    sa.wikisource has real titles like "अब्धिनौयानमीमांसा /चतुर्थं खण्डम्"
    (stray space before the slash) whose parent exists only in un-spaced form.
    MediaWiki itself normalizes this when resolving a subpage on the live
    wiki, so the breadcrumb is genuinely correct there and only fails here.

    Never persisted and never used as a node title -- the real, literal title
    is always what gets stored/displayed, since that's what resolves against
    Wikisource."""
    return "/".join(seg.strip() for seg in title.split("/"))


def _resolve_ancestor(
    title: str,
    records_by_title: dict[str, PageRecord],
    titles_by_normalized: dict[str, list[str]],
) -> str | None:
    """Finds the nearest existing ancestor page for a "/"-bearing title, or
    None if there genuinely isn't one (a real wiki problem -- see
    pipeline.audit's find_unresolvable_slash_paths -- never papered over by
    synthesizing a parent that doesn't exist).

    Three reasons the plain "everything before the last '/'" rule misses a
    parent that really is there:

    1. A missing *intermediate* level: "ऋग्वेदः/संहिता/सस्वरपाठः/१-५" where
       "ऋग्वेदः/संहिता/सस्वरपाठः" was never created but "ऋग्वेदः" was. We walk
       up to the nearest ancestor that does exist rather than giving up at
       the first miss.
    2. A redirect anywhere up the chain, not just at the immediate parent:
       "श्रीमद्भागवत महापुराण/स्कंध ०१/अध्यायः ०१" reaches its real home only
       by resolving the ROOT segment's redirect to श्रीमद्भागवतपुराणम्. So
       every level gets redirect-resolved, not just the first.
    3. Whitespace around a "/" (see _normalize_path).

    The exact literal parent is tried FIRST, before any normalization. This
    matters: "भविष्यपुराणम् /पर्व १ (ब्राह्मपर्व)" is itself a real page whose
    own title carries the stray space, and 226 chapters nest under it today by
    exact match. Normalizing first would look up the space-free form, miss,
    and pull all of them up to the shallow root instead -- so normalization is
    only ever a fallback equivalence, never a rewrite of the path being
    resolved."""
    def _usable(candidate: str) -> str | None:
        """A resolved candidate is usable only if it's a real page AND isn't
        the title itself -- कथासरित्सागरः/लम्बकः १३ is a redirect pointing DOWN
        at its own child, which would otherwise make that child its own parent
        and drop it out of the tree entirely."""
        resolved = _resolve_redirect(candidate, records_by_title)
        if resolved != title and resolved in records_by_title:
            return resolved
        return None

    # 1. Exact immediate parent -- today's behavior, preserved verbatim.
    hit = _usable(title.rsplit("/", 1)[0])
    if hit is not None:
        return hit

    # 2. Walk up the remaining ancestors, longest (nearest) first, allowing a
    #    whitespace-normalized match at each level.
    segments = _normalize_path(title).split("/")
    for i in range(len(segments) - 1, 0, -1):
        ancestor = "/".join(segments[:i])
        hit = _usable(ancestor)
        if hit is not None:
            return hit
        for real_title in titles_by_normalized.get(ancestor, ()):
            hit = _usable(real_title)
            if hit is not None:
                return hit

    return None


_DEVANAGARI_DIGITS = "०१२३४५६७८९"


def _to_devanagari(n: int) -> str:
    return "".join(_DEVANAGARI_DIGITS[int(d)] for d in str(n))


def _from_any_digits(s: str) -> int | None:
    """Reads a run of either ASCII or Devanagari digits as an int. Titles in a
    single family are not consistent about which script they use for numbers."""
    out = ""
    for ch in s:
        if ch in _DEVANAGARI_DIGITS:
            out += str(_DEVANAGARI_DIGITS.index(ch))
        elif ch.isdigit():
            out += ch
        else:
            return None
    return int(out) if out else None


def _mahabharata_parent(m: "re.Match[str]") -> str | None:
    """महाभारतम्-02-सभापर्व-001 -> महाभारतम्/सभापर्व.

    The parva NAME is taken straight from the title, not looked up from the
    parva number -- the number is redundant with it and never disagrees (all
    18 numbers map to exactly one name in the 2026-07 dump), so trusting the
    name avoids baking a number->name table that could silently drift."""
    return f"महाभारतम्/{m.group('parva')}"


def _rgveda_parent(m: "re.Match[str]") -> str | None:
    """ऋग्वेदः सूक्तं १.१ -> ऋग्वेदः मण्डल १.

    The sūkta number is itself dotted hierarchy (maṇḍala.sūkta), so the
    destination comes from the part BEFORE the dot. Maṇḍala pages are titled
    with Devanagari numerals, hence the re-rendering rather than reusing the
    matched text verbatim."""
    mandala = _from_any_digits(m.group("mandala"))
    if mandala is None:
        return None
    return f"ऋग्वेदः मण्डल {_to_devanagari(mandala)}"


# (compiled pattern, match -> destination title, human label for the audit)
#
# A deliberate, narrow exception to the breadcrumb-first rule: these two works
# encode a parent/child decomposition in their titles using a separator
# MediaWiki assigns no meaning to ("-" or " "), so _resolve_ancestor can never
# see it. Both were verified against the 2026-07-01 dump before being added:
# every match resolves to a destination page that ALREADY EXISTS on the wiki
# (18 महाभारतम्/<parva> pages, 10 ऋग्वेदः मण्डल <N> pages, all real, none a
# redirect), the patterns match 2314/2314 and 1028/1028 pages respectively with
# no exceptions, and ऋग्वेदः's per-maṇḍala counts reproduce the canonical
# saṃhitā exactly (191/43/62/58/87/75/104/103/114/191). महाभारतम्/आदिपर्व/००१
# is one chapter an editor already converted to the "/" form by hand, and the
# wiki carries 18 redirects from the hyphen form to the slash form -- so this
# transcribes a relationship sa.wikisource already asserts elsewhere rather
# than inventing one.
#
# This is NOT a general separator rule and must not become one. Below these two
# families the shapes stop being regular -- stems that don't exist at all
# (समराङ्गणसूत्रधार अध्याय, दशक), chapter RANGES rather than single chapters
# (अष्टाङ्गसंग्रहः ... अध्याय १-५), and titles where a naive split lands
# mid-parenthetical (सिद्धान्तकौमुदी (बालमनोरमा पूर्व २-२)) -- and 2,544 flat
# titles have an inferred stem that coincidentally IS a real page, so
# "the stem exists" cannot discriminate a chapter from a shared prefix. A rule
# general enough to catch these two would silently mis-nest hundreds of others.
# Everything not listed here stays flat and is reported by pipeline.audit's
# find_separator_family_candidates for a human to fix upstream with page moves.
#
# Adding a row is cheap; verify first that the destination exists and that the
# pattern's match count equals the family's real size. pipeline.audit asserts
# both on every run, so a row that goes stale (upstream cleanup, a page move)
# surfaces as a loud audit line instead of a quietly wrong tree.
FLAT_FAMILY_PATTERNS: list[tuple["re.Pattern[str]", object, str]] = [
    (
        re.compile(r"^महाभारतम्-(?P<num>\d+)-(?P<parva>[^-]+)-(?P<chapter>.+)$"),
        _mahabharata_parent,
        "महाभारतम्-NN-<parva>-NNN → महाभारतम्/<parva>",
    ),
    (
        # Accepts BOTH the visarga "ऋग्वेदः" and the ASCII-colon "ऋग्वेद:"
        # spelling of the stem. The sūkta pages were titled with a literal ":"
        # until a mass rename in 2017-08 moved all 1,028 to the correct
        # visarga; the colon titles survive today only as redirects to the
        # visarga ones. Matching just the visarga form is correct for the
        # current dump but leaves every historical month before 2017-08 with
        # 1,028 unnested sūktas counted as standalone texts -- the notch in the
        # backfilled text_count curve. The destination is spelled with the
        # visarga in both eras, so only the child side varies.
        re.compile(r"^ऋग्वेद[ः:] सूक्तं (?P<mandala>[०-९\d]+)\.(?P<sukta>[०-९\d]+)$"),
        _rgveda_parent,
        "ऋग्वेद[ः:] सूक्तं M.S → ऋग्वेदः मण्डल M",
    ),
]


def _resolve_flat_family(
    title: str,
    records_by_title: dict[str, PageRecord],
) -> str | None:
    """Finds a parent for a title that carries NO "/" but does encode its
    parent in some other separator, via the FLAT_FAMILY_PATTERNS allowlist.

    Returns None unless the destination is a real page in this same dump --
    redirect-resolved, and rejected if it resolves back to the title itself,
    exactly like _resolve_ancestor's _usable. A parent is never synthesized:
    if a row's destination page were ever deleted upstream, its pages fall
    back to top-level rather than nesting under a title that isn't there."""
    for pattern, to_parent, _label in FLAT_FAMILY_PATTERNS:
        m = pattern.match(title)
        if m is None:
            continue
        candidate = to_parent(m)  # type: ignore[operator]
        if candidate is None:
            continue
        resolved = _resolve_redirect(candidate, records_by_title)
        if resolved != title and resolved in records_by_title:
            return resolved
        return None  # matched the family but its destination isn't real -- stay top-level
    return None


def build_main_tree(records: list[PageRecord]) -> dict[str, MainPageNode]:
    """Returns a title -> MainPageNode map covering every Main-namespace page
    (redirects included, as leaf nodes -- callers filter/dereference as needed).
    Parent/child edges are purely a title-string convention: a node's parent is
    the nearest ancestor path that actually exists as a page in this same
    namespace, resolved through any redirect chain at every level (see
    _resolve_ancestor). A "/"-bearing title with no existing ancestor at all is
    left as its own top-level node, never synthesized into a parent that
    doesn't exist as real content.

    Titles WITHOUT a "/" are top-level by default, with one narrow exception:
    the FLAT_FAMILY_PATTERNS allowlist, covering two works that encode their
    chapter hierarchy with a separator MediaWiki gives no meaning to. See that
    table's comment for why it is an explicit allowlist and not a general rule.
    """
    records_by_title = {rec.title: rec for rec in records}

    # Whitespace-normalized form -> the real title(s) sharing it, so a
    # "Work /Part" style ancestor can still be found from a "Work/Part" probe.
    # A normalized form can legitimately map to several real titles (73 such
    # collisions in the 2026-07 dump), so this is a list, tried in order.
    titles_by_normalized: dict[str, list[str]] = {}
    for title in records_by_title:
        titles_by_normalized.setdefault(_normalize_path(title), []).append(title)

    nodes: dict[str, MainPageNode] = {}
    for rec in records:
        parent_title = None
        if "/" in rec.title:
            parent_title = _resolve_ancestor(rec.title, records_by_title, titles_by_normalized)
        else:
            parent_title = _resolve_flat_family(rec.title, records_by_title)
        nodes[rec.title] = MainPageNode(record=rec, title=rec.title, parent_title=parent_title)

    for node in nodes.values():
        if node.parent_title is not None and node.parent_title in nodes:
            nodes[node.parent_title].children.append(node)
        else:
            node.parent_title = None  # no real parent page -- treat as top-level

    return nodes


def main_tree_roots(nodes: dict[str, MainPageNode]) -> list[MainPageNode]:
    return [n for n in nodes.values() if n.parent_title is None]


# ---------------------------------------------------------------------------
# Category digraph
# ---------------------------------------------------------------------------

@dataclass
class CategoryNode:
    title: str  # bare title, no "वर्गः:" prefix
    record: PageRecord | None  # None if referenced (e.g. as a parent) but never itself a page in the dump
    parents: set[str] = field(default_factory=set)
    children: set[str] = field(default_factory=set)


@dataclass
class CategoryGraph:
    nodes: dict[str, CategoryNode]
    root_title: str

    def reachable_descendants(self, title: str, _memo: dict[str, frozenset] | None = None,
                               _visiting: set[str] | None = None) -> frozenset[str]:
        """Memoized set of all category titles reachable downward from `title`
        (title itself included). Cycle-safe: a category currently being
        visited on the current path is treated as contributing no further
        descendants when re-encountered, breaking the cycle rather than
        recursing forever.
        """
        if _memo is None:
            _memo = {}
        if _visiting is None:
            _visiting = set()
        if title in _memo:
            return _memo[title]
        if title in _visiting:
            return frozenset()  # cycle guard
        _visiting.add(title)

        result = {title}
        node = self.nodes.get(title)
        if node is not None:
            for child in node.children:
                result |= self.reachable_descendants(child, _memo, _visiting)

        _visiting.discard(title)
        frozen = frozenset(result)
        _memo[title] = frozen
        return frozen


def build_category_graph(records: list[PageRecord], category_ns_name: str) -> CategoryGraph:
    """Builds the category digraph from Category-namespace page bodies.
    Nodes are created both for every real Category-namespace page AND for
    any category named as a parent that has no page of its own (a common
    real case: [[वर्गः:SomeParent]] where SomeParent was never separately
    created) -- represented with record=None so it's still a valid graph
    node, just with no stats/content of its own.
    Excluded (maintenance/junk) categories per is_excluded_category are
    dropped entirely, from both nodes and any edges naming them.

    Records are iterated in sorted title order rather than the caller's
    order. The category graph is not a tree (a category can be filed under
    several parents), so when build_tree_json later walks it depth-first,
    whichever occurrence it reaches first holds the real content and the
    rest become category-pointers -- a choice that's arbitrary but must at
    least be *stable*. Callers supply records in genuinely different orders:
    dump order from parse_dump (process.py, backfill.process_dump) vs. JSON
    dict order from a content cache (content_cache.rebuild_inputs_from_cache),
    which otherwise made the same month build a differently-shaped (though
    statistically identical) tree depending on which path produced it.
    """
    nodes: dict[str, CategoryNode] = {}

    def get_or_create(title: str) -> CategoryNode:
        if title not in nodes:
            nodes[title] = CategoryNode(title=title, record=None)
        return nodes[title]

    for rec in sorted(records, key=lambda r: r.title):
        title = rec.title.split(":", 1)[1].strip() if ":" in rec.title else rec.title.strip()
        if is_excluded_category(title):
            continue
        node = get_or_create(title)
        node.record = rec

        for parent_title in category_links(rec.text, category_ns_name):
            if is_excluded_category(parent_title):
                continue
            parent_node = get_or_create(parent_title)
            parent_node.children.add(title)
            node.parents.add(parent_title)

    return CategoryGraph(nodes=nodes, root_title=ROOT_CATEGORY_TITLE)


def refile_category(graph: CategoryGraph, title: str, new_parent_title: str, old_parent_title: str) -> None:
    """Move `title` from being a direct child of `old_parent_title` to being a
    direct child of `new_parent_title` instead, editing both nodes' parent/
    child edge sets in place. Used to fold धर्मशास्त्रम् under ग्रन्थाः: on the
    live site it's filed as a top-level sibling of ग्रन्थाः under root, but
    that's an artifact of Wikisource's own category structure, not a useful
    grouping for this Atlas's readers (same call scrape.py made previously
    by injecting it as an extra child rather than following the site as-is).
    No-ops if the edge doesn't exist (e.g. upstream re-categorizes it), so a
    future dump doesn't need this call removed defensively.
    """
    node = graph.nodes.get(title)
    old_parent = graph.nodes.get(old_parent_title)
    new_parent = graph.nodes.get(new_parent_title)
    if node is None or old_parent is None or new_parent is None:
        return
    old_parent.children.discard(title)
    node.parents.discard(old_parent_title)
    new_parent.children.add(title)
    node.parents.add(new_parent_title)


def orphaned_category_titles(graph: CategoryGraph) -> list[str]:
    """Categories with zero parents that are also not the root itself --
    per the spec, these are real (disconnected components), not an error,
    and should be listed separately rather than forced under root."""
    return sorted(
        title for title, node in graph.nodes.items()
        if not node.parents and title != graph.root_title
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, nargs="?", help="path to the uncompressed dump XML")
    args = parser.parse_args()

    xml_path = args.xml_path
    if xml_path is None:
        # rglob: the exports live in data/dump/1_current_format_live/, so a
        # flat glob here matched nothing and this fallback never fired.
        candidates = sorted(p for p in Path("data/dump").rglob("sawikisource-*.xml"))
        if not candidates:
            print("no data/dump/**/*.xml found", file=sys.stderr)
            sys.exit(1)
        xml_path = candidates[0]

    dump_index: DumpIndex = parse_dump(xml_path)
    cat_ns_name = dump_index.namespaces[dump_index.category_ns_id()]

    main_nodes = build_main_tree(dump_index.pages_by_ns[0])
    roots = main_tree_roots(main_nodes)
    print(f"main tree: {len(main_nodes)} pages, {len(roots)} top-level (no-parent) titles", file=sys.stderr)

    graph = build_category_graph(dump_index.pages_by_ns[14], cat_ns_name)
    print(f"category graph: {len(graph.nodes)} nodes", file=sys.stderr)

    if graph.root_title not in graph.nodes:
        print(f"warning: root category '{graph.root_title}' not found in graph", file=sys.stderr)
    else:
        descendants = graph.reachable_descendants(graph.root_title)
        print(f"reachable from root '{graph.root_title}': {len(descendants)} categories", file=sys.stderr)

    orphans = orphaned_category_titles(graph)
    print(f"orphaned (no-parent, non-root) categories: {len(orphans)}", file=sys.stderr)


if __name__ == "__main__":
    main()
