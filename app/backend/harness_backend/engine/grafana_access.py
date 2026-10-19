"""Read-only Grafana/Loki access for `claude -p` (planning chat, loop turns), opt-in per project (ACCESS.json `grafana_read`).

The `grafana` MCP server is defined by the USER (`mcpServers.grafana` of `~/.claude.json`, user scope). This module copies ONLY
that one definition into a minimal `--mcp-config` file (so every other MCP stays out with `--strict-mcp-config`) and names the
only tools the session may call. The definition carries credentials (headers), so the file goes under `<data dir>/.secrets/`
(the guard's `secrets` rule blocks agent reads of that folder), is written atomically with owner-only permission and its
CONTENT never reaches an argv, a log or a response: callers only get the path.

No definition found (or an unusable one) = the feature stays off ("" path): it never guesses a server. Stdlib only.
"""
import json
import os
import tempfile

SERVER_NAME = "grafana"
CLAUDE_CONFIG_VARIABLE = "LOOP_CLAUDE_CONFIG_FILE"  # tests point this at a fake `.claude.json`
SECRETS_FOLDER_NAME = ".secrets"
MCP_CONFIG_FILE_NAME = "grafana-mcp.json"
TOOL_PREFIX = "mcp__%s__" % SERVER_NAME
READ_TOOL_NAMES = ("query_loki_logs", "list_loki_label_names", "list_loki_label_values", "query_loki_stats",
                   "query_loki_patterns", "find_error_pattern_logs", "list_datasources")
READ_TOOLS = tuple(TOOL_PREFIX + name for name in READ_TOOL_NAMES)
# Belt and braces: the allow list above is what permits tools (everything else is refused in these modes), and these writers are also
# denied by name, so no setting of the user can widen the access to them (a deny rule beats an allow rule).
DENIED_WRITE_TOOL_NAMES = ("create_annotation", "update_annotation", "create_incident", "add_activity_to_incident", "update_dashboard",
                           "create_folder", "create_datasource", "update_datasource", "create_snapshot", "delete_snapshot",
                           "install_plugin", "alerting_manage_rules", "alerting_manage_routing", "grafana_api_request")
DENIED_WRITE_TOOLS = tuple(TOOL_PREFIX + name for name in DENIED_WRITE_TOOL_NAMES)


def claude_config_path():
    return os.environ.get(CLAUDE_CONFIG_VARIABLE) or os.path.join(os.path.expanduser("~"), ".claude.json")


def read_server_definition(config_path=None):
    """The user-scope `grafana` server definition (a dict with a `url` or a `command`), or None. Never prints it."""
    try:
        with open(config_path or claude_config_path(), encoding="utf-8-sig") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return None
    servers = content.get("mcpServers") if isinstance(content, dict) else None
    definition = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(definition, dict):
        return None
    has_url = isinstance(definition.get("url"), str) and definition["url"].strip()
    has_command = isinstance(definition.get("command"), str) and definition["command"].strip()
    return definition if has_url or has_command else None


def mcp_config_path(data_dir):
    return os.path.join(data_dir, SECRETS_FOLDER_NAME, MCP_CONFIG_FILE_NAME)


def write_mcp_config(data_dir, config_path=None):
    """Writes the minimal config and returns its path, or '' when the Grafana server cannot be built reliably."""
    definition = read_server_definition(config_path)
    if definition is None or not data_dir:
        return ""
    path = mcp_config_path(data_dir)
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".grafana-mcp.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump({"mcpServers": {SERVER_NAME: definition}}, stream)
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
