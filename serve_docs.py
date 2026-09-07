#!/usr/bin/env python3
"""Local dev server for docs/, gzip-compressing responses and setting
Cache-Control so repeated reloads during iteration (and testing over an
ngrok tunnel on a mobile data plan) don't re-transfer the full uncompressed
tree.json/changelog.json on every load. python -m http.server does neither.

## `--fulltext` is localhost-only, and deliberately not publishable

`--fulltext` exposes the extracted corpus text at `/text/<pageid>`, so the
frontend can offer a "txt" link beside each page.

**That text is never published.** It lives outside `docs/` -- under the
gitignored `data/text_extract/` -- so no build step and no deploy can pick it
up, and GitHub Pages serves `docs/` alone. Only this dev server can reach it,
and only when explicitly asked. The flag also binds to 127.0.0.1 rather than
all interfaces: 2.9 GB of Wikisource text is fine to read locally and is not
something this repo should be handing out over a network.

pageid -> filename comes from `index.jsonl`, because filenames embed a lossy,
sanitized title (`<pageid> - <Title>.txt`) that cannot be reconstructed. The
frontend therefore asks for `/text/1` and never needs to know the filename.

The corpus is IAST only -- the frontend transliterates on the fly, so a stored
Devanagari twin was 2.4 GB of the same text under a reversible mapping.
"""
import argparse
import gzip
import re
import json
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

PORT = 8001
CACHE_MAX_AGE = 60  # seconds; short, so edits during iteration aren't stale for long
COMPRESSIBLE_SUFFIXES = {".json", ".js", ".html", ".css", ".svg"}

# Where the corpus text lives, relative to the repo root (NOT docs/).
TEXT_DIR = "data/text_extract"
TEXT_PREFIX = "/text/"

# IAST only. The extractor used to write a Devanagari twin as well, which
# doubled the corpus for no new information -- same text, reversible mapping,
# and the frontend transliterates on the fly.


def load_text_index(root: Path) -> dict[str, Path]:
    """pageid -> {script: file}, from the extract's own index.

    Main namespace only. A `page/` entry is one leaf of a scan whose text
    usually ALSO appears, transcluded, inside a `main/` page -- offering both
    would present the same text twice as if it were two texts.
    """
    index_path = root / TEXT_DIR / "index.jsonl"
    if not index_path.exists():
        return {}
    index: dict[str, Path] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `main` works and `index` scans are both browsable items with a file
        # of their own. `page` rows are scan leaves, which are folded into
        # whichever work publishes them and are not offered separately.
        if row.get("ns") not in ("main", "index") or row.get("pageid") is None:
            continue
        # Every entry addresses one real file, including an assembled work --
        # the extractor folds a work's chapters into its file and does not
        # write them separately. An earlier version stored pointers and
        # streamed the pieces; it broke silently, serving a 6 MB Rāmāyaṇa as
        # 601 bytes with a 200.
        candidate = root / TEXT_DIR / row["path"]
        if not candidate.is_file():
            continue
        found = candidate
        index[str(row["pageid"])] = found
        # Also keyed by TITLE, because that is what the tree carries: a page
        # node is `page:<title>` and holds no pageid, and adding 12413 pageids
        # to tree.json purely to build a URL would grow a file the frontend
        # loads whole. The index is already in memory here, so the server
        # absorbs the mapping instead.
        if row.get("title"):
            index[row["title"]] = found
            # An Index row's title carries its namespace prefix
            # (`अनुक्रमणिका:मेदिनीकोशः.djvu`) while the tree and the search
            # index use the bare form, which is what a link asks for.
            bare = row["title"].split(":", 1)[-1]
            if bare != row["title"]:
                index.setdefault(bare, found)
    return index



# "Local" means this machine or this LAN, matching docs/local-links.js:
# browsing from a phone at 192.168.1.x is a normal way to work here.
_LOCAL_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.\d+\.\d+\.\d+|\[::1\]|"
    r"10\.[\d.]+|192\.168\.[\d.]+|172\.(1[6-9]|2\d|3[01])\.[\d.]+)"
    r"(:\d+)?$"
)


def _is_local_origin(origin: str) -> bool:
    return bool(origin) and bool(_LOCAL_ORIGIN_RE.match(origin))


class CachingGzipHandler(SimpleHTTPRequestHandler):
    fulltext = False
    text_index: dict[str, Path] = {}

    def end_headers(self):
        self.send_header("Cache-Control", f"max-age={CACHE_MAX_AGE}")
        super().end_headers()

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route.startswith(TEXT_PREFIX):
            self._serve_text(route[len(TEXT_PREFIX):], query)
            return

        path = self.translate_path(self.path)
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        if accepts_gzip and Path(path).suffix in COMPRESSIBLE_SUFFIXES and Path(path).is_file():
            self._serve_gzipped(path)
        else:
            super().do_GET()

    def do_HEAD(self):
        # The frontend probes `/text/` to learn whether this server offers the
        # corpus at all. Answered with a header rather than by the shape of a
        # 404, so the page never has to guess from a status code that a plain
        # static host could also produce.
        if self.path.split("?")[0].startswith(TEXT_PREFIX):
            self.send_response(404)
            self.send_header("X-Fulltext-Mode", "on" if self.fulltext else "off")
        # Sagarasangama runs on another port, so its probe and its `txt`
        # links are cross-origin. Allowed narrowly: only the /text/ route,
        # only in fulltext mode, and only for a localhost/private-network
        # origin -- the same "local means this machine or this LAN" rule
        # local-links.js uses. Nothing here widens what the PUBLISHED site
        # can reach, because the published site has no /text/ route at all.
        origin = self.headers.get("Origin", "")
        if self.fulltext and _is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", "X-Fulltext-Mode")

            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_HEAD()

    def _serve_text(self, pageid, query):
        """`/text/<pageid>` -> the extracted text (IAST), as UTF-8.

        The pageid is looked up in a prebuilt index, never joined onto a path,
        so a crafted `/text/../../etc/passwd` finds no key and gets a 404. No
        user-supplied string ever reaches the filesystem.
        """
        path = self.text_index.get(unquote(pageid).strip("/"))
        if path is None:
            self.send_error(404, "no text for that pageid")
            return
        raw = path.read_bytes()
        body = gzip.compress(raw) if "gzip" in self.headers.get("Accept-Encoding", "") else raw
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        # Sagarasangama runs on another port, so its probe and its `txt`
        # links are cross-origin. Allowed narrowly: only the /text/ route,
        # only in fulltext mode, and only for a localhost/private-network
        # origin -- the same "local means this machine or this LAN" rule
        # local-links.js uses. Nothing here widens what the PUBLISHED site
        # can reach, because the published site has no /text/ route at all.
        origin = self.headers.get("Origin", "")
        if self.fulltext and _is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", "X-Fulltext-Mode")

        if body is not raw:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_gzipped(self, path):
        raw = Path(path).read_bytes()
        compressed = gzip.compress(raw)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(compressed)))
        self.end_headers()
        self.wfile.write(compressed)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("port", nargs="?", type=int, default=PORT)
    parser.add_argument(
        "--fulltext", action="store_true",
        help="serve the extracted corpus text at /text/<pageid>, so the "
             "frontend shows a `txt` link. LOCALHOST ONLY -- the text lives "
             "outside docs/ and is never published",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    CachingGzipHandler.fulltext = args.fulltext
    if args.fulltext:
        CachingGzipHandler.text_index = load_text_index(root)

    # Pin the served directory to docs/ rather than inheriting the caller's
    # cwd, so running this from the repo root cannot expose data/.
    handler = partial(CachingGzipHandler, directory=str(root / "docs"))

    # Loopback in fulltext mode: everything else here is already-published
    # material, but the corpus text is not ours to hand out.
    host = "127.0.0.1" if args.fulltext else ""
    server = HTTPServer((host, args.port), handler)
    print(f"Serving docs/ on http://localhost:{args.port} "
          f"(gzip + Cache-Control: max-age={CACHE_MAX_AGE})")
    if args.fulltext:
        count = len(CachingGzipHandler.text_index)
        print(f"  fulltext: {count} pages at /text/<pageid> "
              f"-- LOCALHOST ONLY, never published")
        if not count:
            print("            (none found -- run `make extract-text`)")
    server.serve_forever()


if __name__ == "__main__":
    main()
