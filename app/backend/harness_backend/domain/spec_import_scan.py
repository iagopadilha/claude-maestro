"""Scan of a spec feature folder for the planning import: which `.md` files are taken, the size and count limits and the
refusals (a `InvalidRequestError`, 400). Pure file reading; nothing is written here."""
import os

from .project_errors import InvalidRequestError

MAIN_SPEC_NAME = "spec.md"
MARKDOWN_EXTENSION = ".md"
MAX_FILE_BYTES = 400 * 1024
MAX_FILES = 10


def collect_from_folder(path):
    """([(file name, text)], default import name = the folder name). Only a feature folder with `spec.md`; it takes its
    top-level `.md` files (a single file or pasted text is refused)."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        raise InvalidRequestError("pick the feature folder, not a file: the folder that has spec.md")
    if not os.path.isdir(path):
        raise InvalidRequestError("folder not found: %s" % path)
    names = sorted(name for name in os.listdir(path)
                   if name.lower().endswith(MARKDOWN_EXTENSION) and os.path.isfile(os.path.join(path, name)))
    if MAIN_SPEC_NAME not in names:
        raise InvalidRequestError("the folder has no spec.md: pick the folder of a spec feature (spec.md, design.md, tasks.md)")
    if len(names) > MAX_FILES:
        raise InvalidRequestError("the folder has %d .md files: the limit is %d" % (len(names), MAX_FILES))
    return ([(name, read_markdown(os.path.join(path, name))) for name in names],
            os.path.basename(os.path.normpath(path)))


def read_markdown(path):
    label = os.path.basename(path)
    if not label.lower().endswith(MARKDOWN_EXTENSION):
        raise InvalidRequestError("%s is not a Markdown file (.md): only Markdown specs are imported" % label)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as failure:
        raise InvalidRequestError("could not read %s: %s" % (label, failure))
    text = raw.decode("utf-8", errors="replace")
    check_content(label, raw, text)
    return text


def check_content(label, raw, text):
    if len(raw) > MAX_FILE_BYTES:
        raise InvalidRequestError("%s is larger than %d KB: split it or import a smaller file" % (label, MAX_FILE_BYTES // 1024))
    if not text.strip():
        raise InvalidRequestError("%s is empty" % label)
