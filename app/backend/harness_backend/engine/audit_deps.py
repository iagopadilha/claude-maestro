#!/usr/bin/env python3
"""Audits forgotten dependencies between ROADMAP features (skill orchestrator-audit-dependencies). Stdlib only, no AI.

Usage:
  python audit_deps.py <root> [--write] [--apply] [--json]

Looks for evidence that feature B needs feature A while B does not declare `deps=A`:
  1) IMPORT (code that already exists): imports/using/ProjectReference of files in B.paths that fall inside A.paths
     (TypeScript/JavaScript: relative and tsconfig alias; Python: the repo's own module; C#: using -> namespace -> folder, ProjectReference).
  2) TEXT (code that does not exist yet): B's spec (.specs/features/B/*.md) mentions A's slug or a path of A, or lists A in the Dependencies section.
  3) REVERSE: a declared `deps=A` with no evidence at all -> "review" (may be a legitimate ordering dependency; nothing is removed).
Only suggests an A that is not yet `verified` (if A is already done there is nothing to wait for). A suggestion that would close a cycle becomes `cycle` (not applied).

--write    writes .specs/project/loop/DEPENDENCIES.md (`[pending]` suggestions, `[applied]`/`[refused]` decisions preserved)
--apply    writes `deps=` into ROADMAP.md for the `[pending]` suggestions with evidence (validates the grammar; undoes if it breaks) and marks `[applied]`
--json     prints the result as JSON
Never decides on its own in interactive mode: the orchestrator shows the suggestions to the user; in the unsupervised loop it applies them
(erring on the serial side only costs time; erring on the parallel side corrupts code).
Limit: a dependency on code that does not exist yet and that the spec does not mention stays invisible.
"""
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loop_state  # noqa: E402

SKIP = {"node_modules", "bin", "obj", ".git", ".specs", "dist", "build", ".next", ".vs", "packages", "__pycache__", "coverage"}
EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".cs", ".csproj"}
MAX_FILES = 400
MARK = re.compile(r"^- \[(pending|applied|refused|cycle|review)\] (\S+) -> (\S+) \|")
JS_IMPORT = re.compile(r"""(?:\bimport\s[^'";]*?from\s*|\bimport\s*|\brequire\(\s*|\bexport\s[^'";]*?from\s*)['"]([^'"]+)['"]""")
PY_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\b")
PY_IMPORT = re.compile(r"^\s*import\s+([\w., ]+)")
CS_USING = re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;")
CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)")
CS_PROJREF = re.compile(r"""<ProjectReference\s+Include\s*=\s*["']([^"']+)["']""", re.I)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def norm(path):
    return os.path.normpath(path).replace("\\", "/").strip("/")


def feature_paths(feature):
    return [norm(p) for p in (feature.get("paths") or "").split(",") if p.strip()]


def inside(target, folder):
    target, folder = target.lower(), folder.lower()
    return target == folder or target.startswith(folder + "/")


def source_files(root, paths):
    found = []
    for rel in paths:
        absolute = os.path.join(root, rel)
        if os.path.isfile(absolute):
            found.append(rel)
            continue
        for folder, dirs, names in os.walk(absolute):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for name in names:
                if os.path.splitext(name)[1].lower() in EXTS:
                    found.append(norm(os.path.relpath(os.path.join(folder, name), root)))
                    if len(found) >= MAX_FILES:
                        return found
    return found


# ---------------------------------------------------------------- import resolution
class Resolver:
    def __init__(self, root):
        self.root = root
        self._namespaces = None
        self._tsconfigs = {}

    def namespaces(self):
        """C# namespace -> folders (repo-relative) that declare it."""
        if self._namespaces is None:
            self._namespaces = {}
            count = 0
            for folder, dirs, names in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in SKIP]
                for name in names:
                    if name.endswith(".cs"):
                        count += 1
                        if count > 6000:
                            return self._namespaces
                        for line in read(os.path.join(folder, name)).splitlines()[:40]:
                            match = CS_NAMESPACE.match(line)
                            if match:
                                self._namespaces.setdefault(match.group(1), set()).add(norm(os.path.relpath(folder, self.root)))
                                break
        return self._namespaces

    def tsconfig(self, directory):
        """Alias map of the nearest tsconfig above `directory`: [(prefix, [repo_relative_folder])]."""
        here = directory
        while True:
            if here in self._tsconfigs:
                return self._tsconfigs[here]
            candidate = os.path.join(self.root, here, "tsconfig.json")
            if os.path.isfile(candidate):
                text = re.sub(r"/\*.*?\*/|(?<!:)//[^\n]*", "", read(candidate), flags=re.S)
                text = re.sub(r",(\s*[}\]])", r"\1", text)
                aliases = []
                try:
                    options = json.loads(text).get("compilerOptions", {})
                    base = norm(os.path.join(here, options.get("baseUrl", ".")))
                    for pattern, targets in (options.get("paths") or {}).items():
                        aliases.append((pattern.rstrip("*"), [norm(os.path.join(base, t.rstrip("*"))) for t in targets]))
                except (ValueError, AttributeError):
                    pass
                self._tsconfigs[here] = aliases
                return aliases
            if not here or here == ".":
                return []
            here = norm(os.path.dirname(here)) if os.path.dirname(here) else ""

    def imports(self, rel):
        """[(repo-relative target, line, text)] of the file's imports that point inside the repo."""
        out = []
        extension = os.path.splitext(rel)[1].lower()
        directory = norm(os.path.dirname(rel))
        text = read(os.path.join(self.root, rel))
        if extension == ".csproj":
            for number, line in enumerate(text.splitlines(), 1):
                match = CS_PROJREF.search(line)
                if match:
                    target = norm(os.path.join(directory, os.path.dirname(match.group(1).replace("\\", "/"))))
                    out.append((target, number, "ProjectReference " + match.group(1)))
            return out
        for number, line in enumerate(text.splitlines(), 1):
            if extension in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
                for match in JS_IMPORT.finditer(line):
                    spec = match.group(1)
                    if spec.startswith("."):
                        out.append((norm(os.path.join(directory, spec)), number, line.strip()[:100]))
                    else:
                        for prefix, targets in self.tsconfig(directory):
                            if prefix and spec.startswith(prefix):
                                out += [(norm(os.path.join(t, spec[len(prefix):])), number, line.strip()[:100]) for t in targets]
                                break
            elif extension == ".py":
                match = PY_FROM.match(line)
                modules = [match.group(1)] if match else []
                match = PY_IMPORT.match(line)
                if match:
                    modules += [m.strip().split(" as ")[0] for m in match.group(1).split(",")]
                for module in modules:
                    dots = len(module) - len(module.lstrip("."))
                    parts = module.lstrip(".").split(".") if module.strip(".") else []
                    base = directory
                    for _ in range(max(dots - 1, 0)):
                        base = norm(os.path.dirname(base))
                    for origin in ([base] if dots else ["", directory]):
                        candidate = norm(os.path.join(origin, *parts)) if parts else base
                        if os.path.isdir(os.path.join(self.root, candidate)) or os.path.isfile(os.path.join(self.root, candidate + ".py")):
                            out.append((candidate, number, line.strip()[:100]))
                            break
            elif extension == ".cs":
                match = CS_USING.match(line)
                if match:
                    name = match.group(1)
                    for folder in self.namespaces().get(name, ()):
                        out.append((folder, number, line.strip()[:100]))
        return out


# ---------------------------------------------------------------- spec text
def spec_text(root, slug):
    """[(file, line, text)] of the feature's specs; only spec/design/tasks."""
    lines = []
    for name in ("spec.md", "design.md", "tasks.md"):
        path = os.path.join(root, ".specs", "features", slug, name)
        for number, line in enumerate(read(path).splitlines(), 1):
            lines.append((".specs/features/%s/%s" % (slug, name), number, line))
    return lines


def mentions(text_lines, slug, paths):
    found = []
    slug_re = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(slug), re.I) if (len(slug) >= 4 or "-" in slug) else re.compile(r"[`\[]%s[`\]]" % re.escape(slug), re.I)
    section = ""
    for file, number, line in text_lines:
        heading = re.match(r"^#{1,6}\s*(.+)$", line)
        if heading:
            section = heading.group(1).strip().lower()
        hit = slug_re.search(line) or any(p.lower() in line.lower() for p in paths if p.count("/") >= 1)
        if hit and "out of scope" not in section:
            found.append("%s:%d %s" % (file, number, line.strip()[:100]))
    return found


# ---------------------------------------------------------------- audit
def audit(root):
    root = os.path.abspath(root)
    features, errors = loop_state.parse_roadmap(read(os.path.join(root, ".specs", "project", "ROADMAP.md")))
    resolver = Resolver(root)
    suggestions, review = {}, []
    for b, fb in features.items():
        if fb["status"] in ("verified", "blocked"):
            continue  # nothing else will run: a new dependency changes nothing
        declared, folders = loop_state.split_deps(fb)
        own = feature_paths(fb)
        evidence = {}  # a -> {"import": [...], "text": [...]}
        for rel in source_files(root, own):
            for target, number, snippet in resolver.imports(rel):
                if any(inside(target, mine) for mine in own):
                    continue
                for a, fa in features.items():
                    if a == b or fa["status"] == "verified":
                        continue
                    if any(inside(target, theirs) or (inside(theirs, target) and target.count("/") >= 1) for theirs in feature_paths(fa)):
                        evidence.setdefault(a, {}).setdefault("import", []).append("%s:%d %s" % (rel, number, snippet))
        lines = spec_text(root, b)
        for a, fa in features.items():
            if a == b or fa["status"] == "verified":
                continue
            found = mentions(lines, a, feature_paths(fa))
            if found:
                evidence.setdefault(a, {}).setdefault("text", []).extend(found)
        for a, kinds in evidence.items():
            if a in declared:
                continue
            trial = {slug: dict(f) for slug, f in features.items()}
            trial[b]["deps"] = ",".join(filter(None, [fb.get("deps", ""), a]))
            cycle = loop_state.find_cycle(trial)
            suggestions[(b, a)] = {
                "feature": b, "depends_on": a, "type": "+".join(k for k in ("import", "text") if k in kinds),
                "evidence": (kinds.get("import", []) + kinds.get("text", []))[:3], "cycle": cycle,
            }
        for a in declared:
            if a in features and a not in evidence and features[a]["status"] != "verified":
                review.append({"feature": b, "depends_on": a})
    return {"features": features, "suggestions": list(suggestions.values()), "review": review, "roadmap_errors": errors}


# ---------------------------------------------------------------- DEPENDENCIES.md (annotation in .specs)
def load_marks(path):
    marks = {}
    for line in read(path).splitlines():
        match = MARK.match(line.strip())
        if match:
            marks[(match.group(2), match.group(3))] = (match.group(1), line.strip())
    return marks


def render(result, previous):
    today = datetime.date.today().isoformat()
    out = ["# Dependencies between features (audit)", "",
           "> Generated by `audit_deps.py` on %s. A suggestion NEVER enters the ROADMAP by itself: the orchestrator shows it to the user (or, in the unsupervised loop, applies it) and swaps `[pending]` for `[applied]` or `[refused]`." % today,
           "> Evidence `import` = existing code; `text` = the spec mentions it. Erring serial costs time; erring parallel corrupts code.", "", "## Suggestions", ""]
    kept = {}
    for (b, a), (mark, line) in previous.items():
        if mark in ("applied", "refused"):
            kept[(b, a)] = line
    seen = set()
    for item in result["suggestions"]:
        key = (item["feature"], item["depends_on"])
        seen.add(key)
        if key in kept:
            out.append(kept[key])
            continue
        mark = "cycle" if item["cycle"] else "pending"
        note = (" would close a cycle: " + " -> ".join(item["cycle"])) if item["cycle"] else (" deps=%s" % item["depends_on"])
        out.append("- [%s] %s -> %s | %s |%s | %s" % (mark, item["feature"], item["depends_on"], item["type"], note, " ; ".join(item["evidence"]).replace("\n", " ")))
    for key, line in kept.items():
        if key not in seen:
            out.append(line)
    out += ["", "## Review (deps declared without evidence)", "", "May be a legitimate ordering dependency (A creates data/a route that B uses). Nothing is removed on its own.", ""]
    out += ["- [review] %s -> %s | no import nor mention in the spec" % (r["feature"], r["depends_on"]) for r in result["review"]] or ["- (none)"]
    return "\n".join(out) + "\n"


def apply_to_roadmap(root, result, marks):
    """Writes deps= into the ROADMAP for pending suggestions without a cycle. Validates the grammar; undoes if it breaks. Returns the applied [(b, a)]."""
    path = os.path.join(root, ".specs", "project", "ROADMAP.md")
    original = read(path)
    text, applied = original, []
    for item in result["suggestions"]:
        key = (item["feature"], item["depends_on"])
        if item["cycle"] or marks.get(key, ("pending",))[0] in ("refused", "applied"):
            continue
        b, a = key
        pattern = re.compile(r"^(- \[%s\]\s.*)$" % re.escape(b), re.M)
        match = pattern.search(text)
        if not match:
            continue
        line = match.group(1)
        if re.search(r"\bdeps=\S+", line):
            new = re.sub(r"\bdeps=(\S+)", lambda m: "deps=%s,%s" % (m.group(1), a), line, count=1)
        else:
            new = line.rstrip() + " deps=" + a
        text = text.replace(line, new, 1)
        applied.append(key)
    if applied:
        features, errors = loop_state.parse_roadmap(text)
        errors += loop_state.check_fields(features)
        if errors:
            return [], errors
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return applied, []


def mark_applied(markdown, applied):
    for b, a in applied:
        markdown = re.sub(r"^- \[pending\] %s -> %s \|" % (re.escape(b), re.escape(a)), "- [applied] %s -> %s |" % (b, a), markdown, flags=re.M)
    return markdown


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    root = os.path.abspath(args[0])
    result = audit(root)
    target = loop_state.state_file(root, "DEPENDENCIES.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    previous = load_marks(target)
    for key, (mark, _) in list(previous.items()):
        if mark == "refused":  # user decision: do not suggest again
            result["suggestions"] = [s for s in result["suggestions"] if (s["feature"], s["depends_on"]) != key]
    applied = []
    if "--apply" in args:
        applied, problems = apply_to_roadmap(root, result, previous)
        for item in problems:
            print("PROBLEM while applying (ROADMAP untouched):", item)
        for b, a in applied:
            print("APPLIED: %s deps+=%s" % (b, a))
    if "--write" in args or "--apply" in args:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        markdown = mark_applied(render(result, previous), applied)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        print("written:", target)
    if "--json" in args:
        print(json.dumps({k: v for k, v in result.items() if k != "features"}, ensure_ascii=False, indent=2))
    else:
        pending = [s for s in result["suggestions"] if not s["cycle"] and (s["feature"], s["depends_on"]) not in applied]
        print("%d deps suggestion(s), %d to review" % (len(pending), len(result["review"])))
        for item in result["suggestions"]:
            flag = "CYCLE" if item["cycle"] else ("applied" if (item["feature"], item["depends_on"]) in applied else "pending")
            print("  [%s] %s -> %s (%s)" % (flag, item["feature"], item["depends_on"], item["type"]))
            for line in item["evidence"]:
                print("       " + line)
        for item in result["review"]:
            print("  [review] %s -> %s no evidence" % (item["feature"], item["depends_on"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
