"""MCP read access for `claude -p` (planning replies, loop turns): computed from the user's `~/.claude.json`, no per-project toggle.

The servers `grafana`, `mcp-mysql-dev` and `mcp-mysql-prod` are defined by the USER (`mcpServers` of `~/.claude.json`, user scope).
Every one that is present is exposed with its READ tools only (Grafana: the Loki/datasource read list; MySQL: `mysql_query`, which is
a read/write server, so the guard rule `sql-write` is what keeps it read only). One strict MCP config file is written per run with only
those definitions (`--mcp-config <file> --strict-mcp-config`), so every other MCP stays out.

The definitions carry credentials, so the file goes under `<data dir>/.secrets/` (the guard's `secrets` rule blocks agent reads there),
is written atomically with owner-only permission, and its CONTENT never reaches an argv, a log or a response: callers get the path.
An unreadable `~/.claude.json` (or a server without `url`/`command`) = "none found": never an exception, never a guess. Stdlib only.
"""
import json
import os
import tempfile

CLAUDE_CONFIG_VARIABLE = "LOOP_CLAUDE_CONFIG_FILE"  # tests point this at a fake `.claude.json`
SECRETS_FOLDER_NAME = ".secrets"
MCP_CONFIG_FILE_NAME = "mcp-access.json"

GRAFANA_SERVER = "grafana"
MYSQL_DEV_SERVER = "mcp-mysql-dev"
MYSQL_PROD_SERVER = "mcp-mysql-prod"
GRAFANA_READ_TOOL_NAMES = ("query_loki_logs", "list_loki_label_names", "list_loki_label_values", "query_loki_stats",
                           "query_loki_patterns", "find_error_pattern_logs", "list_datasources")
MYSQL_READ_TOOL_NAMES = ("mysql_query",)
READ_TOOL_NAMES = {GRAFANA_SERVER: GRAFANA_READ_TOOL_NAMES, MYSQL_DEV_SERVER: MYSQL_READ_TOOL_NAMES, MYSQL_PROD_SERVER: MYSQL_READ_TOOL_NAMES}
SERVER_NAMES = tuple(READ_TOOL_NAMES)
# Belt and braces for Grafana: these writers are denied by name too (a deny rule beats an allow rule in any user setting).
GRAFANA_DENIED_WRITE_TOOL_NAMES = ("create_annotation", "update_annotation", "create_incident", "add_activity_to_incident", "update_dashboard",
                                   "create_folder", "create_datasource", "update_datasource", "create_snapshot", "delete_snapshot",
                                   "install_plugin", "alerting_manage_rules", "alerting_manage_routing", "grafana_api_request")


def tool_prefix(server):
    return "mcp__%s__" % server


def read_tools(server):
    """Full names (`mcp__<server>__<tool>`) of the read tools of a known server, [] for an unknown one."""
    return [tool_prefix(server) + name for name in READ_TOOL_NAMES.get(server, ())]


def servers_to_names(servers):
    names = []
    for item in servers or []:
        name = item.get("server") if isinstance(item, dict) else item
        if name in READ_TOOL_NAMES and name not in names:
            names.append(name)
    return names


def denied_write_tools(servers=None):
    """Writers denied by name (Grafana only; the MySQL server is kept read only by the guard rule `sql-write`)."""
    names = servers_to_names(servers) if servers is not None else SERVER_NAMES
    return [tool_prefix(GRAFANA_SERVER) + name for name in GRAFANA_DENIED_WRITE_TOOL_NAMES] if GRAFANA_SERVER in names else []


def claude_config_path():
    return os.environ.get(CLAUDE_CONFIG_VARIABLE) or os.path.join(os.path.expanduser("~"), ".claude.json")


def read_server_definitions(claude_json=None):
    """{server: definition} of the known servers that `~/.claude.json` defines with a `url` or a `command`. {} when unreadable."""
    try:
        with open(claude_json or claude_config_path(), encoding="utf-8-sig") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return {}
    servers = content.get("mcpServers") if isinstance(content, dict) else None
    if not isinstance(servers, dict):
        return {}
    found = {}
    for name in SERVER_NAMES:
        definition = servers.get(name)
        if not isinstance(definition, dict):
            continue
        has_url = isinstance(definition.get("url"), str) and definition["url"].strip()
        has_command = isinstance(definition.get("command"), str) and definition["command"].strip()
        if has_url or has_command:
            found[name] = definition
    return found


def describe_servers(claude_json=None):
    """One row per known server, configured or not: [{"server", "tools": [full read tool names], "configured": bool}]. No credentials."""
    definitions = read_server_definitions(claude_json)
    return [{"server": name, "tools": read_tools(name), "configured": name in definitions} for name in SERVER_NAMES]


def available_servers(claude_json=None):
    """Only the servers present in `~/.claude.json`: [{"server", "tools", "configured": True}] (a missing one is just absent)."""
    return [row for row in describe_servers(claude_json) if row["configured"]]


def allowed_tool_names(servers):
    """Read tool names of the given servers (rows of `available_servers` or plain names), in a stable order, no duplicates."""
    tools = []
    for name in servers_to_names(servers):
        tools.extend(read_tools(name))
    return tools


def strict_config_path(data_dir):
    return os.path.join(data_dir, SECRETS_FOLDER_NAME, MCP_CONFIG_FILE_NAME)


def write_strict_config(path, servers, claude_json=None):
    """Writes `{"mcpServers": {...}}` with ONLY the given servers (definitions copied from `~/.claude.json`) and returns `path`, or ''
    when nothing can be written (no server, no definition, or an OS error). For `--mcp-config <path> --strict-mcp-config`."""
    definitions = read_server_definitions(claude_json)
    chosen = {name: definitions[name] for name in servers_to_names(servers) if name in definitions}
    if not chosen or not path:
        return ""
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".mcp-access.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump({"mcpServers": chosen}, stream)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
    except OSError:
        return ""
    return path


def build_access(data_dir, claude_json=None):
    """Everything a `claude -p` needs: {"servers": [...], "allowed_tools": [...], "denied_tools": [...], "config_path": str}.
    `config_path` is '' when no known server is configured (then no MCP flags should be passed)."""
    servers = available_servers(claude_json)
    config_path = write_strict_config(strict_config_path(data_dir), servers, claude_json) if data_dir else ""
    if not config_path:
        return {"servers": [], "allowed_tools": [], "denied_tools": [], "config_path": ""}
    return {"servers": servers, "allowed_tools": allowed_tool_names(servers), "denied_tools": denied_write_tools(servers),
            "config_path": config_path}
