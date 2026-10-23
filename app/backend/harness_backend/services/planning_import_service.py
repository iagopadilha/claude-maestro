"""Import of a spec into the planning conversation: reads a tlc-spec-driven FEATURE FOLDER (`spec.md` required; every `.md`
inside it, subfolders too, up to the scan limits), read only, and copies it twice (credential redaction on both copies): into
`.specs/project/loop/internal/planning/imported/<stamp>-<name>/` (relative paths kept, with `import.json` and `IMPORT-LOG.md`) and
into `.specs/features/<slug>/` of the work folder. It sends ONE fixed planning message that tells the planning AI to read the
copies and build ROADMAP, FLOWS, PROJECT, the repository scope and `.specs/project/IMPORT-COVERAGE.md`.

`preview_import` shows what an import would take (no copy, no AI). The same feature (same source folder or same slug) is not
imported twice: `already_imported` (409) carries the earlier log path. Another feature adds to the plan. A refusal of the folder
is an `InvalidRequestError` (400) and nothing is copied or sent.
"""
import datetime
import json
import os
import re

from ..domain.project_errors import InvalidRequestError, JobNotFoundError, RepoConflictError
from ..domain.spec_import_scan import MAIN_SPEC_NAME, STATUS_READ, build_warnings, detect_items, scan_folder
from ..infrastructure.claude_client import redact_secrets
from ..repositories.specs_file_repository import loop_state
from .context_block_service import ContextBlockService

IMPORTED_FOLDER_PARTS = ("planning", "imported")
FEATURES_FOLDER_PARTS = (".specs", "features")
META_FILE_NAME = "import.json"
IMPORT_LOG_NAME = "IMPORT-LOG.md"
DEFAULT_IMPORT_NAME = "spec"
NAME_LIMIT = 40
ALREADY_IMPORTED_CODE = "already_imported"
FEATURE_FOLDER_EXISTS_CODE = "feature_folder_exists"
COVERAGE_LINE_PREFIX = "Coverage ("
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
    "do not ask the user for what the spec already answers.\n\n"
    "Coverage rules (nothing of the source may be lost). The source is also copied to {feature_dir}/ (the complete spec of this "
    "feature):\n"
    "- The ROADMAP is an INDEX, not a copy: every feature built from the source points to {feature_dir}/ and lists the "
    "requirement IDs it covers as `reqs=ORIG-01..06,CRIA-01..08`.\n"
    "- A Large source (SCOPE Large/Complex, or a tasks.md with waves or tracks) is split into ROADMAP features following those "
    "waves and tracks (deps from the wave order); a Small/Medium source stays one feature. Never rewrite the imported spec: the "
    "spec-writer reviews and completes it.\n"
    "- Write .specs/project/IMPORT-COVERAGE.md: under `## {slug}` one Markdown table `| Item | Kind | Destination |` with one row "
    "for EVERY id below and EVERY core section present. Kind is one of requirement, decision, task, goal, out-of-scope, "
    "edge-case, validation, environment, risk, paths, open-question, other. Destination is a location (`ROADMAP:[slug] reqs`, "
    "`RULES.md#...`, `FLOWS.md#...`, `context.md`) or `dropped: <reason>`; ranges like `ORIG-01..06` are accepted.\n"
    "- Never summarize a requirement, a limit or a validation away. `dropped: <reason>` is the only way to leave an item out.\n\n"
    "Detected in the source:\n{detected}")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def normalize_source(path):
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def format_size(size):
    return "%d KB" % max(1, round(size / 1024))


def describe_detected(items):
    """The detected ids and the core sections present, as the lines of the import message."""
    lines = ["- %s: %s" % (key, ", ".join(items.get(key) or []) or "none") for key in ("requirements", "decisions", "tasks")]
    sections = [name for name, present in (items.get("sections") or {}).items() if present]
    lines.append("- core sections present: %s" % (", ".join(sections) or "none"))
    return "\n".join(lines)


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

    @staticmethod
    def feature_relative_folder(slug):
        return "/".join(FEATURES_FOLDER_PARTS + (slug,))

    @staticmethod
    def feature_folder_exists(context, slug):
        return os.path.exists(os.path.join(context.work_dir, *FEATURES_FOLDER_PARTS, slug))

    # ------------------------------------------------ preview
    def preview_import(self, context, body):
        """{slug, files, skipped, counts, items, warnings, refused, already_imported}: what an import would take. Nothing is
        copied and no AI is called."""
        source = self.read_source_path(body)
        scan = scan_folder(source)
        read_files = [entry for entry in scan["files"] if entry["status"] == STATUS_READ]
        slug = self.slug_of(body, source)
        items = detect_items([] if scan["refused"] else read_files)
        warnings = [] if scan["refused"] else build_warnings(scan, items, ContextBlockService.read_conformance(context.work_dir, ""))
        earlier = self.find_earlier_import(context, source, slug)
        if not earlier and self.feature_folder_exists(context, slug):
            warnings.append("%s already exists without an import record: the import will be refused" % self.feature_relative_folder(slug))
        counts = dict(scan["counts"], requirements=len(items["requirements"]), decisions=len(items["decisions"]),
                      tasks=len(items["tasks"]), out_of_scope_rows=items["out_of_scope_rows"], edge_cases=items["edge_cases"],
                      success_criteria=items["success_criteria"])
        return {"slug": slug, "files": [self.public_entry(entry) for entry in read_files],
                "skipped": [self.public_entry(entry) for entry in scan["files"] if entry["status"] != STATUS_READ],
                "counts": counts, "items": items, "warnings": warnings, "refused": scan["refused"], "already_imported": earlier}

    @staticmethod
    def public_entry(entry):
        return {key: value for key, value in entry.items() if key not in ("text", "refusal")}

    @staticmethod
    def read_source_path(body):
        body = body if isinstance(body, dict) else {}
        if not (isinstance(body.get("path"), str) and body["path"].strip()):
            raise InvalidRequestError("send `path`: the folder of a spec feature (with spec.md)")
        return body["path"].strip()

    @staticmethod
    def slug_of(body, source):
        name = body.get("name") if isinstance(body, dict) else None
        return sanitize_name(name or os.path.basename(os.path.normpath(os.path.expanduser(source))))

    # ------------------------------------------------ import
    def import_spec(self, context, body):
        """{import_id, files:[relative path], main, at, source} after copying; raises InvalidRequestError with the reason, or
        RepoConflictError `already_imported` (earlier log path in the response) / `feature_folder_exists`."""
        source = self.read_source_path(body)
        scan = scan_folder(source)
        if scan["refused"]:
            raise InvalidRequestError(scan["refused"]["reason"])
        slug = self.slug_of(body, source)
        self.refuse_second_import(context, source, slug)
        read_files = [entry for entry in scan["files"] if entry["status"] == STATUS_READ]
        items = detect_items(read_files)
        warnings = build_warnings(scan, items, ContextBlockService.read_conformance(context.work_dir, ""))
        import_id = self.unique_import_id(context, "%s-%s" % (self.clock().strftime("%Y%m%d-%H%M%S"), slug))
        folder = os.path.join(self.imported_root(context), import_id)
        feature_folder = os.path.join(context.work_dir, *FEATURES_FOLDER_PARTS, slug)
        for entry in read_files:
            redacted = redact_secrets(entry["text"])
            relative = entry["relative_path"].split("/")
            self.specs_file_repository.write_text_atomic(os.path.join(folder, *relative), redacted)
            self.specs_file_repository.write_text_atomic(os.path.join(feature_folder, *relative), redacted)
        self.specs_file_repository.write_text_atomic(
            os.path.join(folder, IMPORT_LOG_NAME), self.build_log_text(source, slug, import_id, scan, items, warnings))
        meta = {"import_id": import_id, "source": "folder", "source_path": os.path.abspath(os.path.expanduser(source)),
                "slug": slug, "feature_dir": self.feature_relative_folder(slug),
                "files": [entry["relative_path"] for entry in read_files] + [IMPORT_LOG_NAME], "main": MAIN_SPEC_NAME,
                "at": now_iso(), "sent_at": None,
                "items": {"requirements": items["requirements"], "decisions": items["decisions"], "tasks": items["tasks"],
                          "sections": items["sections"]}}
        self.specs_file_repository.write_text_atomic(os.path.join(folder, META_FILE_NAME), json.dumps(meta, indent=2))
        self.log.info("planning_spec_imported", project=context.project.id, import_id=import_id, files=len(read_files))
        return {key: meta[key] for key in ("import_id", "files", "main", "at", "source")}

    def refuse_second_import(self, context, source, slug):
        earlier = self.find_earlier_import(context, source, slug)
        if earlier:
            raise RepoConflictError(
                "this spec feature was already imported (%s): see the log %s. A changed spec goes through the "
                "planning chat (Back to planning), not a second import" % (earlier["import_id"], earlier["log_path"]),
                code=ALREADY_IMPORTED_CODE, extras=earlier)
        if self.feature_folder_exists(context, slug):
            raise RepoConflictError(
                "%s already exists and was not imported by the app: rename the source folder or remove it by hand"
                % self.feature_relative_folder(slug), code=FEATURE_FOLDER_EXISTS_CODE,
                extras={"feature_dir": self.feature_relative_folder(slug)})

    def find_earlier_import(self, context, source, slug):
        """{import_id, log_path} of an import with the same normalized source folder or the same slug, or None."""
        wanted = normalize_source(source)
        root = self.imported_root(context)
        for entry in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            meta = self.specs_file_repository.read_json(os.path.join(root, entry, META_FILE_NAME))
            if meta.get("import_id") != entry:
                continue
            same_source = bool(meta.get("source_path")) and normalize_source(meta["source_path"]) == wanted
            if meta.get("slug") == slug or same_source:
                return {"import_id": entry, "log_path": "%s/%s" % (self.relative_folder(context, entry), IMPORT_LOG_NAME)}
        return None

    @staticmethod
    def build_log_text(source, slug, import_id, scan, items, warnings):
        rows = ["| %s | %s | %s | %s |" % (entry["relative_path"], format_size(entry["size"]), entry["sha256"] or "-",
                                          "read" if entry["status"] == STATUS_READ else "skipped: %s" % entry["reason"])
                for entry in scan["files"]]
        detected = ("Detected: %d requirements, %d decisions, %d tasks, %d out-of-scope rows, %d edge cases, %d success criteria."
                    % (len(items["requirements"]), len(items["decisions"]), len(items["tasks"]), items["out_of_scope_rows"],
                       items["edge_cases"], items["success_criteria"]))
        lines = (["# Import log — %s" % slug, "", "- Source: %s" % source.replace(os.sep, "/"),
                  "- Slug: %s · Import id: %s" % (slug, import_id), "", "| File | Size | SHA-256 | Result |", "| --- | --- | --- | --- |"]
                 + rows + ["", detected, "Warnings: %s" % ("; ".join(warnings) or "none")])
        return "\n".join(lines) + "\n"

    def unique_import_id(self, context, base):
        candidate, number = base, 2
        while os.path.exists(os.path.join(self.imported_root(context), candidate)):
            candidate = "%s-%d" % (base, number)
            number += 1
        return candidate

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
        slug = meta.get("slug") or import_id
        message = IMPORT_MESSAGE.format(main="%s/%s" % (folder, meta.get("main")), files=files, slug=slug,
                                        feature_dir=meta.get("feature_dir") or self.feature_relative_folder(slug),
                                        detected=describe_detected(meta.get("items") or {}))
        log_path = os.path.join(os.path.dirname(meta_path), IMPORT_LOG_NAME)
        sent_meta = dict(meta, sent_at=now_iso())
        self.specs_file_repository.write_text_atomic(meta_path, json.dumps(sent_meta, indent=2))  # before the send: a job that fails at once unmarks it

        def unmark(failure):
            self.specs_file_repository.write_text_atomic(meta_path, json.dumps(meta, indent=2))
            self.append_to_log(log_path, "Not sent: %s" % str(failure)[:200])

        try:
            reply = self.planning_service.send_message(context, message, on_prepare_failed=unmark)
        except Exception:
            self.specs_file_repository.write_text_atomic(meta_path, json.dumps(meta, indent=2))
            raise
        self.append_to_log(log_path, "Sent: %s" % sent_meta["sent_at"])
        return reply

    def meta_path(self, context, import_id):
        if not isinstance(import_id, str) or not IMPORT_ID_PATTERN.match(import_id):
            raise JobNotFoundError("import not found")
        return os.path.join(self.imported_root(context), import_id, META_FILE_NAME)

    # ------------------------------------------------ coverage
    def record_coverage(self, context, result):
        """Appends `Coverage (<at>): N covered, N dropped, N missing.` to the IMPORT-LOG.md of the latest import when it
        differs from the last recorded one. `result`: {covered, dropped, missing}, each a list of ids or a count."""
        imports = self.list_imports(context)["imports"]
        if not imports or not isinstance(result, dict):
            return False
        log_path = os.path.join(self.imported_root(context), imports[0]["import_id"], IMPORT_LOG_NAME)
        counts = tuple(self.count_of(result.get(key)) for key in ("covered", "dropped", "missing"))
        summary = "%d covered, %d dropped, %d missing." % counts
        missing = result.get("missing")
        if isinstance(missing, (list, tuple, set)) and missing:
            summary += " Missing: %s." % ", ".join(sorted(str(item) for item in missing))
        recorded = [line for line in (self.specs_file_repository.read_text(log_path) or "").splitlines()
                    if line.startswith(COVERAGE_LINE_PREFIX)]
        if recorded and recorded[-1].split("): ", 1)[-1] == summary:
            return False
        return self.append_to_log(log_path, "%s%s): %s" % (COVERAGE_LINE_PREFIX, now_iso(), summary))

    @staticmethod
    def count_of(value):
        return len(value) if isinstance(value, (list, tuple, set)) else int(value or 0)

    def append_to_log(self, log_path, line):
        current = self.specs_file_repository.read_text(log_path)
        if current is None:
            return False
        self.specs_file_repository.write_text_atomic(log_path, current.rstrip("\n") + "\n" + line + "\n")
        return True
