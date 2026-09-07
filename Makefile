.PHONY: serve-fulltext refresh-dump refresh-dump-force process extract-text serve ngrok backfill regen-changelog audit audit-update-about verify

# Resolve the latest complete monthly dump export on dumps.wikimedia.org and
# compare it against data/dump/: download/verify/decompress whatever's missing or
# stale, remove leftover files from a prior export, and no-op if everything
# already matches.
refresh-dump:
	python -m pipeline.fetch

# Same as refresh-dump, but re-download, re-verify, and re-decompress every
# part file even if already present and verified locally.
refresh-dump-force:
	python -m pipeline.fetch --force

# Build docs/data/tree.json from the downloaded dump. Override worker count
# with e.g. `make process WORKERS=4` (default: os.cpu_count()).
process:
	python -m pipeline.process --out docs/data/tree.json $(if $(WORKERS),--workers $(WORKERS))

# Same as `process`, but ALSO writes the corpus text to
# data/text_extract/{deva,iast}/{main,page}/ -- the text `process` already
# computes for its size metric and then throws away. Adds only the file writes
# to the run. Gitignored; this repo hosts no text.
#
# NEEDS the private `rivulet` package, which owns the writer. Without it this
# EXITS 2 ("fulltext machinery not installed"), distinct from 1 = failure --
# and `make process` itself is entirely unaffected either way.
extract-text:
	python -m pipeline.process --out docs/data/tree.json --extract-text $(if $(WORKERS),--workers $(WORKERS))

# Report likely breadcrumb/category structural problems on the live wiki for
# manual review -- never mutates the dump or docs/data/tree.json. See
# notes/wikisource-editing-plan.md.
audit:
	python -m pipeline.audit

# Same as audit, but also regenerates the audit findings section of
# docs/about.html.
audit-update-about:
	python -m pipeline.audit --update-about

# Check that the generated artifacts committed under docs/ agree with each
# other -- most importantly that changelog.json actually covers the dump
# VERSION claims to be publishing. Offline and fast; the deploy workflow runs
# this as a gate, so running it before pushing catches the same problems early.
verify:
	python -m pipeline.verify_publish

# Walk the full historical range and rebuild docs/data/changelog.json from
# scratch. Safe to interrupt and rerun; already-downloaded/materialized
# months are reused, not redone. Takes hours on a full run. See CLAUDE.md
# for how this works internally.
backfill:
	bash pipeline/run_backfill_sequence.sh --workers 10

# Rebuild docs/data/changelog.json from the historical snapshots already on
# disk, with no downloading and no network access at all -- fast (under a
# minute). Use this instead of `make backfill` when you trust the existing
# snapshots and just want the changelog regenerated, e.g. after a fix to how
# entries are diffed/compared.
regen-changelog:
	rm -f docs/data/changelog.json
	python -m pipeline.backfill --months $(shell ls data/dump/_backfill_snapshots | sed -E 's/^tree-(.+)\.json(\.gz)?$$/\1/' | sort -u)

# Serve the frontend (docs/) locally, on port 8001. Unlike plain
# `python -m http.server`, gzip-compresses JSON/JS/HTML/CSS responses and
# sets Cache-Control -- matters most when tunneling over ngrok on a mobile
# data plan, where re-transferring uncompressed tree.json/changelog.json on
# every reload burns through a data cap fast.
# Same server, plus the extracted text at /text/<pageid>, so each page gets a
# `txt` badge. LOCALHOST ONLY -- binds 127.0.0.1, and the text lives outside
# docs/ so no deploy can pick it up. Needs `make extract-text` to have run.
serve-fulltext:
	cd docs && python ../serve_docs.py --fulltext $(ARGS)

serve:
	cd docs && python ../serve_docs.py

# Expose the local server (port 8001) via a public ngrok tunnel.
ngrok:
	ngrok http 8001

free-server-port:
	kill $$(lsof -ti tcp:8001)