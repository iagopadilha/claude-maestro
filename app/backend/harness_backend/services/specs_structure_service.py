"""`.specs/` structure of a project: creates it from the `templates/` models without ever overwriting, puts `.specs/` in
`.gitignore`, links `RULES.md` from `CLAUDE.md` and writes the `Conformance:` line (through `engine/classify_structure.py`).

Read (what exists), decide (what is missing) and write (only what is missing) stay separate in each operation.
"""
import datetime
import os
import re
from typing import Protocol

from ..domain.project_errors import InvalidPathError, LoopRunningError
from ..repositories.specs_file_repository import classify_structure, list_test_environment_problems, loop_state, run_hints

CONFORMANCE_VALUES = ("STANDARD", "LEGACY")
CONFORMANCE_LINE_PATTERN = re.compile(r"^Conformance:\s*(STANDARD|LEGACY)", re.M)  # the same check as run_loop --check
STRUCTURE_FOLDERS = ("project", "features")  # `.specs/codebase` is no longer created (the map lives in docs/codebase); old projects keep theirs
PROJECT_TEMPLATE_FILES = ("PROJECT.md", "ROADMAP.md", "STATE.md", "FLOWS.md")
RULES_FILE_NAME = "RULES.md"
TEST_ENVIRONMENT_FILE_NAME = "TEST-ENVIRONMENT.md"
RULES_TEMPLATE_NAME = "PROJECT_RULES.md"
TEST_ENVIRONMENT_TEMPLATE_NAME = "TEST_ENVIRONMENT.md"
ENV_TEST_FILE_NAME = ".env.test"
ENV_TEST_TEMPLATE_NAME = "ENV_TEST.example"
ENV_IGNORE_LINES = (".env", ".env.*", "!.env.example", ".specs/project/.env.test", ".specs/project/ACCESS.json")
SPECS_TEMPLATES_DIR_NAME = "specs"
RULES_IMPORT_LINE = loop_state.RULES_IMPORT
SPECS_IGNORE_LINE = ".specs/"
TRASH_RELATIVE_DIR = ".specs/.trash"  # where "Reset specs" moves the old state; git-ignored, never read by the app
TRASH_IGNORE_LINE = TRASH_RELATIVE_DIR + "/"
SPECS_IGNORE_VARIANTS = (".specs", ".specs/", "/.specs", "/.specs/", ".specs/*", "/.specs/*")
MARKDOWN_BLOCK_PATTERN = re.compile(r"```markdown\r?\n(.*?)\r?\n```", re.S)
EXAMPLE_RULE_LINE_PATTERN = re.compile(r"^\|\s*R-\d+\s*\|")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\|[-\s|]+\|\s*$")
EXAMPLE_RULE_COMMENT = ('<!-- example (delete this comment): | R-01 | Never create a tracking route | real cost | '
                        'Reject a feature that depends on it | user, YYYY-MM-DD: "exact phrase" | -->')
MINIMAL_RULES = "# Limiting rules\n\n| ID | Rule | Why | How to apply | Origin |\n|----|------|-----|--------------|--------|\n"
MINIMAL_TEST_ENVIRONMENT = "# Test environment (DEV)\n\nFILL_IN\n"
CONFORMANCE_NOTE = "written by the harness app"
PROJECT_NAME_PLACEHOLDER = "{project_name}"
TODAY_PLACEHOLDER = "{today}"


def extract_markdown_block(text):
    """Content of the template's first ```markdown block, or '' when there is none."""
    found = MARKDOWN_BLOCK_PATTERN.search(text or "")
    return found.group(1).strip() + "\n" if found else ""


def build_rules_without_examples(block):
    """RULES.md of a new project: the table header stays, the template's example rules (R-01..) become a comment."""
    lines = []
    for line in block.splitlines():
        if EXAMPLE_RULE_LINE_PATTERN.match(line.strip()):
            continue
        lines.append(line)
        if TABLE_SEPARATOR_PATTERN.match(line.strip()):
            lines.append(EXAMPLE_RULE_COMMENT)
    return "\n".join(lines).rstrip() + "\n"


def ignores_specs(gitignore_text):
    return any(line.strip() in SPECS_IGNORE_VARIANTS for line in (gitignore_text or "").splitlines())


class ISpecsStructureService(Protocol):
    def initialize_project_structure(self, context, conformance=None) -> dict: ...
    def write_project_conformance(self, context, value) -> str: ...
    def initialize_structure(self, project_dir, conformance=None, project_name="") -> dict: ...
    def check_structure(self, project_dir) -> dict: ...
    def get_conformance(self, project_dir): ...
    def suggest_conformance(self, project_dir) -> dict: ...
    def write_conformance(self, project_dir, value) -> str: ...
    def create_test_environment_template(self, project_dir) -> bool: ...


class SpecsStructureService:
    def __init__(self, specs_file_repository, templates_dir, process_runner, log):
        self.specs_file_repository = specs_file_repository
        self.templates_dir = templates_dir
        self.process_runner = process_runner
        self.log = log

    # ------------------------------------------------ from the project context (routes /p/<id>/api/...)
    def initialize_project_structure(self, context, conformance=None):
        """Initializes in the `work` dir (loop worktree when it exists). Refused with the loop running: it touches CLAUDE.md
        and .gitignore."""
        self.ensure_loop_stopped(context)
        return self.initialize_structure(context.work_dir, conformance, context.project.name)

    def write_project_conformance(self, context, value):
        self.ensure_loop_stopped(context)
        return self.write_conformance(context.work_dir, value)

    def ensure_loop_stopped(self, context):
        if self.process_runner.is_alive(self.specs_file_repository.read_loop_lock_pid(context.work_dir)):
            raise LoopRunningError("loop running: stop the loop before changing the project structure")

    # ------------------------------------------------ read
    def check_structure(self, project_dir):
        """{"ok", "missing": [paths relative to .specs/]}: ok = `.specs/project/` exists (initialize completes the rest)."""
        specs_dir = os.path.join(project_dir, ".specs")
        missing = [name + "/" for name in STRUCTURE_FOLDERS if not os.path.isdir(os.path.join(specs_dir, name))]
        missing += ["project/" + name for name in PROJECT_TEMPLATE_FILES + (RULES_FILE_NAME,)
                    if not os.path.isfile(os.path.join(specs_dir, "project", name))]
        return {"ok": os.path.isdir(os.path.join(specs_dir, "project")), "missing": missing}

    def get_conformance(self, project_dir):
        text = self.specs_file_repository.read_text(os.path.join(project_dir, "CLAUDE.md")) or ""
        found = CONFORMANCE_LINE_PATTERN.search(text)
        return found.group(1) if found else None

    def suggest_conformance(self, project_dir):
        stack, suggestion, evidence = classify_structure.analyze(project_dir)
        return {"suggestion": suggestion, "stack": stack, "evidence": evidence}

    # ------------------------------------------------ write
    def initialize_structure(self, project_dir, conformance=None, project_name=""):
        """Creates what is missing in `.specs/` (never overwrites). Returns {"created","changed","existing","conformance"}."""
        if not os.path.isdir(project_dir):
            raise InvalidPathError("project folder does not exist: %s" % project_dir)
        conformance_value = self.validate_conformance(conformance) if conformance else None
        name = project_name or os.path.basename(os.path.normpath(project_dir))
        created, existing, changed = [], [], []
        for path in self.list_structure_folders(project_dir):
            if os.path.isdir(path):
                continue
            os.makedirs(path, exist_ok=True)
            created.append(self.relative_to_project(project_dir, path) + "/")
        for path, content in self.build_template_files(project_dir, name).items():
            (created if self.specs_file_repository.create_file_if_missing(path, content) else existing).append(
                self.relative_to_project(project_dir, path))
        changed += self.ensure_specs_in_gitignore(project_dir)
        changed += self.ensure_env_files_in_gitignore(project_dir)
        changed += self.ensure_rules_in_claude_md(project_dir, name)
        changed += self.ensure_run_block(project_dir)
        if conformance_value:
            self.write_conformance(project_dir, conformance_value)
            changed.append("CLAUDE.md (Conformance: %s)" % conformance_value)
        if self.create_test_environment_template(project_dir):  # always: the planning chat edits it (it cannot create files)
            created.append(".specs/project/" + TEST_ENVIRONMENT_FILE_NAME)
        self.log.info("specs_structure_initialized", folder=project_dir, created=len(created), changed=len(changed))
        return {"created": created, "changed": changed, "existing": existing, "conformance": self.get_conformance(project_dir)}

    def write_conformance(self, project_dir, value):
        """Writes the Conformance section of CLAUDE.md with the engine code (the same as `classify_structure.py --write`)."""
        validated_value = self.validate_conformance(value)
        stack, suggestion, _ = classify_structure.analyze(project_dir)
        classify_structure.write_claude_md(project_dir, validated_value, stack, suggestion, CONFORMANCE_NOTE)
        self.log.info("conformance_written", folder=project_dir, value=validated_value)
        return validated_value

    def create_test_environment_template(self, project_dir):
        """Creates `.specs/project/TEST-ENVIRONMENT.md` from the template (with FILL_IN). False when it already existed."""
        content = extract_markdown_block(self.read_template(TEST_ENVIRONMENT_TEMPLATE_NAME)) or MINIMAL_TEST_ENVIRONMENT
        self.create_env_test_template(project_dir)
        return self.specs_file_repository.create_file_if_missing(
            os.path.join(project_dir, ".specs", "project", TEST_ENVIRONMENT_FILE_NAME), content)

    def create_env_test_template(self, project_dir):
        """Creates `.specs/project/.env.test` (the project's test VALUES, commented out) and makes sure git ignores it."""
        self.ensure_env_files_in_gitignore(project_dir)
        return self.specs_file_repository.create_file_if_missing(
            os.path.join(project_dir, ".specs", "project", ENV_TEST_FILE_NAME), self.read_template(ENV_TEST_TEMPLATE_NAME))

    def ensure_env_files_in_gitignore(self, project_dir):
        """Appends the missing secret-file lines (`.env`, `.env.*`, `!.env.example`, `.specs/project/.env.test`, `.specs/project/ACCESS.json`) to .gitignore."""
        path = os.path.join(project_dir, ".gitignore")
        text = self.specs_file_repository.read_text(path) or ""
        present = {line.strip() for line in text.splitlines()}
        missing = [line for line in ENV_IGNORE_LINES if line not in present]
        if not missing:
            return []
        separator = "" if not text or text.endswith("\n") else "\n"
        self.specs_file_repository.append_text(path, separator + "\n".join(missing) + "\n")
        return [".gitignore (env files)"]

    def ensure_specs_in_gitignore(self, project_dir):
        path = os.path.join(project_dir, ".gitignore")
        text = self.specs_file_repository.read_text(path)
        if text is not None and ignores_specs(text):
            return []
        separator = "" if not text or text.endswith("\n") else "\n"
        self.specs_file_repository.append_text(path, separator + SPECS_IGNORE_LINE + "\n")
        return [".gitignore (.specs/)"]

    def ensure_specs_trash_in_gitignore(self, project_dir):
        """`.specs/.trash/` must never reach git. Nothing to add when the whole `.specs/` is already ignored."""
        path = os.path.join(project_dir, ".gitignore")
        text = self.specs_file_repository.read_text(path)
        if text is not None and (ignores_specs(text) or TRASH_IGNORE_LINE in {line.strip() for line in text.splitlines()}):
            return []
        separator = "" if not text or text.endswith("\n") else "\n"
        self.specs_file_repository.append_text(path, separator + TRASH_IGNORE_LINE + "\n")
        return [".gitignore (.specs/.trash/)"]

    def ensure_run_block(self, project_dir):
        """CLAUDE.md gets the generated "How to run" block (https profile first); rewritten when the project changed."""
        changed, conflicts = run_hints.write_block(project_dir)
        for line in conflicts:
            self.log.warn("claude_md_run_line_without_https", folder=project_dir, line=line[:160])
        return ["CLAUDE.md (How to run)"] if changed else []

    def ensure_rules_in_claude_md(self, project_dir, project_name):
        """CLAUDE.md imports RULES.md (read in every session). Appends the line; never rewrites what exists."""
        path = os.path.join(project_dir, "CLAUDE.md")
        text = self.specs_file_repository.read_text(path)
        if text is not None and RULES_IMPORT_LINE in text:
            return []
        if text is None:
            self.specs_file_repository.create_file_if_missing(path, "# %s\n\n%s\n" % (project_name, RULES_IMPORT_LINE))
            return ["CLAUDE.md (created with %s)" % RULES_IMPORT_LINE]
        separator = "\n" if text.endswith("\n") else "\n\n"
        self.specs_file_repository.append_text(path, separator + RULES_IMPORT_LINE + "\n")
        return ["CLAUDE.md (%s)" % RULES_IMPORT_LINE]

    # ------------------------------------------------ support
    @staticmethod
    def roadmap_requires_test_environment(project_dir):
        """Same rule as the engine preflight (`run_loop.test_environment_problems`): a ROADMAP asking for regression and the
        test environment file missing or incomplete."""
        return bool(list_test_environment_problems(project_dir))

    @staticmethod
    def validate_conformance(value):
        text = str(value or "").strip().upper()
        if text not in CONFORMANCE_VALUES:
            raise InvalidPathError("conformance must be STANDARD or LEGACY")
        return text

    @staticmethod
    def list_structure_folders(project_dir):
        specs_dir = os.path.join(project_dir, ".specs")
        return [os.path.join(specs_dir, name) for name in STRUCTURE_FOLDERS] + [
            os.path.dirname(loop_state.loop_path(project_dir, "LOOP.json")),
            os.path.dirname(loop_state.loop_path(project_dir, "LOOP.lock"))]

    def build_template_files(self, project_dir, project_name):
        project_specs_dir = os.path.join(project_dir, ".specs", "project")
        today = datetime.date.today().isoformat()
        files = {}
        for name in PROJECT_TEMPLATE_FILES:
            template = self.read_template(os.path.join(SPECS_TEMPLATES_DIR_NAME, name))
            files[os.path.join(project_specs_dir, name)] = template.replace(PROJECT_NAME_PLACEHOLDER, project_name).replace(TODAY_PLACEHOLDER, today)
        rules_block = extract_markdown_block(self.read_template(RULES_TEMPLATE_NAME))
        files[os.path.join(project_specs_dir, RULES_FILE_NAME)] = build_rules_without_examples(rules_block) if rules_block else MINIMAL_RULES
        return files

    def read_template(self, relative):
        text = self.specs_file_repository.read_text(os.path.join(self.templates_dir, relative))
        if text is None:
            self.log.warn("specs_template_missing", template=relative)
            return ""
        return text

    @staticmethod
    def relative_to_project(project_dir, path):
        return os.path.relpath(path, project_dir).replace("\\", "/")
