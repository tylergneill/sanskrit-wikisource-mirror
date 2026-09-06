const CHANGELOG_URL = "./data/changelog.json";
const SOURCE_ERAS_URL = "./data/source_eras.json";

const state = {
  scheme: "iast", // devanagari | iast | hk | itrans | slp1 | iso
  log: null,
  granularity: 12, // months per group: 1 = monthly, 3 = quarterly, 12 = yearly
  eras: null, // source_eras.json once loaded; lets trend lines be colored by
              // which source each span came from (see sourceGradientStops)
  includeOrphans: true, // when true, trend charts use each entry's "all" total
                          // (central + असम्बद्धवर्गीकृतम्, the orphan bucket) instead
                          // of the central-only old/new/sizes. Defaults ON: the
                          // orphan bucket is real corpus content that merely
                          // isn't reachable by category descent, and its share
                          // swings enormously over the history (0% pre-2015,
                          // ~50% from 2020-07 to 2025-01 while पुराणानि was
                          // detached from the root -- see
                          // notes/interpretive-decisions.md), so the
                          // central-only series makes upstream category
                          // maintenance look like corpus loss.
};

function translitText(s) {
  if (!s) return s;
  if (state.scheme === "devanagari") return s;
  try {
    return window.Sanscript.t(s, "devanagari", state.scheme);
  } catch {
    return s;
  }
}

// The artificial catch-all category process.py files unreachable pages under
// (compare.py's ORPHAN_BUCKET_TITLE). Named here rather than described, so
// reader-facing labels use the same term as the rest of the page.
const ORPHAN_BUCKET_TITLE = "असम्बद्धवर्गीकृतम्";

const style = document.createElement("style");
style.textContent = `
  .changelog-summary { list-style: none; justify-content: flex-start; align-items: center; gap: 6px; }
  .changelog-summary::-webkit-details-marker { display: none; }
  .changelog-summary::after {
    content: "";
    display: inline-block;
    width: 0.5em;
    height: 0.5em;
    border-right: 2px solid var(--muted);
    border-bottom: 2px solid var(--muted);
    transform: rotate(-45deg);
    transition: transform 0.15s ease;
  }
  details[open] > .changelog-summary::after { transform: rotate(45deg); }

  .changelog-bucket { margin-top: 8px; }
  .changelog-bucket-summary {
    cursor: pointer;
    font-weight: 600;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .changelog-bucket-summary::-webkit-details-marker { display: none; }
  .changelog-bucket-summary::after {
    content: "";
    display: inline-block;
    width: 0.4em;
    height: 0.4em;
    border-right: 2px solid var(--muted);
    border-bottom: 2px solid var(--muted);
    transform: rotate(-45deg);
    transition: transform 0.15s ease;
  }
  details[open] > .changelog-bucket-summary::after { transform: rotate(45deg); }
`;
document.head.appendChild(style);

function fmtChange(pct, oldFormatted) {
  if (pct === null || pct === undefined) return "";
  const arrow = pct >= 0 ? "↑" : "↓";
  return ` (${arrow} ${Math.abs(pct).toFixed(1)}% from ${oldFormatted})`;
}

function fmtDate(s) {
  if (!s) return "n/a";
  return s.slice(0, 10);
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "n/a";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

// Compact size formatter for per-item deltas -- picks B/KB/MB by magnitude
// rather than always MB, since a single page's transliterated_bytes is
// usually in the KB range and "0.0 MB" reads as meaningless precision loss.
function fmtBytesCompact(n) {
  if (n === null || n === undefined) return "n/a";
  if (n === 0) return "0";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// "(0→400 KB)" for a new item, "(400 KB → 480 KB, ↑ 20.0%)" for a size change.
// Omits the trailing percent when the old size is 0 (added items), since a
// percent-of-zero is undefined/misleading.
//
// Otherwise the parenthetical is dropped entirely unless the change is real on
// *both* measures, because each one alone lies in a different direction:
//
//   - Rendered-identical, e.g. "39 KB → 39 KB, ↓ 0%". The common case on the
//     Updated list (timestamp moved, content barely did); a delta the reader
//     can't see is noise next to the date range already shown.
//   - Rendered-different but numerically trivial, e.g. "891 KB → 892 KB" for a
//     *3-byte* change, or "7 KB → 6 KB, ↓ 0.9%". fmtBytesCompact's zero-decimal
//     KB step is coarser than the percent beside it, so a pair straddling a
//     rounding boundary looks like a whole-kilobyte move. Adding decimals does
//     not fix this -- a finer grid just has more boundaries to straddle (one
//     decimal quadruples these, 715 rows -> 2698) -- so the real magnitude has
//     to be checked directly.
const MIN_VISIBLE_PCT = 0.1;

function fmtSizeDelta(oldBytes, newBytes) {
  oldBytes = oldBytes || 0;
  newBytes = newBytes || 0;
  const oldStr = fmtBytesCompact(oldBytes);
  const newStr = fmtBytesCompact(newBytes);
  if (oldBytes === 0) return ` (${oldStr}→${newStr})`;
  if (newBytes === 0) return ` (${oldStr}→${newStr})`;
  if (oldStr === newStr) return "";
  const pct = ((newBytes - oldBytes) / oldBytes) * 100;
  if (Math.abs(pct) < MIN_VISIBLE_PCT) return "";
  const arrow = pct >= 0 ? "↑" : "↓";
  return ` (${oldStr} → ${newStr}, ${arrow} ${Math.abs(pct).toFixed(1)}%)`;
}

// Combine N consecutive monthly changelog entries (oldest-first, chained --
// each entry's old_date equals the previous entry's date) into one synthetic
// entry spanning the whole group. Sizes/counts reduce trivially to the
// group's first "old" and last "new". Item-level lists need real net-effect
// tracking across the group, not concatenation: e.g. an item added in month 2
// and removed in month 5 nets to neither an add nor a remove over the full
// span, and an item added then later edited should still show as "added"
// (with its final size/date), not also as "changed".
//
// The same netting applies to placement changes, which is the whole point of
// tracking them separately: a page categorized in month 2 and orphaned again
// in month 5 nets to no placement change at all, and one categorized then
// edited stays "categorized". Replaying each month's events in order against
// a per-item running state is the only way to get this right from the monthly
// records alone (they don't carry a full item roster per month).
function reduceGroup(entries) {
  if (entries.length === 1) return entries[0];

  // status: 'added'|'removed'|'categorized'|'orphaned'|'recategorized'|'changed'
  const state = new Map();

  for (const entry of entries) {
    for (const item of entry.items_added || []) {
      state.set(item.id, { status: "added", bytes: item.new_bytes, date: item.date });
    }
    for (const item of entry.items_removed || []) {
      if (state.has(item.id) && state.get(item.id).status === "added") {
        // Added then removed within the same group: nets to no-op.
        state.delete(item.id);
      } else {
        state.set(item.id, { status: "removed", bytes: item.old_bytes });
      }
    }
    // Placement moves. An item still net-new over the group stays "added" --
    // being filed somewhere is part of arriving, not a separate event. Two
    // opposite crossings cancel, restoring whatever the item was before.
    for (const item of entry.items_categorized || []) {
      const prev = state.get(item.id);
      if (prev && prev.status === "added") {
        state.set(item.id, { status: "added", bytes: item.new_bytes, date: item.date });
      } else if (prev && prev.status === "orphaned") {
        state.delete(item.id);
      } else {
        state.set(item.id, {
          status: "categorized",
          bytes: item.new_bytes,
          date: item.date,
          to: item.to,
          from: prev && prev.status === "categorized" ? prev.from : item.from,
        });
      }
    }
    for (const item of entry.items_orphaned || []) {
      const prev = state.get(item.id);
      if (prev && prev.status === "added") {
        state.set(item.id, { status: "added", bytes: item.old_bytes, date: prev.date });
      } else if (prev && prev.status === "categorized") {
        state.delete(item.id);
      } else {
        state.set(item.id, { status: "orphaned", bytes: item.old_bytes, from: item.from });
      }
    }
    for (const item of entry.items_recategorized || []) {
      const prev = state.get(item.id);
      // A recategorization on top of an add or a crossing tells us nothing
      // new -- the stronger event already describes the item's fate.
      if (prev && (prev.status === "added" || prev.status === "categorized" || prev.status === "orphaned")) {
        continue;
      }
      state.set(item.id, {
        status: "recategorized",
        bytes: item.new_bytes,
        // Keep the group's earliest "from" so the span reads end-to-end.
        from: prev && prev.status === "recategorized" ? prev.from : item.from,
        to: item.to,
      });
    }
    for (const item of entry.items_with_changed_timestamp || []) {
      const prev = state.get(item.id);
      if (prev && prev.status !== "changed") {
        // Already classified as something stronger over this group (added, or
        // a placement move) -- keep that, but roll bytes/date forward.
        if (prev.status === "added") {
          state.set(item.id, { status: "added", bytes: item.new_bytes, date: item.new });
        } else {
          state.set(item.id, { ...prev, bytes: item.new_bytes });
        }
      } else {
        state.set(item.id, {
          status: "changed",
          old: prev && prev.status === "changed" ? prev.old : item.old,
          new: item.new,
          old_bytes: prev && prev.status === "changed" ? prev.old_bytes : item.old_bytes,
          new_bytes: item.new_bytes,
        });
      }
    }
  }

  const items_added = [];
  const items_removed = [];
  const items_categorized = [];
  const items_orphaned = [];
  const items_recategorized = [];
  const items_with_changed_timestamp = [];
  for (const [id, v] of state) {
    if (v.status === "added") items_added.push({ id, date: v.date, new_bytes: v.bytes });
    else if (v.status === "removed") items_removed.push({ id, old_bytes: v.bytes });
    else if (v.status === "categorized") {
      items_categorized.push({ id, date: v.date, new_bytes: v.bytes, from: v.from, to: v.to });
    } else if (v.status === "orphaned") {
      items_orphaned.push({ id, old_bytes: v.bytes, from: v.from });
    } else if (v.status === "recategorized") {
      items_recategorized.push({ id, new_bytes: v.bytes, from: v.from, to: v.to });
    } else if (v.status === "changed") {
      items_with_changed_timestamp.push({ id, old: v.old, new: v.new, old_bytes: v.old_bytes, new_bytes: v.new_bytes });
    }
  }
  const byId = (a, b) => a.id.localeCompare(b.id);
  items_added.sort(byId);
  items_removed.sort(byId);
  items_categorized.sort(byId);
  items_orphaned.sort(byId);
  items_recategorized.sort(byId);
  items_with_changed_timestamp.sort(byId);

  const first = entries[0];
  const last = entries[entries.length - 1];
  const oldCount = first.old?.count ?? 0;
  const newCount = last.new?.count ?? 0;
  const deltaCount = newCount - oldCount;

  // text_count is missing entirely on older changelog entries (added after
  // the fact) -- track presence with null rather than defaulting to 0, so
  // a group spanning the gap reports "n/a" instead of a misleading delta.
  const oldTextCount = first.old?.text_count;
  const newTextCount = last.new?.text_count;
  const hasTextCount = oldTextCount != null && newTextCount != null;
  const deltaTextCount = hasTextCount ? newTextCount - oldTextCount : null;

  const sizes = {};
  for (const key of ["raw_bytes", "content_bytes", "transliterated_bytes"]) {
    const oldV = first.sizes?.[key]?.old ?? 0;
    const newV = last.sizes?.[key]?.new ?? 0;
    const deltaV = newV - oldV;
    sizes[key] = { old: oldV, new: newV, delta: deltaV, delta_pct: oldV === 0 ? null : (100 * deltaV) / oldV };
  }

  // entry.all (true total, including असम्बद्धवर्गीकृतम्) is only present on
  // changelog entries generated after that field was introduced -- older
  // entries fall back to their own central-only old/new/sizes, same as
  // compare.py's build_report does for individual snapshots that predate it.
  const firstAll = first.all || { old: first.old, sizes: first.sizes };
  const lastAll = last.all || { new: last.new, sizes: last.sizes };
  const oldCountAll = firstAll.old?.count ?? 0;
  const newCountAll = lastAll.new?.count ?? 0;
  const oldTextCountAll = firstAll.old?.text_count;
  const newTextCountAll = lastAll.new?.text_count;
  const hasTextCountAll = oldTextCountAll != null && newTextCountAll != null;
  const deltaTextCountAll = hasTextCountAll ? newTextCountAll - oldTextCountAll : null;
  const sizesAll = {};
  for (const key of ["raw_bytes", "content_bytes", "transliterated_bytes"]) {
    const oldV = firstAll.sizes?.[key]?.old ?? 0;
    const newV = lastAll.sizes?.[key]?.new ?? 0;
    const deltaV = newV - oldV;
    sizesAll[key] = { old: oldV, new: newV, delta: deltaV, delta_pct: oldV === 0 ? null : (100 * deltaV) / oldV };
  }

  return {
    id: last.id,
    date: last.date,
    old_date: first.old_date,
    old: first.old,
    new: last.new,
    sizes,
    delta: {
      count: deltaCount,
      count_pct: oldCount === 0 ? null : (100 * deltaCount) / oldCount,
      text_count: deltaTextCount,
      text_count_pct: !hasTextCount || oldTextCount === 0 ? null : (100 * deltaTextCount) / oldTextCount,
    },
    all: {
      old: firstAll.old,
      new: lastAll.new,
      sizes: sizesAll,
      delta: {
        count: newCountAll - oldCountAll,
        count_pct: oldCountAll === 0 ? null : (100 * (newCountAll - oldCountAll)) / oldCountAll,
        text_count: deltaTextCountAll,
        text_count_pct: !hasTextCountAll || oldTextCountAll === 0 ? null : (100 * deltaTextCountAll) / oldTextCountAll,
      },
    },
    items_added,
    items_removed,
    items_categorized,
    items_orphaned,
    items_recategorized,
    items_with_changed_timestamp,
    items_added_count: items_added.length,
    items_removed_count: items_removed.length,
    items_categorized_count: items_categorized.length,
    items_orphaned_count: items_orphaned.length,
    items_recategorized_count: items_recategorized.length,
    items_changed_count: items_with_changed_timestamp.length,
    items_added_pct: oldCount === 0 ? null : (100 * items_added.length) / oldCount,
    items_removed_pct: oldCount === 0 ? null : (100 * items_removed.length) / oldCount,
  };
}

// Group the oldest-first monthly log into chunks of `size` months, most
// recent chunk first (i.e. grouping counts back from "now"), so a leftover
// partial chunk falls at the oldest end where history runs out rather than
// silently merging into the most recent (and most relevant) group.
function groupEntries(log, size) {
  if (size <= 1) return [...log];
  const groups = [];
  for (let end = log.length; end > 0; end -= size) {
    const start = Math.max(0, end - size);
    groups.push(reduceGroup(log.slice(start, end)));
  }
  return groups.reverse(); // oldest-first, matching the ungrouped log's order
}

function renderEntry(entry) {
  const div = document.createElement("div");
  div.className = "block";

  const oldDate = fmtDate(entry.old_date);
  const newDate = fmtDate(entry.date);

  const header = document.createElement("div");
  header.style.margin = "0 0 4px";
  header.innerHTML = `<strong>${newDate}</strong> (since ${oldDate})`;
  div.appendChild(header);

  const translitSize = entry.sizes?.transliterated_bytes || {};
  const translitLine = document.createElement("p");
  translitLine.style.margin = "0 0 4px";
  translitLine.innerHTML = `<strong>${fmtBytes(translitSize.new)}</strong> ${fmtChange(translitSize.delta_pct, fmtBytes(translitSize.old))}`;
  div.appendChild(translitLine);

  const stats = document.createElement("p");
  stats.style.margin = "0 0 4px";
  const textCount = entry.new?.text_count;
  if (textCount != null) {
    stats.innerHTML = `<strong>${textCount.toLocaleString()} texts</strong>${fmtChange(entry.delta?.text_count_pct, entry.old?.text_count?.toLocaleString() ?? "n/a")}`;
  } else {
    stats.innerHTML = `<strong>n/a texts</strong> (not tracked for this period)`;
  }
  div.appendChild(stats);

  const pageStats = document.createElement("p");
  pageStats.style.margin = "0 0 4px";
  pageStats.style.color = "var(--muted)";
  pageStats.style.fontSize = "0.9em";
  pageStats.innerHTML = `<strong>${entry.new?.count?.toLocaleString() ?? "n/a"} pages</strong>${fmtChange(entry.delta?.count_pct, entry.old?.count?.toLocaleString() ?? "n/a")}`;
  div.appendChild(pageStats);

  const added = entry.items_added_count ?? 0;
  const removed = entry.items_removed_count ?? 0;
  const categorized = entry.items_categorized_count ?? 0;
  const orphanedCount = entry.items_orphaned_count ?? 0;
  const recategorized = entry.items_recategorized_count ?? 0;
  const changed = entry.items_changed_count ?? 0;

  if (added || removed || categorized || orphanedCount || recategorized || changed) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.style.cursor = "pointer";
    summary.style.color = "var(--muted)";
    summary.style.fontSize = "0.9em";
    summary.className = "changelog-summary";
    summary.style.display = "flex";
    // Placement changes are named separately from real additions/removals:
    // a page merely crossing into or out of the orphan bucket used to read as
    // an add or a remove, which made curation drives look like corpus events
    // (2026-08 -> 09: 488 categorized pages showed as 504 "added").
    const parts = [`${added} pages added`, `${removed} removed`];
    if (categorized) parts.push(`${categorized} categorized`);
    if (orphanedCount) parts.push(`${orphanedCount} uncategorized`);
    if (recategorized) parts.push(`${recategorized} recategorized`);
    parts.push(`${changed} updated`);
    summary.innerHTML = `<span>${parts.join(", ")} — <span style="color:var(--accent)">show detail</span></span>`;
    details.appendChild(summary);

    // Each bucket is its own nested dropdown rather than a heading over an
    // always-open list: a single month can carry hundreds of entries in one
    // bucket (2026-09 alone has 488 categorized), which buried the smaller
    // buckets below it under a wall of scrolling.
    const buildList = (title, items, render) => {
      if (!items || items.length === 0) return;
      const sub = document.createElement("details");
      sub.className = "changelog-bucket";
      const subSummary = document.createElement("summary");
      subSummary.className = "changelog-bucket-summary";
      subSummary.textContent = `${title} (${items.length})`;
      sub.appendChild(subSummary);
      const ul = document.createElement("ul");
      for (const item of items) {
        const li = document.createElement("li");
        li.textContent = render(item);
        ul.appendChild(li);
      }
      sub.appendChild(ul);
      details.appendChild(sub);
    };

    const stripPrefix = (id) => translitText(id.replace(/^(page|index-item):/, ""));
    buildList("Added", entry.items_added, (item) => {
      if (typeof item === "string") return stripPrefix(item);
      return `${stripPrefix(item.id)}: ${fmtDate(item.date)}${fmtSizeDelta(0, item.new_bytes)}`;
    });
    buildList("Removed", entry.items_removed, (item) => {
      if (typeof item === "string") return stripPrefix(item);
      return `${stripPrefix(item.id)}${fmtSizeDelta(item.old_bytes, 0)}`;
    });
    const stripCat = (id) => translitText(String(id).replace(/^cat:/, ""));
    const fmtCats = (ids) => (ids && ids.length ? ids.map(stripCat).join(", ") : "—");

    // Placement rows show the item's size plainly, never as a 0→n delta: the
    // page existed before and after, so nothing grew from nothing. That framing
    // is the same error the categorized/orphaned split exists to correct.
    // The artificial catch-all category is named by its own title, matching how
    // the rest of the page refers to it (the "include असम्बद्धवर्गीकृतम्" control
    // just above these entries, and the Structure section that glosses it).
    // translitText keeps it in whatever scheme the reader has selected.
    const orphanCat = translitText(ORPHAN_BUCKET_TITLE);
    buildList(`Categorized (moved out of ${orphanCat})`, entry.items_categorized, (item) =>
      `${stripPrefix(item.id)} → ${fmtCats(item.to)} (${fmtBytesCompact(item.new_bytes ?? 0)})`);
    buildList(`Moved into ${orphanCat}`, entry.items_orphaned, (item) =>
      `${stripPrefix(item.id)} (was ${fmtCats(item.from)}) (${fmtBytesCompact(item.old_bytes ?? 0)})`);
    buildList("Recategorized", entry.items_recategorized, (item) =>
      `${stripPrefix(item.id)}: ${fmtCats(item.from)} → ${fmtCats(item.to)} (${fmtBytesCompact(item.new_bytes ?? 0)})`);
    buildList("Updated", entry.items_with_changed_timestamp, (c) =>
      `${stripPrefix(c.id)}: ${fmtDate(c.old)} → ${fmtDate(c.new)}${fmtSizeDelta(c.old_bytes, c.new_bytes)}`);

    div.appendChild(details);
  } else {
    const counts = document.createElement("p");
    counts.style.color = "var(--muted)";
    counts.style.fontSize = "0.9em";
    counts.textContent = "no pages added, removed, recategorized, or updated";
    div.appendChild(counts);
  }

  return div;
}

function renderChangelog() {
  const container = document.getElementById("changelog");
  if (!container || !state.log) return;
  container.textContent = "";
  const grouped = groupEntries(state.log, state.granularity);
  // Newest first
  for (const entry of [...grouped].reverse()) {
    container.appendChild(renderEntry(entry));
  }
}

// === Trend charts (size, count over time) ===
// Two separate single-series line charts sharing a time x-axis, rather than
// one dual-axis chart -- bytes and item-count have no principled shared
// scale, so overlaying them on one plot with two independently-scaled axes
// would make the visual comparison an artifact of axis choice, not signal.

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function niceTicks(min, max, count) {
  if (min === max) return [min];
  const span = max - min;
  const rawStep = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

// Ticks at an exact fixed step (e.g. round 100 MB increments), rather than
// niceTicks' auto-picked 1/2/5x10^n step -- lets a chart's gridlines land on
// fixed, predictable, cross-chart-comparable values instead of whatever
// number happens to divide the current data's range evenly.
function fixedStepTicks(min, max, step) {
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks.length > 0 ? ticks : [min];
}

function fmtAxisBytes(n) {
  const mb = n / 1024 / 1024;
  if (mb >= 1000) return (mb / 1024).toFixed(1) + " GB";
  return Math.round(mb) + " MB";
}

function fmtAxisCount(n) {
  return n.toLocaleString();
}

// Hard-stop gradient stops mapping the chart's x-range onto the source-type
// segments the timeline bar above already computes, so a trend line is drawn
// in the same colors: readers can see at a glance which stretch of the curve
// came from live dumps vs. reconstruction. Returns null when eras haven't
// loaded (the chart then falls back to its plain single-color stroke).
//
// Doubled stops (each boundary emitted twice, at the same offset) give a crisp
// switch rather than a blend -- these are categorical sources, not a
// continuum, so an interpolated midpoint would imply a nonexistent hybrid.
function sourceGradientStops(xMinMs, xMaxMs) {
  if (!state.eras) return null;
  let segments;
  try {
    segments = buildTimelineSegments(state.eras);
  } catch {
    return null;
  }
  if (!segments || !segments.length) return null;
  const span = xMaxMs - xMinMs;
  if (!(span > 0)) return null;

  const pct = (ms) => Math.max(0, Math.min(100, ((ms - xMinMs) / span) * 100));
  const stops = [];
  for (const seg of segments) {
    const info = TIMELINE_KIND_INFO[seg.kind];
    if (!info) continue;
    // Segment end dates are inclusive month starts; extend to the following
    // month so the last month's color reaches the next boundary.
    const startMs = new Date(`${seg.start}T00:00:00Z`).getTime();
    const endMs = new Date(`${monthAfter(seg.end)}T00:00:00Z`).getTime();
    if (endMs < xMinMs || startMs > xMaxMs) continue;
    stops.push({ offset: pct(startMs), color: info.color });
    stops.push({ offset: pct(endMs), color: info.color });
  }
  return stops.length ? stops : null;
}

function monthAfter(dateStr) {
  let year = Number(dateStr.slice(0, 4));
  let month = Number(dateStr.slice(5, 7)) + 1;
  if (month > 12) { month = 1; year += 1; }
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-01`;
}

function renderTrendChart(container, points, { title, getValue, fmtValue, fmtAxis, tickStep }) {
  const width = 720;
  const height = 220;
  const margin = { top: 10, right: 16, bottom: 26, left: 56 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;

  const card = document.createElement("div");
  card.className = "chart-card";
  const h4 = document.createElement("h4");
  h4.textContent = title;
  card.appendChild(h4);

  const wrap = document.createElement("div");
  wrap.className = "chart-wrap";
  card.appendChild(wrap);

  const values = points.map(getValue);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const pad = (maxV - minV) * 0.08 || Math.abs(maxV) * 0.08 || 1;
  const yMin = Math.max(0, minV - pad);
  const yMax = maxV + pad;

  const dates = points.map((p) => new Date(p.date));
  const xMin = dates[0].getTime();
  const xMax = dates[dates.length - 1].getTime();

  const xScale = (t) => margin.left + (xMax === xMin ? innerW / 2 : ((t - xMin) / (xMax - xMin)) * innerW);
  const yScale = (v) => margin.top + innerH - ((v - yMin) / (yMax - yMin || 1)) * innerH;

  const svg = svgEl("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": title,
  });

  // Gridlines + y-axis labels (nice round values, or a fixed step if given).
  const yTicks = tickStep ? fixedStepTicks(yMin, yMax, tickStep) : niceTicks(yMin, yMax, 4);
  for (const t of yTicks) {
    const y = yScale(t);
    svg.appendChild(svgEl("line", {
      class: "chart-grid", x1: margin.left, x2: width - margin.right, y1: y, y2: y,
    }));
    const label = svgEl("text", {
      class: "chart-axis-text", x: margin.left - 8, y: y + 3, "text-anchor": "end",
    });
    label.textContent = fmtAxis(t);
    svg.appendChild(label);
  }

  // X-axis: label first, last, and a few evenly-spaced dates in between.
  // Fewer date ticks on a phone: the axis text is counter-scaled up there (see
  // styles.css) so six "YYYY-MM" labels crowd into each other, and the two leftmost
  // end up nearly touching.
  const narrow =
    typeof window !== "undefined" &&
    window.matchMedia("(max-width: 800px)").matches;
  const xTickCount = Math.min(narrow ? 4 : 6, points.length);
  const xTickIdxs = new Set();
  for (let i = 0; i < xTickCount; i++) {
    xTickIdxs.add(Math.round((i / (xTickCount - 1 || 1)) * (points.length - 1)));
  }
  // Anchor the end labels inward rather than centering them: a centered label at
  // the first/last tick hangs half its width past the plot area, which at phone
  // widths (where the axis text is counter-scaled up, see styles.css) runs visibly
  // outside the chart box.
  const sortedTickIdxs = [...xTickIdxs].sort((a, b) => a - b);
  const firstIdx = sortedTickIdxs[0];
  const lastIdx = sortedTickIdxs[sortedTickIdxs.length - 1];
  for (const idx of sortedTickIdxs) {
    const x = xScale(dates[idx].getTime());
    const anchor =
      idx === firstIdx ? "start" : idx === lastIdx ? "end" : "middle";
    const label = svgEl("text", {
      class: "chart-axis-text", x, y: height - 6, "text-anchor": anchor,
    });
    label.textContent = points[idx].date.slice(0, 7);
    svg.appendChild(label);
  }

  // Area wash + line. When source-era data is available the stroke/fill use a
  // hard-stop gradient keyed to the timeline bar's colors, so the curve is
  // legible as "this stretch came from that source" (see sourceGradientStops).
  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${xScale(dates[i].getTime())},${yScale(getValue(p))}`).join(" ");
  const areaPath = `${linePath} L${xScale(xMax)},${yScale(yMin)} L${xScale(xMin)},${yScale(yMin)} Z`;

  const stops = sourceGradientStops(xMin, xMax);
  let lineStroke = null;
  let areaFill = null;
  if (stops) {
    // Unique ids: several charts share one document, and duplicate gradient
    // ids would make later charts silently reuse the first one's stops.
    const uid = `srcgrad-${Math.random().toString(36).slice(2, 9)}`;
    const defs = svgEl("defs", {});
    // gradientUnits=userSpaceOnUse so offsets track the chart's x pixels, not
    // each path's own bounding box (the area path is wider than the line).
    const mkGrad = (id, opacity) => {
      const g = svgEl("linearGradient", {
        id, gradientUnits: "userSpaceOnUse",
        x1: margin.left, x2: width - margin.right, y1: 0, y2: 0,
      });
      for (const stop of stops) {
        g.appendChild(svgEl("stop", {
          offset: `${stop.offset}%`, "stop-color": stop.color, "stop-opacity": opacity,
        }));
      }
      return g;
    };
    defs.appendChild(mkGrad(`${uid}-line`, 1));
    defs.appendChild(mkGrad(`${uid}-area`, 1));
    svg.appendChild(defs);
    lineStroke = `url(#${uid}-line)`;
    areaFill = `url(#${uid}-area)`;
  }

  // Inline style, not a presentation attribute: .chart-line/.chart-area set
  // stroke/fill in styles.css, and a CSS declaration beats a presentation
  // attribute regardless of specificity -- so `stroke="url(#...)"` silently
  // lost to `stroke: var(--accent)` and the line stayed one flat color.
  const areaAttrs = { class: "chart-area", d: areaPath };
  // Only the fill is overridden -- .chart-area's own opacity:0.1 still supplies
  // the wash, so the tinted area stays as subtle as the original flat one.
  if (areaFill) areaAttrs.style = `fill:${areaFill};`;
  svg.appendChild(svgEl("path", areaAttrs));
  const lineAttrs = { class: "chart-line", d: linePath };
  if (lineStroke) lineAttrs.style = `stroke:${lineStroke};`;
  svg.appendChild(svgEl("path", lineAttrs));

  // End marker (direct-labeled per marks-and-anatomy: label the endpoint).
  const lastX = xScale(xMax);
  const lastY = yScale(getValue(points[points.length - 1]));
  svg.appendChild(svgEl("circle", { class: "chart-dot", cx: lastX, cy: lastY, r: 4 }));
  const endLabel = svgEl("text", {
    class: "chart-axis-text", x: lastX, y: lastY - 10, "text-anchor": "end",
    style: "font-weight:600;",
  });
  endLabel.textContent = fmtValue(getValue(points[points.length - 1]));
  svg.appendChild(endLabel);

  // Crosshair + hover dot (shared X readout via the tooltip built below).
  const crosshair = svgEl("line", {
    class: "chart-crosshair", x1: 0, x2: 0, y1: margin.top, y2: margin.top + innerH,
  });
  svg.appendChild(crosshair);
  const hoverDot = svgEl("circle", { class: "chart-hover-dot", r: 5 });
  svg.appendChild(hoverDot);

  // Hit layer: one big transparent rect, nearest-point snap on pointermove.
  const hit = svgEl("rect", {
    class: "chart-hit", x: margin.left, y: margin.top, width: innerW, height: innerH,
  });
  svg.appendChild(hit);

  wrap.appendChild(svg);

  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  wrap.appendChild(tooltip);

  function showAt(clientX) {
    const rect = svg.getBoundingClientRect();
    const scaleX = width / rect.width;
    const localX = (clientX - rect.left) * scaleX;
    // Nearest point by x-pixel distance.
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < points.length; i++) {
      const d = Math.abs(xScale(dates[i].getTime()) - localX);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    const p = points[best];
    const px = xScale(dates[best].getTime());
    const py = yScale(getValue(p));
    crosshair.setAttribute("x1", px);
    crosshair.setAttribute("x2", px);
    crosshair.style.opacity = "1";
    hoverDot.setAttribute("cx", px);
    hoverDot.setAttribute("cy", py);
    hoverDot.style.opacity = "1";

    tooltip.innerHTML = "";
    const dateRow = document.createElement("div");
    dateRow.className = "chart-tooltip-date";
    dateRow.textContent = p.date.slice(0, 10);
    tooltip.appendChild(dateRow);
    const valueRow = document.createElement("div");
    valueRow.className = "chart-tooltip-value";
    valueRow.textContent = fmtValue(getValue(p));
    tooltip.appendChild(valueRow);

    tooltip.style.opacity = "1";
    const tipRect = tooltip.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    let left = (px / width) * wrapRect.width - tipRect.width / 2;
    left = Math.max(0, Math.min(wrapRect.width - tipRect.width, left));
    tooltip.style.left = `${left}px`;
    const top = (py / height) * wrapRect.height - tipRect.height - 12;
    tooltip.style.top = `${Math.max(0, top)}px`;
  }

  function hide() {
    crosshair.style.opacity = "0";
    hoverDot.style.opacity = "0";
    tooltip.style.opacity = "0";
  }

  hit.addEventListener("pointermove", (ev) => showAt(ev.clientX));
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("pointerdown", (ev) => showAt(ev.clientX));

  container.appendChild(card);
}

function renderChangelogCharts() {
  const container = document.getElementById("changelogCharts");
  if (!container || !state.log) return;
  container.textContent = "";

  // One point per group's "new" state, in chronological order (grouped
  // entries come back oldest-first, matching the ungrouped log), plus the
  // first group's "old" state so the earliest data point isn't dropped from
  // the trend. Same grouping as the deltas list below, so the chart's
  // resolution matches whatever granularity is currently selected.
  const sorted = groupEntries(state.log, state.granularity);
  if (sorted.length === 0) {
    container.textContent = "No data yet.";
    return;
  }
  // includeOrphans switches every point to each entry's "all" total (central
  // + असम्बद्धवर्गीकृतम्, the orphan bucket) instead of the central-only figures --
  // falls back to the central old/new/sizes on entries that predate "all".
  const first0 = state.includeOrphans ? (sorted[0].all || sorted[0]) : sorted[0];
  const points = [
    {
      date: sorted[0].old_date,
      count: first0.old?.count ?? 0,
      textCount: first0.old?.text_count ?? null,
      bytes: first0.sizes?.transliterated_bytes?.old ?? 0,
    },
    ...sorted.map((e) => {
      const src = state.includeOrphans ? (e.all || e) : e;
      return {
        date: e.date,
        count: src.new?.count ?? 0,
        textCount: src.new?.text_count ?? null,
        bytes: src.sizes?.transliterated_bytes?.new ?? 0,
      };
    }),
  ];

  renderTrendChart(container, points, {
    title: "Effective Size (transliterated content bytes)",
    getValue: (p) => p.bytes,
    fmtValue: fmtBytes,
    fmtAxis: fmtAxisBytes,
    tickStep: 100 * 1024 * 1024,
  });

  // text_count is absent on snapshots from before that stat existed --
  // only chart the trailing run of points that actually have it, rather
  // than plotting a misleading 0 for the untracked era.
  const textPoints = points.filter((p) => p.textCount != null);
  if (textPoints.length > 1) {
    renderTrendChart(container, textPoints, {
      title: "Text Count",
      getValue: (p) => p.textCount,
      fmtValue: (n) => `${n.toLocaleString()} texts`,
      fmtAxis: fmtAxisCount,
    });
  }

  renderTrendChart(container, points, {
    title: "Page Count",
    getValue: (p) => p.count,
    fmtValue: (n) => `${n.toLocaleString()} pages`,
    fmtAxis: fmtAxisCount,
  });
}

// "YYYY-MM-01" -> "YYYY-MM-01" for the previous calendar month -- the
// legacy-format live window ends the month before the current-format live
// window's rolling start takes over, never the same month (the two never
// overlap).
function monthBefore(yyyyMmDd) {
  const [y, m] = yyyyMmDd.split("-").map(Number);
  const prevM = m === 1 ? 12 : m - 1;
  const prevY = m === 1 ? y - 1 : y;
  return `${prevY}-${String(prevM).padStart(2, "0")}-01`;
}

function fmtYearMonth(yyyyMmDd) {
  return yyyyMmDd.slice(0, 7);
}

function fmtDateRanges(ranges) {
  return ranges
    .map(([start, end]) => (start === end ? fmtYearMonth(start) : `${fmtYearMonth(start)} to ${fmtYearMonth(end)}`))
    .join(", ");
}

// "YYYY-MM-01" -> integer month index (months since 0000-01), for subtracting/
// comparing dates and computing proportional segment widths along the timeline.
function monthIndex(yyyyMmDd) {
  const [y, m] = yyyyMmDd.split("-").map(Number);
  return y * 12 + (m - 1);
}

// This Atlas's tree-building depends on वर्गसर्वस्वम् (created 2012-01-20),
// so no month before this floor could ever produce a usable snapshot: the
// root category doesn't exist yet, and process_dump raises
// RootCategoryMissing (see pipeline/backfill.py's MATERIALIZED_FLOOR).
// Everything before this floor is folded into the separate
// #sourceTimelinePre block instead of being miscolored as usable coverage
// in the real bar.
const TIMELINE_FLOOR = "2012-02-01"; // first month with a real changelog snapshot (Jan straddles the category's creation)

// Builds the full chronological list of {start, end, kind} segments covering
// TIMELINE_FLOOR through the present, for the source-type timeline bar. kind
// is one of "current-live" / "legacy-live" / "materialized".
//
// Every historical month is materialized -- reconstructed from the full
// revision history -- rather than read from whichever archived dump happens
// to exist for it. Internet Archive snapshots are deliberately unused: a real
// archived dump records the titles pages bore at that date, while the
// reconstruction records the titles they bear today, and since text_count is
// derived from title breadcrumbs the two count the same corpus differently.
// Mixing them stepped the series at every source switch. See
// notes/interpretive-decisions.md §6.
function buildTimelineSegments(eras) {
  const { era1_rolling_start, era2_rolling_start } = eras;
  const segments = [];

  // Materialized covers everything from the floor up to the legacy-live
  // window's start -- one contiguous span, no gaps, since reconstruction
  // never depends on a dump existing for a given month.
  segments.push({ start: TIMELINE_FLOOR, end: monthBefore(era2_rolling_start), kind: "materialized" });

  segments.push({ start: era2_rolling_start, end: monthBefore(era1_rolling_start), kind: "legacy-live" });

  const now = new Date();
  const presentDate = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}-01`;
  segments.push({ start: era1_rolling_start, end: presentDate, kind: "current-live" });

  segments.sort((a, b) => monthIndex(a.start) - monthIndex(b.start));
  return segments;
}

const TIMELINE_KIND_INFO = {
  "current-live": { color: "var(--series-1)", label: "Current-format live", desc: "newer Wikimedia “Content File” export format" },
  "legacy-live": { color: "var(--series-2)", label: "Legacy-format live", desc: "legacy Wikimedia export format" },
  "materialized": { color: "var(--series-4)", label: "Materialized", desc: "synthetic, reconstructed on demand from full Wikimedia revision history" },
};

const TIMELINE_PRE_TOOLTIP_HTML =
  `<span class="ttLabel">Before this changelog</span><br>` +
  `sa.wikisource's actual edit history goes back to 2004-07-23, roughly 7.5 years ` +
  `before वर्गसर्वस्वम् (the root category this Atlas's tree-building depends on) was ` +
  `created on 2012-01-20. The changelog can't reach earlier than 2012-02, not because ` +
  `the underlying revision data runs out, but because there's no category structure ` +
  `to build a tree from before then.`;

function renderSourceTimeline(eras, idPrefix = "sourceTimeline") {
  const preEl = document.getElementById(`${idPrefix}Pre`);
  const barEl = document.getElementById(`${idPrefix}Bar`);
  const axisEl = document.getElementById(`${idPrefix}Axis`);
  const tooltipEl = document.getElementById(`${idPrefix}Tooltip`);
  if (!barEl) return;

  if (preEl) {
    preEl.addEventListener("mouseenter", () => {
      tooltipEl.innerHTML = TIMELINE_PRE_TOOLTIP_HTML;
      tooltipEl.hidden = false;
    });
    preEl.addEventListener("mousemove", (ev) => positionTimelineTooltip(ev));
    preEl.addEventListener("mouseleave", () => { tooltipEl.hidden = true; });
  }

  const segments = buildTimelineSegments(eras);
  const spanStart = monthIndex(segments[0].start);
  const spanEnd = monthIndex(segments[segments.length - 1].end);
  const totalMonths = spanEnd - spanStart + 1;

  barEl.innerHTML = "";
  for (const seg of segments) {
    const info = TIMELINE_KIND_INFO[seg.kind];
    const months = monthIndex(seg.end) - monthIndex(seg.start) + 1;
    const pct = (months / totalMonths) * 100;
    const el = document.createElement("div");
    el.className = "sourceTimelineSeg";
    el.style.width = `${pct}%`;
    el.style.background = info.color;
    el.addEventListener("mouseenter", () => showTimelineTooltip(el, seg, info));
    el.addEventListener("mousemove", (ev) => positionTimelineTooltip(ev));
    el.addEventListener("mouseleave", () => { tooltipEl.hidden = true; });
    barEl.appendChild(el);
  }

  axisEl.innerHTML = "";
  const startYear = Number(segments[0].start.slice(0, 4));
  const endYear = Number(segments[segments.length - 1].end.slice(0, 4));
  const tickYears = new Set([startYear, endYear]);
  // Skip a generated tick that would land within 2 years of either edge label
  // (e.g. startYear 2011 + the next multiple-of-4 being 2012) -- they'd
  // overlap since both compete for the same left-aligned corner.
  for (let y = Math.ceil(startYear / 4) * 4; y < endYear; y += 4) {
    if (y - startYear < 2 || endYear - y < 2) continue;
    tickYears.add(y);
  }
  for (const y of [...tickYears].sort((a, b) => a - b)) {
    const idx = monthIndex(`${y}-01-01`);
    const leftPct = ((Math.max(idx, spanStart) - spanStart) / totalMonths) * 100;
    const span = document.createElement("span");
    span.style.left = `${leftPct}%`;
    span.textContent = y;
    axisEl.appendChild(span);
  }

  function showTimelineTooltip(el, seg, info) {
    const range = seg.start === seg.end ? fmtYearMonth(seg.start) : `${fmtYearMonth(seg.start)} to ${fmtYearMonth(seg.end)}`;
    tooltipEl.innerHTML = `<span class="ttLabel">${info.label}</span><br>${range}`;
    tooltipEl.hidden = false;
  }
  function positionTimelineTooltip(ev) {
    const rootRect = document.getElementById(idPrefix).getBoundingClientRect();
    tooltipEl.style.left = `${ev.clientX - rootRect.left + 12}px`;
    tooltipEl.style.top = `${ev.clientY - rootRect.top + 16}px`;
  }
}

// Rendered twice with the same data: once under "Snapshots" (where the source
// types are explained) and again under "Data Quantity" (so the trend charts'
// bumps/dips can be visually cross-referenced against which source type
// produced that stretch of the changelog, without scrolling back up).
const SOURCE_TIMELINE_ID_PREFIXES = ["sourceTimeline", "sourceTimelineDataQuantity"];

async function loadSourceEras() {
  const anyContainer = SOURCE_TIMELINE_ID_PREFIXES.some((id) => document.getElementById(id));
  if (!anyContainer) return;
  try {
    const r = await fetch(SOURCE_ERAS_URL, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status}`);
    const eras = await r.json();
    // Stash for renderTrendChart's source-colored stroke (see sourceGradientStops).
    state.eras = eras;
    for (const idPrefix of SOURCE_TIMELINE_ID_PREFIXES) {
      if (document.getElementById(idPrefix)) renderSourceTimeline(eras, idPrefix);
    }
  } catch (e) {
    console.log("Could not load source era boundaries:", e);
  }
}

async function main() {
  const container = document.getElementById("changelog");
  const chartsContainer = document.getElementById("changelogCharts");
  if (!container) return;
  try {
    const r = await fetch(CHANGELOG_URL, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status}`);
    const log = await r.json();
    if (!Array.isArray(log) || log.length === 0) {
      container.textContent = "No changelog entries yet.";
      if (chartsContainer) chartsContainer.textContent = "No data yet.";
      return;
    }
    state.log = log;
    renderChangelog();
    renderChangelogCharts();
  } catch (e) {
    container.textContent = "Could not load changelog.";
    if (chartsContainer) chartsContainer.textContent = "Could not load data.";
    console.log("Could not load changelog:", e);
  }
}

const schemeSelect = document.getElementById("schemeSelect");
if (schemeSelect) {
  schemeSelect.value = state.scheme;
  schemeSelect.addEventListener("change", (ev) => {
    state.scheme = ev.target.value;
    renderChangelog();
  });
}

// Theme toggle. about.html doesn't load app.js, so the icons and handler are
// duplicated here rather than shared; the inline <script> in each page's <head>
// already applies the saved theme before first paint, so this only has to
// handle the click and keep the button's label in sync. Both pages read/write
// the same localStorage "theme" key, so switching here carries over to the tree.
const SUN_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 3v2"/><path d="M12 19v2"/><path d="M5 5l1.4 1.4"/><path d="M17.6 17.6L19 19"/><path d="M3 12h2"/><path d="M19 12h2"/><path d="M5 19l1.4-1.4"/><path d="M17.6 6.4L19 5"/></svg>`;
const MOON_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/></svg>`;

function updateThemeToggleLabel() {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  const icon = theme === "dark" ? SUN_ICON : MOON_ICON;
  const label = theme === "dark" ? "Light" : "Dark";
  btn.innerHTML = `${icon}<span class="toggle-label">${label}</span>`;
}

const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  updateThemeToggleLabel();
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    updateThemeToggleLabel();
  });
}

const granularitySelect = document.getElementById("changelogGranularity");
if (granularitySelect) {
  granularitySelect.value = String(state.granularity);
  granularitySelect.addEventListener("change", (ev) => {
    state.granularity = Number(ev.target.value);
    renderChangelog();
    renderChangelogCharts();
  });
}

const includeOrphansCheckbox = document.getElementById("changelogIncludeOrphans");
if (includeOrphansCheckbox) {
  includeOrphansCheckbox.checked = state.includeOrphans;
  includeOrphansCheckbox.addEventListener("change", (ev) => {
    state.includeOrphans = ev.target.checked;
    renderChangelogCharts();
  });
}

// The charts pick their x-tick count from the viewport width (see buildChart), so
// re-render when we cross that breakpoint -- otherwise rotating a phone leaves the
// portrait tick count on a landscape chart. Listening to the media query rather
// than every resize event keeps this to one re-render per actual crossing.
const narrowQuery = window.matchMedia("(max-width: 800px)");
narrowQuery.addEventListener("change", () => {
  if (state.log) renderChangelogCharts();
});

// Eras first, then the changelog: renderTrendChart colors its line by source
// (sourceGradientStops) and reads state.eras synchronously, so racing these
// two fetches would leave the charts plain whenever the eras lost. The eras
// file is tiny, and a failed/slow load still resolves -- loadSourceEras
// swallows its own errors -- so main() is never blocked by it.
loadSourceEras().then(main);

// The About page carries one link up to the parent site. The parent's own
// local-links.js knows the published Atlas URLs and rewrites them down to ports
// 8001-8003 when it is itself served locally; a single link going the other way
// does not warrant a whole rewrite pass, so the markup carries the production
// URL (the file that ships is the file that is deployed) and we swap in the
// local parent only when this Atlas is being served from localhost. Port 8000
// matches the parent's serve_docs.py and Makefile, where 8001 is us.
const PARENT_LOCAL_PORT = 8000;

/* Host test copied from the parent's local-links.js, deliberately identical --
   including the private-IPv4 range, which the e-bharatisampat sibling's copy
   drops. "Local" means reachable on this machine or this LAN, not just
   loopback: browsing from a phone at 192.168.1.165:8001 is a normal way to work
   here (see the Makefile's get-server-ip-address target), and a loopback-only
   test sends those sessions to the public parent site instead. `127.` is inside
   PRIVATE_IPV4, so it needs no separate clause. */
const PRIVATE_IPV4 = /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/;

function isLocal(hostname) {
  return hostname === "localhost"
      || hostname === "[::1]"
      || hostname === "::1"
      || hostname.endsWith(".localhost")
      || hostname.endsWith(".local")   // mDNS, e.g. my-mac.local
      || PRIVATE_IPV4.test(hostname);
}

if (isLocal(location.hostname)) {
  const parentLink = document.getElementById("parentLink");
  if (parentLink) {
    parentLink.href = `http://${location.hostname}:${PARENT_LOCAL_PORT}/`;
    parentLink.dataset.localized = "true";  // visible in devtools, as in the parent
  }
}
