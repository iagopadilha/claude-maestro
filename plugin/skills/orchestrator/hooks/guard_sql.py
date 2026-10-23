"""SQL analysis of the orchestrator guard (imported by guard.py; standard library only).

One left-to-right tokenizer handles quotes (`'...'`, `"..."`, `` `...` ``, doubled quote and backslash escapes) and comments
(`-- `, `#`, `/* */`, MySQL `/*! ... */` which is CODE) together. Stripping comments before string literals (or the reverse) is what let
`SELECT '/*'; DELETE FROM t; SELECT '*/'` and "SELECT 1 -- '" newline "DELETE FROM t; -- '" through: what is a comment depends on which quote opened
first, so the two cannot be removed in separate passes.

The server's own mode can change how the same text splits (`NO_BACKSLASH_ESCAPES` turns `\\'` into a closed string; MySQL needs a space after `--`,
other servers do not), so the text is scanned under every combination and a statement found under any of them counts. An unterminated quote or
comment in the way the server reads it by default is unparseable and FAILS CLOSED (a pseudo statement that `sql_statement_writes` reports).
"""
import re

SQL_WRITE_KEYWORDS = {
    # data and schema
    "insert", "update", "delete", "replace", "merge", "create", "alter", "drop", "truncate", "rename", "load", "copy",
    # privileges, procedures, prepared statements, session control that runs or changes something
    "grant", "revoke", "call", "do", "prepare", "execute", "exec", "deallocate", "kill", "handler",
    # server administration
    "flush", "lock", "unlock", "reset", "purge", "optimize", "repair", "analyze", "install", "uninstall", "shutdown", "restart", "clone",
    "import", "change", "stop", "xa", "binlog", "cache", "vacuum", "reindex",
}
SQL_UNPARSEABLE = "\x00unparseable"
_SET_WRITE_RE = re.compile(r"set\s+(?:global|persist|persist_only|@@global\.|@@persist\.|@@persist_only\.|password|role|default\s+role|resource\s+group|binary_log)")
_START_WRITE_RE = re.compile(r"start\s+(?:replica|slave|group_replication)")
_QUOTES = "'\"`"
# a comment leaves this mark in the statement text. MySQL reads `SELECT 1 -- x` newline `DELETE FROM t` as ONE statement (a syntax error), but a
# parser that treats the line end as a break would run the DELETE, so what follows a comment is also looked at as a statement of its own when it
# starts like an unmistakable write
_COMMENT_MARK = "\x01"
_STRONG_WRITE_RE = re.compile(
    r"^\s*(?:delete\s+from|insert\s+(?:ignore\s+)?into|update\s+[\w.`'\"]+\s+set|replace\s+into|drop\s+\w|truncate\s+\w|alter\s+\w|create\s+\w|grant\s+\w|revoke\s+\w|"
    r"rename\s+table|load\s+data|call\s+\w|prepare\s+\w|execute\s+\w|deallocate\s+\w|kill\s+\w|flush\s+\w|lock\s+tables|handler\s+\w|do\s+\w)")


def _quoted_end(text, start, quote, backslash_escapes):
    """Index just past the closing quote of the literal opened at `start`, or -1 when it never closes. `quote` doubled is an escaped quote;
    a backslash escapes the next character in a string (not in a backtick identifier) when `backslash_escapes`."""
    index, size = start + 1, len(text)
    while index < size:
        char = text[index]
        if backslash_escapes and char == "\\" and quote != "`":
            index += 2
            continue
        if char == quote:
            if text[index + 1:index + 2] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return -1


def _scan(text, backslash_escapes, strict_dashes):
    """(statements, complete): the lowercase statements of `text` with comments dropped and quoted literals replaced by `''`. `complete` is
    False when a quote, a comment or an executable comment never closes (the statement being read at that point is still returned)."""
    statements, current = [], []
    index, size, executable_depth = 0, len(text), 0
    complete = True
    while index < size:
        char, pair = text[index], text[index:index + 2]
        if char in _QUOTES:
            end = _quoted_end(text, index, char, backslash_escapes)
            if end < 0:
                complete = False
                break
            current.append("''")
            index = end
        elif pair == "/*":
            if text.startswith("/*!", index) or text.startswith("/*M!", index):  # executable comment: its body is code
                index += 3 if text[index + 2] == "!" else 4
                while index < size and text[index].isdigit():
                    index += 1
                executable_depth += 1
                current.append(" ")
            else:
                end = text.find("*/", index + 2)
                if end < 0:
                    complete = False
                    break
                current.append(_COMMENT_MARK)
                index = end + 2
        elif pair == "*/" and executable_depth:
            executable_depth -= 1
            current.append(" ")
            index += 2
        elif char == "#" or (pair == "--" and (not strict_dashes or index + 2 >= size or text[index + 2] in " \t\r\n\f\v" or ord(text[index + 2]) < 32)):
            end = text.find("\n", index)
            index = size if end < 0 else end
            current.append(_COMMENT_MARK)
        elif char == ";":
            statements.append("".join(current))
            current = []
            index += 1
        else:
            current.append(char.lower())
            index += 1
    if executable_depth:
        complete = False
    statements.append("".join(current))
    result = []
    for part in statements:
        pieces = part.split(_COMMENT_MARK)
        whole = " ".join(pieces).strip().lstrip("( \t\r\n")
        if whole.strip("( \t\r\n"):
            result.append(whole)
        result.extend(piece.strip() for piece in pieces[1:] if _STRONG_WRITE_RE.match(piece.strip().lstrip("( \t\r\n")))
    return result, complete


def sql_statements(sql):
    """Statements of a SQL text, lowercased, comments removed and string/identifier literals replaced by `''` (a MySQL `/*! ... */` is code and stays).
    The text is read under each combination of the server modes that change where a literal or a comment ends; the union is returned. Unparseable
    text (an unterminated quote or comment as the server reads it by default) adds the `SQL_UNPARSEABLE` statement, which counts as a write."""
    text = str(sql)
    found = []
    modes = [(True, True)]
    if "\\" in text:
        modes.append((False, True))
    if "--" in text:
        modes.append((True, False))
        if "\\" in text:
            modes.append((False, False))
    for position, (backslash_escapes, strict_dashes) in enumerate(modes):
        statements, complete = _scan(text, backslash_escapes, strict_dashes)
        for statement in statements:
            if statement not in found:
                found.append(statement)
        if position == 0 and not complete:
            found.append(SQL_UNPARSEABLE)
    return found


def sql_statement_writes(statement):
    """The keyword that makes a statement write (or administer, or run something), else ''."""
    if statement == SQL_UNPARSEABLE:
        return "UNPARSEABLE SQL"
    words = re.findall(r"[a-z_@.]+", statement)
    first = words[0] if words else ""
    if first in SQL_WRITE_KEYWORDS:
        return first.upper()
    if first == "set" and _SET_WRITE_RE.match(statement):
        return "SET GLOBAL"
    if first == "start" and _START_WRITE_RE.match(statement):
        return "START REPLICA"
    if first in ("with", "explain", "describe", "desc"):
        # a CTE that writes, and EXPLAIN ANALYZE of a statement that writes (it runs it); `for update` is a lock, `insert(`/`replace(` are functions
        if first != "with" and "analyze" not in words:
            return ""
        for position, word in enumerate(words):
            if word not in ("insert", "update", "delete", "replace"):
                continue
            if word == "update" and words[position - 1:position] == ["for"]:
                continue
            if word in ("insert", "replace") and re.search(r"\b" + word + r"\s*\(", statement) and not re.search(r"\b" + word + r"\s+(?:into|ignore|low_priority|delayed|high_priority)\b", statement):
                continue
            return word.upper()
    if re.search(r"\binto\s+(?:outfile|dumpfile)\b", statement):
        return "SELECT INTO OUTFILE"
    return ""
