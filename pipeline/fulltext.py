"""The one place this repo mentions `rivulet`, and it never requires it.

Fulltext acquisition and clean-text output live in a private package. This
Atlas is public and **runs to completion without it**: the dump download, the
tree build, and the sizes are all here. What rivulet adds is the per-text
extraction -- writing the corpus out as files -- which is a publishing act
rather than a build step.

So the dependency is optional and one-way. `try: import rivulet` is the whole
mechanism, and the failure is a *distinct exit code* rather than a traceback:

    0   it worked
    1   it ran and failed
    2   the machinery is not installed

2 is not an error to be fixed on this machine. It means "this build cannot
produce fulltext output, and that is a supported configuration" -- which is
what lets a public checkout run `make` targets without pretending a private
package is missing by mistake.
"""

import sys

EXIT_NOT_INSTALLED = 2

_MISSING = """\
fulltext machinery not installed.

Text extraction lives in the private `rivulet` package, which is not present
in this environment. Everything else in this repo runs without it: the dump
refresh, `make process`, the tree, and the byte sizes are all unaffected.

To enable it:  pip install -e ../../rivulet
"""


def load_writer():
    """Return rivulet's `write_text_extract`, or exit 2 if it is absent.

    Called at the point of use rather than at import, so that merely importing
    this module -- which `process.py` does unconditionally -- never depends on
    rivulet being installed.
    """
    try:
        from rivulet.extract.wikisource.text_extractor import write_text_extract
    except ImportError:
        print(_MISSING, file=sys.stderr)
        raise SystemExit(EXIT_NOT_INSTALLED)
    return write_text_extract


def available() -> bool:
    """Whether fulltext output is possible here. Asks, rather than exits.

    For callers that want to report or branch instead of stopping -- the
    `has_text` flag in the built tree, for instance.
    """
    try:
        import rivulet  # noqa: F401
    except ImportError:
        return False
    return True
