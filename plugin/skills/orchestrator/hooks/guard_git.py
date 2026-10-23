"""Shell command analysis of the orchestrator guard (imported by guard.py; standard library only).

`shell_events` lexes a Bash/PowerShell command text left to right (quotes, line continuations, `&&` `||` `;` `|` `&`, `(` `)`, `$(`, backticks, redirections)
into the commands it would run, each as a list of unquoted words, with `open`/`close` events around sub-shells so a `cd` can be scoped. A quoted word
that holds spaces is lexed again (`bash -c "cd x && git push"`). On top of that, `git_calls` and `gh_calls` find a git or gh invocation ANYWHERE in a
command, not only at its start: after `VAR=x`, `env`, `command`, `sudo`, `time`, `xargs`, `&`, a subshell, `$(...)` or a backtick. Git's global options
(`-C <path>`, `-c k=v`, `--git-dir=...`, `--no-pager`...) are skipped generically to reach the subcommand, and `-c alias.x=push` counts as a push.
It is text analysis, not a shell: a command built from a variable or a script that runs git itself is not seen (the README lists these residual risks).
"""
import os
import re

ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*\+?=")
# words after which another command (or the same command's argument that is itself a command) follows: anything later in the segment may be the program
WRAPPERS = {"env", "command", "exec", "sudo", "doas", "time", "nohup", "nice", "ionice", "setsid", "stdbuf", "timeout", "xargs", "builtin", "watch", "unbuffer",
            "start", "call", "cmd", "bash", "sh", "zsh", "dash", "pwsh", "powershell", "wsl", "start-process", "saps", "invoke-expression", "iex",
            "invoke-command", "icm", "&", "!", "if", "then", "else", "elif", "do", "while", "until", "{", "}", "foreach-object", "%", "foreach"}
_ESCAPABLE = " \t\"'&;|()<>$`"
_MAX_DEPTH = 3
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source", "--exec-path"}
# `git -c <key>=...` keys that make a read-only subcommand run a program or change what is run
DANGEROUS_CONFIG_RE = re.compile(r"^(?:core\.(?:fsmonitor|pager|editor|sshcommand|hookspath|askpass|gitproxy)|credential\.helper|diff\.external|gpg\.program|sequence\.editor|uploadpack\.packobjectshook)$", re.I)
PUSH_OPTIONS_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec", "--recurse-submodules"}


def executable_name(word):
    """`C:\\Program Files\\Git\\bin\\git.exe` -> `git`, `\\rm` -> `rm`, `/usr/bin/gh` -> `gh` (lowercase, no .exe/.cmd/.bat, no quotes)."""
    name = re.split(r"[\\/]", str(word).strip("\"'"))[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat|com)$", "", name)


class _Lexer:
    """Single pass over a command text. `events` receives ("cmd", words), ("open",), ("close",)."""

    def __init__(self, text, depth, events, powershell=False):
        self.text, self.depth, self.events, self.powershell = text, depth, events, powershell
        self.words, self.nested = [], []
        self.buffer, self.in_word, self.quoted, self.skip_word = [], False, False, False

    def finish_word(self):
        if not self.in_word:
            return
        word = "".join(self.buffer)
        quoted = self.quoted
        self.buffer, self.in_word, self.quoted = [], False, False
        if self.skip_word:  # the target of a redirection, not an argument
            self.skip_word = False
            return
        self.words.append(word)
        if quoted and self.depth < _MAX_DEPTH and re.search(r"\s", word.strip()):
            self.nested.append(word)  # a quoted word with spaces may be a command line (`bash -c "..."`, `python -c "..."`)

    def end_command(self):
        self.finish_word()
        if self.words:
            self.events.append(("cmd", self.words))
        self.words = []
        for word in self.nested:
            self.events.append(("open",))
            _Lexer(word, self.depth + 1, self.events, self.powershell).run()
            self.events.append(("close",))
        self.nested = []

    def continuation(self, index):
        """Length of the newline pair (`\\` or a backtick, then LF or CRLF) that starts at `index`, 0 when it is not one."""
        following = self.text[index + 1:index + 3]
        return 2 if following[:1] == "\n" else (3 if following == "\r\n" else 0)

    def run(self):
        text, size, index = self.text, len(self.text), 0
        while index < size:
            char = text[index]
            skipped = self.continuation(index) if char in "\\`" else 0
            if skipped:
                index += skipped
            elif char == "'":
                end = text.find("'", index + 1)
                end = size if end < 0 else end
                self.buffer.append(text[index + 1:end])
                self.in_word = self.quoted = True
                index = end + 1
            elif char == '"':
                self.in_word = self.quoted = True
                index += 1
                chunk = []
                while index < size and text[index] != '"':
                    pair = self.continuation(index) if text[index] == "\\" else 0
                    if pair:
                        index += pair
                    elif text[index] == "\\" and index + 1 < size and text[index + 1] in '"\\$`' and not self.powershell:
                        chunk.append(text[index + 1])
                        index += 2
                    else:
                        chunk.append(text[index])
                        index += 1
                self.buffer.append("".join(chunk))
                index += 1
            elif char == "\\" and index + 1 < size and text[index + 1] in _ESCAPABLE:
                self.buffer.append(text[index + 1])
                self.in_word = True
                index += 2
            elif char in " \t\r":
                self.finish_word()
                index += 1
            elif char in "\n;`" or text.startswith(("&&", "||"), index):
                self.end_command()
                index += 2 if text.startswith(("&&", "||"), index) else 1
            elif char in "<>" or (char == "&" and text[index + 1:index + 2] == ">"):
                if self.in_word and "".join(self.buffer).isdigit():
                    self.buffer, self.in_word, self.quoted = [], False, False  # the file descriptor of `2>`
                else:
                    self.finish_word()
                while index < size and text[index] in "<>&|":
                    if text[index] == "|" and text[index - 1] != ">":
                        break
                    index += 1
                self.skip_word = True
            elif char in "|&":
                self.end_command()
                index += 1
            elif char == "(":
                joined = "".join(self.buffer)
                if joined.endswith("$"):
                    self.buffer, self.in_word = [joined[:-1]], len(joined) > 1
                self.skip_word = False
                self.end_command()
                self.events.append(("open",))
                index += 1
            elif char == ")":
                self.end_command()
                self.events.append(("close",))
                index += 1
            elif char in "{}" and not self.in_word and (index + 1 >= size or text[index + 1] in " \t\r\n"):
                self.end_command()
                index += 1
            else:
                self.buffer.append(char)
                self.in_word = True
                index += 1
        self.end_command()


def shell_events(command, powershell=False):
    """Events of a command text: ("cmd", [words]) in execution order, ("open",) / ("close",) around sub-shells and quoted command lines. With `powershell` a
    backslash inside double quotes is not an escape (`dir "C:\\x\\" ; git push` ends the string at the quote, as PowerShell reads it)."""
    events = []
    _Lexer(str(command), 0, events, powershell).run()
    return events


def shell_event_variants(command):
    """The event lists to check: the Bash reading, and the PowerShell reading too when the text has a backslash-quote (the one place the two readings differ in
    a way that can hide a command: Bash keeps the string open and swallows what follows)."""
    text = str(command)
    return [shell_events(text)] + ([shell_events(text, True)] if '\\"' in text else [])


def command_starts(words):
    """Indexes in `words` where a program may start: the first word after any `VAR=x` assignments and, when that word is a wrapper
    (`env`, `command`, `sudo`, `xargs`, `bash`, `&`...), every later word (its arguments may be the program)."""
    index = 0
    while index < len(words) and ASSIGNMENT_RE.match(words[index]):
        index += 1
    if index >= len(words):
        return []
    starts = [index]
    if executable_name(words[index]) in WRAPPERS or words[index] in WRAPPERS:
        starts.extend(range(index + 1, len(words)))
    return starts


class GitCall:
    """One `git ...` invocation: subcommand, its arguments, the `-C`/`--git-dir`/`--work-tree` it carries (and GIT_DIR/GIT_WORK_TREE of the line),
    `-c` values, and whether an alias turns it into a push."""

    def __init__(self, sub, args, dirs_c, extra_dirs, config, alias_push, raw_sub):
        self.sub, self.args, self.dirs_c, self.extra_dirs, self.config = sub, args, dirs_c, extra_dirs, config
        self.alias_push, self.raw_sub = alias_push, raw_sub

    @property
    def is_push(self):
        return self.sub == "push" or self.alias_push

    def push_facts(self):
        """(force, mirror_or_all, positional) of a push's arguments."""
        force = mirror_all = False
        positional, skip = [], False
        for arg in self.args:
            if skip:
                skip = False
                continue
            if arg in PUSH_OPTIONS_WITH_VALUE:
                skip = True
            elif arg.startswith("-"):
                if arg == "--force" or arg.startswith("--force-with-lease") or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", arg):
                    force = True
                elif arg in ("--mirror", "--all") or arg.startswith(("--mirror=", "--all=")):
                    mirror_all = True
            elif arg.startswith("+") and len(arg) > 1:
                force = True
                positional.append(arg)
            else:
                positional.append(arg)
        return force, mirror_all, positional


def parse_git(words, start, assignments=()):
    """GitCall for the git invocation whose program word is `words[start]`, else None. Global options are skipped generically."""
    index = start + 1
    dirs_c, extra_dirs, config = [], [], {}
    for assignment in assignments:
        name, _, value = assignment.partition("=")
        if name.upper() in ("GIT_DIR", "GIT_WORK_TREE") and value:
            extra_dirs.append(value)
    while index < len(words) and words[index].startswith("-"):
        token = words[index]
        has_value = index + 1 < len(words)
        if token == "-C" and has_value:
            dirs_c.append(words[index + 1])
            index += 2
        elif token == "-c" and has_value:
            key, _, value = words[index + 1].partition("=")
            config[key.lower()] = value
            index += 2
        elif token in GIT_OPTIONS_WITH_VALUE and has_value:
            if token in ("--git-dir", "--work-tree"):
                extra_dirs.append(words[index + 1])
            index += 2
        else:
            inline = re.match(r"^--(?:git-dir|work-tree)=(.+)$", token)
            if inline:
                extra_dirs.append(inline.group(1))
            index += 1
    if index >= len(words):
        return None
    if re.search(r"\s", words[index].strip()):  # `Start-Process git -ArgumentList "push origin main"`: the subcommand and its arguments came as one word
        return parse_git(words[:index] + words[index].split() + words[index + 1:], start, assignments)
    raw_sub = words[index]
    sub, args, alias_push = raw_sub.lower(), words[index + 1:], False
    alias_value = config.get("alias." + sub)
    if alias_value is not None:
        stripped = alias_value.strip()
        first = stripped.split(None, 1)[0].lower() if stripped.split() else ""
        if stripped.startswith("!") or first == "push":
            alias_push = True
        sub = first or sub
    if sub == "config" and any(re.match(r"^alias\.", arg, re.I) for arg in args):
        value = " ".join(arg for arg in args if not re.match(r"^alias\.", arg, re.I) and not arg.startswith("-")).strip()
        if value.startswith("!") or value.lower().split(None, 1)[:1] == ["push"] or "push" in value.lower().split():
            alias_push = True
    if sub != "push" and any(DANGEROUS_CONFIG_RE.match(key) for key in config):
        sub = "config-exec"  # a -c key that runs a program: not a read, whatever the subcommand
    return GitCall(sub, args, dirs_c, extra_dirs, config, alias_push, raw_sub)


def git_calls_in(words):
    """GitCall for every git invocation in one command's words (after assignments and wrappers)."""
    calls = []
    for start in command_starts(words):
        if executable_name(words[start]) == "git":
            assignments = [word for word in words[:start] if ASSIGNMENT_RE.match(word)]
            call = parse_git(words, start, assignments)
            if call:
                calls.append(call)
    return calls


class GhCall:
    def __init__(self, sub, sub2, args):
        self.sub, self.sub2, self.args = sub, sub2, args


GH_OPTIONS_WITH_VALUE = {"-R", "--repo", "--hostname", "-H", "--header"}


def gh_calls_in(words):
    """GhCall for every gh invocation (`gh`, `gh.exe`, full path) in one command's words; flags before the subcommand are skipped."""
    calls = []
    for start in command_starts(words):
        if executable_name(words[start]) != "gh":
            continue
        index, positional = start + 1, []
        while index < len(words) and len(positional) < 2:
            word = words[index]
            if word.startswith("-"):
                index += 2 if word in GH_OPTIONS_WITH_VALUE else 1
                continue
            positional.append(word.lower())
            index += 1
        calls.append(GhCall(positional[0] if positional else "", positional[1] if len(positional) > 1 else "", words[start + 1:]))
    return calls


def gh_api_writes(args):
    """True when `gh api ...` is not a plain read: an explicit method other than GET, else any field/input flag (gh then POSTs)."""
    method, fields = None, False
    for position, arg in enumerate(args):
        lowered = arg.lower()
        if lowered in ("-x", "--method") and position + 1 < len(args):
            method = args[position + 1].upper()
        elif lowered.startswith("--method="):
            method = arg.split("=", 1)[1].upper()
        elif re.match(r"^-X\w+$", arg):
            method = arg[2:].upper()
        elif arg in ("-f", "-F", "--field", "--raw-field", "--input") or re.match(r"^--(?:field|raw-field|input)=", arg) or re.match(r"^-[fF].+", arg):
            fields = True
    return (method != "GET") if method else fields


def quoted_ranges(text):
    """(start, end) of every CLOSED quoted string: a `>` inside one is text, not a redirect (`python -c "print('a > b')"`)."""
    ranges, quote, start = [], None, 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                ranges.append((start, index))
                quote = None
        elif char in "\"'":
            quote, start = char, index
    return ranges
