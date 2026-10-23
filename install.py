#!/usr/bin/env python3
"""One-command setup of loop-engineering-harness (standard library only; Windows, macOS and Linux).

    python install.py            # asks once before writing anything
    python install.py --yes      # no prompt (CI, scripts)

What it does, in order (each step prints PASS, WARN or FAIL):
  1. checks Python 3.10+, `git` and `claude` on the PATH, and that `claude --version` answers; `gh` is optional
     (only the final pull request needs it);
  2. creates the app data folder (~/.loop-harness, or LOOP_DATA_DIR / --data-dir) and copies app/.env.example to
     <data folder>/.env ONLY when that file does not exist yet (yours is never overwritten);
  3. runs `python app/harness.py install` (writes <data folder>/config.json so the skill finds the app) and
     `python app/harness.py doctor`;
  4. looks for the claude-dev-toolkit plugin (the generic dev skills the loop requires; WARN when it is not found);
  5. prints the `/plugin` commands that install Maestro and the toolkit in Claude Code and how to open the panel.

It never installs anything system-wide, never touches ~/.claude and never asks for or stores a token: the harness drives
the `claude` CLI you are already signed in to. Exit code: 0 when every hard check passed, 1 otherwise, 2 for bad usage.
"""
import argparse
import os
import shutil
import subprocess
import sys

MIN_PYTHON = (3, 10)
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(REPO_DIR, "app")
HARNESS = os.path.join(APP_DIR, "harness.py")
ENV_EXAMPLE = os.path.join(APP_DIR, ".env.example")
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
DEFAULT_DATA_DIR_NAME = ".loop-harness"
VERSION_TIMEOUT_SECONDS = 60
STEP_TIMEOUT_SECONDS = 300
PLUGIN_COMMANDS = (
    "/plugin marketplace add iagopadilha/loop-engineering-harness",
    "/plugin install loop-engineering-harness@loop-engineering-harness",
)
TOOLKIT_COMMANDS = (
    "/plugin marketplace add iagopadilha/claude-dev-toolkit",
    "/plugin install claude-dev-toolkit@claude-dev-toolkit",
)
EXIT_OK, EXIT_FAILED = 0, 1


def find_toolkit_root():
    """Plugin root of claude-dev-toolkit (config.json `dev_toolkit_dir`, the plugin cache, the sibling clone) or '' when not found."""
    backend = os.path.join(APP_DIR, "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    try:
        from harness_backend.config import skill_locator
        return skill_locator.find_toolkit_root()
    except Exception:  # noqa: BLE001  (a broken import only means "not found" here; the doctor reports the details)
        return ""


def print_now(line):
    """print + flush, so our lines and the output of the harness.py steps come out in order even when piped."""
    print(line, flush=True)


class Installer:
    """The steps, with the outside world injected (`which`, `run`, `ask`, `out`) so the tests can mock every check."""

    def __init__(self, assume_yes=False, data_dir="", which=shutil.which, run=subprocess.run, ask=input, out=None,
                 python_version=None, environ=None, find_toolkit=None):
        self.assume_yes = assume_yes
        self.which = which
        self.run = run
        self.ask = ask
        self.out = out or print_now
        self.python_version = tuple(python_version or sys.version_info[:3])
        self.environ = os.environ if environ is None else environ
        self.find_toolkit = find_toolkit or find_toolkit_root
        self.data_dir = os.path.abspath(os.path.expanduser(
            data_dir or self.environ.get(DATA_DIR_VARIABLE) or os.path.join("~", DEFAULT_DATA_DIR_NAME)))
        self.failures = []

    # ---- output
    def report(self, status, message):
        self.out("%-4s  %s" % (status, message))
        if status == "FAIL":
            self.failures.append(message)

    # ---- checks
    def check_python(self):
        version = ".".join(str(part) for part in self.python_version)
        if self.python_version[:2] >= MIN_PYTHON:
            self.report("PASS", "Python %s" % version)
            return True
        self.report("FAIL", "Python %s found, %d.%d or newer is required (run this script with a newer python)" % ((version,) + MIN_PYTHON))
        return False

    def version_of(self, binary):
        """First line of `<binary> --version`, or '' when it does not answer."""
        try:
            done = self.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return ""
        lines = [line.strip() for line in ((done.stdout or "") + "\n" + (done.stderr or "")).splitlines() if line.strip()]
        return lines[0] if done.returncode == 0 and lines else ""

    def check_tool(self, name, required, hint):
        binary = self.which(name)
        if not binary:
            self.report("FAIL" if required else "WARN", "`%s` not found on the PATH: %s" % (name, hint))
            return False
        version = self.version_of(binary)
        if not version:
            self.report("FAIL" if required else "WARN", "`%s --version` did not answer (%s): %s" % (name, binary, hint))
            return False
        self.report("PASS", "%s: %s" % (name, version))
        return True

    def run_checks(self):
        results = [
            self.check_python(),
            self.check_tool("git", True, "install Git"),
            self.check_tool("claude", True, "install Claude Code and sign in once (run `claude` in a terminal)"),
        ]
        self.check_tool("gh", False, "optional, only the final pull request needs the GitHub CLI")
        return all(results)

    # ---- setup
    def confirm(self):
        if self.assume_yes:
            return True
        question = ("Create %s, copy .env.example there if it is missing and register the app (config.json)? [Y/n] "
                    % self.data_dir)
        try:
            answer = self.ask(question)
        except EOFError:
            answer = ""
        return not answer.strip().lower().startswith("n")

    def prepare_data_dir(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as error:
            self.report("FAIL", "could not create %s: %s" % (self.data_dir, error))
            return False
        self.report("PASS", "data folder %s" % self.data_dir)
        target = os.path.join(self.data_dir, ".env")
        if os.path.exists(target):
            self.report("PASS", ".env already exists, left untouched (%s)" % target)
            return True
        if not os.path.isfile(ENV_EXAMPLE):
            self.report("WARN", "app/.env.example not found: no .env copied (notifications stay on console, toast and file)")
            return True
        try:
            shutil.copyfile(ENV_EXAMPLE, target)
        except OSError as error:
            self.report("FAIL", "could not copy .env.example to %s: %s" % (target, error))
            return False
        self.report("PASS", "copied app/.env.example to %s (fill in the LOOP_* values you need; never commit it)" % target)
        return True

    def harness(self, command, extra=()):
        """`python app/harness.py <command> --data-dir <data folder>`, output shown as it runs. Returns the exit code."""
        argv = [sys.executable, HARNESS, command, "--data-dir", self.data_dir] + list(extra)
        self.out("----  python app/harness.py %s" % command)
        try:
            return self.run(argv, cwd=APP_DIR, timeout=STEP_TIMEOUT_SECONDS).returncode
        except (OSError, subprocess.SubprocessError) as error:
            self.out(str(error))
            return EXIT_FAILED

    def register_app(self, toolkit_found=True):
        if self.harness("install") != 0:
            self.report("FAIL", "harness.py install failed (see above)")
            return False
        self.report("PASS", "harness.py install")
        # the toolkit is installed from inside Claude Code AFTER this script: a missing one is only a warning here
        if self.harness("doctor", () if toolkit_found else ("--toolkit-optional",)) != 0:
            self.report("FAIL", "harness.py doctor reported a problem (see the PROBLEM lines above)")
            return False
        self.report("PASS", "harness.py doctor")
        return True

    def check_toolkit(self):
        """The loop requires claude-dev-toolkit (backend-dev, frontend-dev, tlc-spec-driven...): WARN, not FAIL, because it is installed
        from inside Claude Code after this script."""
        root = self.find_toolkit()
        if root:
            self.report("PASS", "claude-dev-toolkit found: %s" % root)
            return True
        self.report("WARN", "claude-dev-toolkit not found: the loop needs it (install it with the /plugin lines below)")
        return False

    def print_next_steps(self):
        self.out("")
        self.out("Next steps")
        self.out("  1. In Claude Code, install the plugin:")
        for command in PLUGIN_COMMANDS:
            self.out("       " + command)
        self.out("     (or, from this clone: /plugin marketplace add %s)" % REPO_DIR)
        self.out("  2. Then the dev toolkit the loop requires (generic dev skills):")
        for command in TOOLKIT_COMMANDS:
            self.out("       " + command)
        self.out("  3. Open the panel:  python app/harness.py serve")
        self.out("  4. Optional notifications: edit %s, then python app/harness.py notify --test"
                 % os.path.join(self.data_dir, ".env"))

    def main(self):
        self.out("loop-engineering-harness setup")
        if not self.run_checks():
            self.out("")
            self.out("FAILED: fix the FAIL lines above and run `python install.py` again. Nothing was written.")
            return EXIT_FAILED
        if not self.confirm():
            self.out("Cancelled: nothing was written.")
            return EXIT_FAILED
        if self.prepare_data_dir():
            self.register_app(self.check_toolkit())
        if self.failures:
            self.out("")
            self.out("FAILED (%d): %s" % (len(self.failures), "; ".join(self.failures)))
            return EXIT_FAILED
        self.print_next_steps()
        self.out("")
        self.out("ALL PASSED")
        return EXIT_OK


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog="install.py", description="One-command setup of loop-engineering-harness (no system-wide install, no token).")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask before writing the data folder")
    parser.add_argument("--data-dir", default="", help="app data folder (default: LOOP_DATA_DIR or ~/.loop-harness)")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_arguments(sys.argv[1:] if argv is None else argv)
    return Installer(assume_yes=options.yes, data_dir=options.data_dir).main()


if __name__ == "__main__":
    sys.exit(main())
