"""Import of a spec into the planning conversation: copies a tlc-spec-driven FEATURE FOLDER (`spec.md` required; `design.md`,
`tasks.md`, `context.md` and the other top-level `.md` files come along), read only, into
`.specs/project/loop/internal/planning/imported/<stamp>-<name>/` (credential redaction on the copy) and sends ONE fixed planning
message that tells the planning AI to read the copies and build ROADMAP, FLOWS, PROJECT and the repository scope.

No parser of tlc-spec-driven documents lives here: the AI knows the format; the server only stores, limits and redacts. Importing
again adds to the conversation (nothing is wiped). A refusal is an `InvalidRequestError` (400) and nothing is sent.
"""
import datetime
import json
import os
import re

from ..domain.project_errors import InvalidRequestError, JobNotFoundError
from ..infrastructure.claude_client import redact_secrets
from ..repositories.specs_file_repository import loop_state

IMPORTED_FOLDER_PARTS = ("planning", "imported")
META_FILE_NAME = "import.json"
MAIN_SPEC_NAME = "spec.md"
MARKDOWN_EXTENSION = ".md"
MAX_FILE_BYTES = 400 * 1024
MAX_FILES = 10
DEFAULT_IMPORT_NAME = "spec"
NAME_LIMIT = 40
IMPORT_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9._-]{1,60}$")
UNSAFE_NAME_CHARACTERS = re.compile(r"[^a-z0-9._-]+")
IMPORT_MESSAGE = (
    "Import of a spec feature. The files below are read-only copies of a tlc-spec-driven feature folder the user already wrote "
    "(main spec: {main}):\n{files}\n\n"
    "Read them with the Read tool, then build the plan of THIS project (.specs/project) from them, in the grammar of the "
    "orchestrator references:\n"
    "1. .specs/project/ROADMAP.md: one line per feature with `title=\"...\"`, `paths=<repo>:<path>,...` and `deps=`. Names must be "
    "descriptive: the slug says what the feature delivers in 3 to 6 kebab-case words (verb + object, e.g. "
    "`import-spec-feature-into-planning`, never `feature-1`, `part-a` or a bare noun like `calculator`); the title is a short "
    "sentence a reader understands without opening the spec; every stage header names the outcome of the stage, not `Stage 1`.\n"
    "2. .specs/project/FLOWS.md: one flow of 2+ steps per feature.\n"
    "3. .specs/project/PROJECT.md: the short project context (Vision filled in); never edit inside the maestro:capabilities markers.\n"
    "4. The repository scope in .specs/project/REPOS-SUGGESTED.json: every repository the spec names (absolute paths, `<repo>:path`, "
    "catalog names) with `role` edit or reference and a one-line `reason`.\n"
    "5. Whatever the spec leaves open goes to the `## Open questions` section of the ROADMAP (`<question> | recommended: <answer>`); "
    "do not ask the user for what the spec already answers.")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def sanitize_name(name):
    base = UNSAFE_NAME_CHARACTERS.sub("-", str(name or "").strip().lower()).strip("-.")
    return base[:NAME_LIMIT].strip("-.") or DEFAULT_IMPORT_NAME


class PlanningImportService:
    def __init__(self, specs_file_repository, planning_service, log, clock=None):
        self.specs_file_repository = specs_file_repository
        self.planning_service = planning_service
        self.log = log
        self.clock = clock or datetime.datetime.now

    # ------------------------------------------------ paths
    @staticmethod
    def imported_root(context):
        return os.path.join(loop_state.internal_dir(context.work_dir), *IMPORTED_FOLDER_PARTS)

    def relative_folder(self, context, import_id):
        root = os.path.relpath(self.imported_root(context), context.work_dir).replace(os.sep, "/")
        return "%s/%s" % (root, import_id)

    # ------------------------------------------------ import
    def import_spec(self, context, body):
        """{import_id, files:[name], main, at, source} after copying; raises InvalidRequestError with the reason otherwise."""
        body = body if isinstance(body, dict) else {}
        if not (isinstance(body.get("path"), str) and body["path"].strip()):
            raise InvalidRequestError("send `path`: the folder of a spec feature (with spec.md)")
        sources, default_name = self.collect_from_folder(body["path"].strip())
        name = sanitize_name(body.get("name") or default_name)
        main = MAIN_SPEC_NAME
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        import_id = self.unique_import_id(context, "%s-%s" % (stamp, name))
        folder = os.path.join(self.imported_root(context), import_id)
        for file_name, content in sources:
            self.specs_file_repository.write_text_atomic(os.path.join(folder, file_name), redact_secrets(content))
        meta = {"import_id": import_id, "files": [file_name for file_name, _ in sources], "main": main, "at": now_iso(),
                "source": "folder", "sent_at": None}
        self.specs_file_repository.write_text_atomic(os.path.join(folder, META_FILE_NAME), json.dumps(meta, indent=2))
        self.log.info("planning_spec_imported", project=context.project.id, import_id=import_id, files=len(sources))
        return {key: meta[key] for key in ("import_id", "files", "main", "at", "source")}

    def unique_import_id(self, context, base):
        candidate, number = base, 2
        while os.path.exists(os.path.join(self.imported_root(context), candidate)):
            candidate = "%s-%d" % (base, number)
            number += 1
        return candidate

    @staticmethod
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
        return ([(name, PlanningImportService.read_markdown(os.path.join(path, name))) for name in names],
                os.path.basename(os.path.normpath(path)))

    @staticmethod
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
        PlanningImportService.check_content(label, raw, text)
        return text

    @staticmethod
    def check_content(label, raw, text):
        if len(raw) > MAX_FILE_BYTES:
            raise InvalidRequestError("%s is larger than %d KB: split it or import a smaller file" % (label, MAX_FILE_BYTES // 1024))
        if not text.strip():
            raise InvalidRequestError("%s is empty" % label)

    # ------------------------------------------------ list
    def list_imports(self, context):
        """{imports:[{import_id, files, main, at, source, sent_at}]} newest first (the chat shows them as attachments)."""
        root = self.imported_root(context)
        imports = []
        for entry in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
            meta = self.specs_file_repository.read_json(os.path.join(root, entry, META_FILE_NAME))
            if meta.get("import_id") == entry:
                imports.append({key: meta.get(key) for key in ("import_id", "files", "main", "at", "source", "sent_at")})
        return {"imports": imports}

    # ------------------------------------------------ send
    def send_import(self, context, import_id):
        """Sends the ONE fixed message of an import through the planning chat; returns what `planning/send` returns ({ok, job_id})."""
        meta_path = self.meta_path(context, import_id)
        meta = self.specs_file_repository.read_json(meta_path)
        if not meta:
            raise JobNotFoundError("import not found")
        folder = self.relative_folder(context, import_id)
        files = "\n".join("- %s/%s" % (folder, name) for name in meta.get("files") or [])
        reply = self.planning_service.send_message(
            context, IMPORT_MESSAGE.format(main="%s/%s" % (folder, meta.get("main")), files=files))
        meta["sent_at"] = now_iso()
        self.specs_file_repository.write_text_atomic(meta_path, json.dumps(meta, indent=2))
        return reply

    def meta_path(self, context, import_id):
        if not isinstance(import_id, str) or not IMPORT_ID_PATTERN.match(import_id):
            raise JobNotFoundError("import not found")
        return os.path.join(self.imported_root(context), import_id, META_FILE_NAME)
