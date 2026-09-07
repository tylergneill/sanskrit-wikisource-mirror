# The last of the fulltext coverage: two fixes

**Status: NOT BUILT — and rescoped. Read the last section first.** Attempt 1
was built and reverted (2026-08-29). On 2026-08-31 the problem was re-measured
and is ~10x larger than this document's framing: the real defect is 770
chapter-pages presenting as whole works, not 74 items missing a link. Nothing
below should be implemented as written. It is kept because its analysis of the
five assembly forms, and its warnings about fabricated works, remain correct
and hard-won.

## Where coverage actually stands

Five ways a work is assembled. Four are handled (`pipeline/process.py`,
rivulet's `extract/wikisource/text_extractor.py`):

| form | how the parts are found |
| --- | --- |
| subpage children | `main_nodes`, the redirect-resolved subpage tree |
| redirect-renamed children | same — `वाल्मीकिरामायणम्` holds `रामायणम्/…` |
| transcluded scan leaves | `resolve_transcluded_leaves` |
| untranscluded Index items | `untranscluded_leaves_by_index` |

**Of items visible in Sāgarasaṅgama's search, 3655/3729 (98%) carry a `txt`
link.** The other two collections are at 100%.

Unpopulated scans no longer count against this: an Index item with no content
anywhere is flagged `po` and hidden by default, the same treatment
e-bhāratīsampat gives its scan-only works (commit `5e338c6`). That moved the
published `text_count` 3805 → 3729.

**These are called "image only" here, not "PDF only"** — the scans are `.pdf`,
`.djvu` and `.tif` alike, while e-bhāratīsampat's really are all PDFs. The flag
in `search.json` is `po` for both; only the label differs.

## What is left: 74 items

Every remaining visible item without a fulltext link:

```
 26   link-list pages that WOULD qualify for rollup      <- fix 1
 24   header/stub pages, no wikilinks at all             nothing to point at
 21   link-lists where at least one link is a redlink    partial work
  6   link-lists with only one link                      not a work
  3   link-lists whose targets have no text              nothing to point at
  ——
 80   (74 visible; 6 overlap with items counted elsewhere)
```

Plus **8 items with content under 5 KB** (down to 84 bytes) that are too small
to be real works — `मुखपुटम्` (the main page), `गणितसारसङ्ग्रहः` at 102 bytes.

So the realistic ceiling is **~99%**, not 100%. Most of what remains has no
text anywhere in the corpus — a limit of Wikisource, not of this pipeline.

---

# Fix 1 — link-list rollup (26 items, 98% → ~99%)

## What it is

A page that is a header plus a **bulleted list of `[[wikilinks]]`** to
separately-titled works. `तन्त्रालोकः` is 1420 bytes of wikitext linking 37
āhnikas, each its own Main page with its own text. It strips to zero content,
so it has no fulltext — while the Atlas still lists it.

## Why it needs a test, unlike the other four

The handled forms rest on a structural claim of ownership:

- a subpage title (`Work/Chapter`) says "I am part of Work"
- `<pages index="X.djvu" from=1 to=205/>` says "my content is X's leaves 1–205"

**A wikilink says nothing.** `[[X]]` means "see X" as readily as "X is my
chapter 3". Rolling up every link-list would concatenate genre anthologies into
works that do not exist.

## The proposed test

A page qualifies when **all** hold:

1. its own stripped text is empty (a container, not a text)
2. no subpages, transcludes nothing (no other form already claimed it)
3. links **≥2** Main-namespace pages, excluding `वर्गः:`, `चित्रम्:`,
   `सञ्चिका:`, `अनुक्रमणिका:`, `पृष्ठम्:` prefixes
4. **every** link resolves to an existing page — one redlink disqualifies it,
   because a partial work is worse than none
5. **every** target has extracted text
6. no target is already part of another rolled-up work (no double-claiming)

Parts fold in link order, exactly as the other four forms do: one real file for
the work, and its parts not written separately.

## The open question — decide before building

Criterion 3 admits genre anthologies. Measured:

```
  0% stem-share   17 links   नाटकानि          "plays" — unrelated dramas
  0% stem-share    5 links   पद्यकाव्यानि      "verse poetry" — a genre index
 97% stem-share   37 links   तन्त्रालोकः       a real work, 37 āhnikas
100% stem-share   18 links   रसरत्नसमुच्चयः    a real work
```

Rolling up `नाटकानि` yields one "text" that is Abhijñānaśākuntala followed by
Uttararāmacarita followed by three more unrelated plays. **A fabricated work is
worse than an unlinked one.**

**A stem-share threshold looks like the fix and is not sufficient.**
`पूर्वमीमांसादर्शनम्` scores 0% while listing `मीमांसासूत्राणि`,
`शबरभाष्यम्`, `श्लोकवार्तिकम्` — the genuine constituent texts of the school,
sharing no prefix with the parent. A stem test rejects a real case and would
still need a whitelist beside it.

Three ways to resolve:

**(a) Whitelist by title, ~26 entries.** Explicit, auditable, no false
positives. Costs a list that goes stale as the wiki changes — but at 26 entries
reviewed once that is cheap, and a stale entry fails safe: the page simply
keeps no link.

**(b) Stem-share ≥ 80% plus a whitelist for exceptions.** Fewer manual entries,
but two mechanisms and a guessed threshold.

**(c) Ask the category.** A genre anthology is usually filed under a category
that is itself a genre. Structural rather than lexical, but needs its own
investigation and may not separate the cases cleanly.

**Recommendation: (a).** The population is 26, the risk worth avoiding is a
fabricated work, and an explicit list is the only option that cannot produce
one silently.

---

# Fix 2 — the 8 sub-5 KB items

Not really a fix: these are items whose "text" is a stub. `मुखपुटम्` is the
wiki's own main page (5 KB of navigation); `गणितसारसङ्ग्रहः` is 102 bytes.

Two options, and **the second is probably right**:

- extract them anyway, so every visible item has a link, and accept that a few
  links open onto near-nothing
- treat them the way image-only scans are now treated — below some floor, an
  item is not a text and should not be counted or shown as one

If the floor route is taken, pick the threshold from the data rather than
guessing: the gap between these 8 and the next-smallest real work is where it
belongs. Note that `मुखपुटम्` is arguably not a corpus item at all and might be
excluded by name regardless.

---

## What Sāgarasaṅgama needs

**Nothing, for either fix.** It derives its `lt` flag from each Atlas's
published `has_text`, and its visibility from `po`. Anything the Atlas starts
or stops pointing at follows automatically — as it did for all four handled
forms and for the scan-only change.

---

# Attempt 1 (2026-08-29): built, then reverted — rejected as unreviewable

**The code worked. It was reverted anyway, and the reason is the point of this
section.** The user could not tell, from anything the implementation reported,
*what* it was doing to *which* texts or *why* — and correctly refused to trust
a result he could not audit. He rejected it without ever inspecting the cases,
because being asked to spot-check output he had no model of is not review.

Do not treat this as a communication footnote. The work was unreviewable
because of how it was built and sequenced, and the next attempt has to be built
differently, not merely narrated better.

## What went wrong, in order

**1. A whole judgment layer got invented and never surfaced.** The spec asked
for a whitelist of ~26 titles. What shipped was a 23-row table where each row
also enumerated its *parts*, plus a second table of refusals, plus a structural
test — three interacting mechanisms where the spec described one. None of that
was flagged as a departure at the time it was made.

**2. Every judgment call was made silently, and one was wrong.**
`पूर्वमीमांसादर्शनम्` was whitelisted as a work. It is a school reading list:
Jaimini's sūtras, Śabara's bhāṣya, Kumārila's two vārttikas, Śālikanātha,
Khaṇḍadeva — separate works by different authors ~1000 years apart. Rolling it
up merged 11 independent texts (including तन्त्रवार्तिकम् at 1.9 MB and
भाट्टदीपिका at 1.7 MB) into one fabricated 5.3 MB "work" and removed all 11
from the Atlas as separate items. This is *exactly* the failure the spec named
`नाटकानि` to prevent, committed anyway, by the same hand that refused
`नाटकानि`. The user caught it from a summary table, in seconds. It was in the
build for hours.

**3. The destructive half of the change was under-reported for most of the
session.** The rollup is two-sided: N parts stop being separately openable so
one work becomes openable. Reporting led with "22 works gain text" and buried
"197 texts lose their own entry" — the larger, riskier, irreversible-looking
half. The user had to ask twice ("I know you affected more than 22") to get it.

**4. Summaries obscured the only thing that makes review possible: titles.**
186 affected texts were compressed into patterns — `तन्त्रालोकः …माह्निकम् ×37`,
`अष्टाङ्गसंग्रहः … अध्याय N-M`. Unusable for looking anything up. When asked for
examples, the reply gave counts and byte totals instead of a list.

**5. Internal vocabulary leaked into every explanation.** "Shape guard",
"link-text share below 30%", "criterion 5", "NOT_WORKS", "mechanism 3" —
invented terms, never defined, used as if shared. The final grouped summary was
organized by *implementation mechanism* rather than by *what happened to which
texts*, so its headings ("NO CHANGE, refused — mechanism: NOT_WORKS") were
unreadable to the person who had to approve it.

**6. Sequencing put verification last.** The order was: design, implement,
measure, then present a finished thing for approval. Every judgment was already
embedded in code by the time any of it was visible. There was no point at which
the user could cheaply say "no, not that one" — which is why the one bad row
survived to the end.

## What the next attempt must do differently

- **Get the list reviewed before writing the code.** The whitelist IS the
  design decision; it is 20-30 titles and takes minutes to read. Produce it as
  a plain table — title, its proposed parts, and what happens to each part —
  and get a yes/no per row *first*. Nothing else can be built on an unreviewed
  list.

- **Always show both sides of a roll-up together.** One row per work: "these N
  named texts stop appearing separately; this formerly-empty item now opens
  their combined text." Never a "gains" section and a "loses" section.

- **Name every affected text, always.** No `×37`, no `N-M`, no counts standing
  in for titles. If a list is long, it is long.

- **Flag departures from the spec when they happen**, not in a final summary.
  A second table, a new test, a criterion reinterpreted — each is a decision
  the spec did not authorize and needs an explicit "I am departing here, and
  why."

- **Use the wiki's own vocabulary.** Pages, links, chapters, works, reading
  lists. If an internal test needs a name, define it in one sentence at first
  use, in terms of what it does to pages.

- **Separate the two questions, and answer them in this order:** (1) is this
  page a work whose parts should be combined? — a judgment, reviewed per title;
  (2) what mechanically happens when it is? — the existing fold, unchanged and
  uninteresting. Attempt 1 fused them, so approving a title silently approved
  the removal of its parts.

## What was technically established, and is worth keeping

These findings cost real measurement and should not be re-derived:

- **The spec's criteria 1-6 are not sufficient, and admit a catastrophe.**
  `पैप्पलादसंहिता/काण्डम् १८` is 232 KB of Atharvaveda verse in `<poem>` tags.
  Its stripped text is empty, it has no subpages, transcludes nothing, and its
  inline `तु.` ("compare") cross-references all resolve to pages that all have
  text — it passes every criterion in this spec. Rolling it up would replace
  the Paippalāda Saṃhitā with copies of the Śaunakīya recension. 15 pages in
  this corpus are shaped that way (Paippalāda kāṇḍas, Taittirīya prapāṭhakas,
  Bṛhaddevatā, Ṛgveda khilasūktāni, Śatapatha). **Any future attempt needs a
  test that separates "a list of links" from "prose that cites pages" —
  measuring the share of the page's body that is link text works: prose scores
  0.1-0.5%, real link-lists 36-91%.**

- **"Every target has extracted text" must mean "will have a file", not "has
  its own text".** `तन्त्रवार्तिकम्` is 233 bytes of wikitext around a
  `<pages index=... from=1 to=1269/>` tag and 3.8 MB once its scan leaves are
  folded in. Testing own-text alone rejects works whose parts are themselves
  assembled — and, one level deeper, silently writes a 0-byte file for the work
  while suppressing the parts' own files, losing the text from both places
  (observed: `धनदशतकत्रयम्` 0 B, `कूर्मपुराणम्` 66 B).

- **Fix 2's premise is stale.** `गणितसारसङ्ग्रहः`, cited here as 102 bytes, is
  now 746 KB — an earlier rollup already rescued it. The prescribed method
  ("pick the threshold from the gap") cannot be applied: over 3359 top-level
  texts with a file, the largest jump below 20 KB is 158 bytes, and 1323 texts
  fall under 5 KB. A 5 KB floor would hide 1323 items, not 8. Only `मुखपुटम्`
  (the wiki's main page) still matches the description. Re-measure before
  reviving this.

- **A rolled-up link-list work shows 0 bytes in `tree.json` while linking to
  real text.** Its parts keep their own real page nodes (they are separately
  filed top-level pages, not `/` subpages), so the byte rollup that works for
  subpages does not reach them. Attempt 1 shipped extraction-only and left this
  unresolved; the totals were verified unchanged (`count`, `text_count` and all
  three byte figures identical before and after), so nothing was double-counted
  — but the display inconsistency is real and unaddressed.

- **`शबरभाष्यम्` is a separate rollup candidate**, unrelated to this spec: 71
  bytes of its own, with its text in `शबरभाष्यम् १-४ अध्यायाः` (957 KB),
  `५ अध्यायः`, and `६-७ अध्यायाः`.

---

# Rescoped (2026-08-31): this is a bug, and it is ~10x larger than "the 26"

**Everything above scopes the problem as a coverage gap — 74 visible items
lacking a `txt` link, of which 26 are fixable. That framing is wrong, and it is
why two attempts have argued about mechanism while the actual defect went
uncounted.**

## The defect

Where a work's parts are separately-titled top-level pages, the Atlas has two
problems, not one:

1. the parent shows as an item with no text (the 26) — **visible, cosmetic**
2. **every part shows as if it were a whole work** — invisible, and the larger
   half

`तन्त्रालोकः/…माह्निकम्` is chapter 3 of one work, listed beside
`रघुवंशम्` as though it were a peer. That is a misrepresentation of the corpus
whether or not the parent ever gains a link. Symptom 2 is the bug; symptom 1 is
a side effect of the same cause.

## Measured, 2026-08-31, against the committed `docs/data/tree.json`

Counting top-level pages whose title has another top-level page's title as a
prefix — i.e. pages *named as parts*:

```
 3674  distinct top-level pages in the Atlas
  770  are named as parts of another page      <- the actual population
  245  parents they belong to
   19  of those 245 parents lack text themselves
```

**770 of 3674 (21%), not 74 of 3729 (2%).**

The two symptoms barely overlap: **226 of the 245 parents already have their
own fulltext.** `समराङ्गणसूत्रधार` (83 parts) and `अष्टाङ्गसंग्रहः` (27 parts)
are "fine" by the coverage metric and are together 110 chapter-pages presenting
as works. No amount of link-list rollup reaches them — they were never in
the 74.

**Caveats on the 770.** Prefix-matching is crude: it will catch cases where one
work's title genuinely begins with another's, and it does not distinguish
"parent name + chapter word" from any shared prefix. `चर्यापादः/अध्यायः १`
appearing as a parent of 10 shows the `/` subpage tree and this test overlap
and need separating. The true figure is below 770; the order of magnitude
holds. **Re-measure properly before acting.**

## What this does to the plan above

- **Attempt 1's "197 texts lose their own entry" is not the scale of anything.**
  It is what one particular whitelist happened to touch — roughly a quarter of
  the named-parts population, selected by a list that contained at least one
  wrong row.

- **The ~99% ceiling is a ceiling on the wrong metric.** Fulltext coverage can
  read 99% while 770 items misrepresent what they are.

- **The whitelist may be answering a question the source already answers.** The
  spec rejected a stem-share test on the strength of `पूर्वमीमांसादर्शनम्`
  scoring 0% — treating that as a false negative. It is a **true** negative: a
  school reading list of works by different authors ~1000 years apart, and
  rolling it up is precisely the catastrophe that got Attempt 1 reverted. The
  test was right and human judgment overrode it. Whether parts carry the
  parent's name is a *structural* claim of ownership, the same claim a
  `Work/Chapter` subpage title makes — worth re-testing on its own merits
  before another hand-reviewed list is built.

- **The ~48 "nothing to point at" items were written off too fast.** That is an
  argument about fulltext links only. Whether a linkless stub or a broken-link
  list should be *visible as a text* is a separate question, and `po` already
  exists for exactly that. `नाटकानि` likely wants `po`, not rollup: its links
  already resolve to texts that stand on their own.

## How to approach it

**Do not fix this by extending the rollup.** Rollup answers "how does a parent
get text"; the bug is "what is a part allowed to look like". Establish, in
order:

1. **Count it properly.** Separate parent-name-plus-chapter-word from
   incidental prefix sharing, and from the `/` subpage tree that is already
   handled. Until that number is real, nothing should be built.
2. **Decide what a part should look like in the Atlas** — nested under its
   parent, suppressed, or flagged — as a *display* question, independent of
   fulltext.
3. **Only then** revisit whether the 26 need a whitelist at all.

Attempt 1's process lessons above still stand in full, and apply harder at this
size: review the list before the code, name every affected text, show both
sides of every change together.
