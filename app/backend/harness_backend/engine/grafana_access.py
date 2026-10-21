"""Grafana/Loki read access: a thin wrapper over `mcp_access` (the `grafana_read` toggle of ACCESS.json no longer gates anything).

Kept for the callers that still import it (`write_mcp_config`, `READ_TOOLS`, `DENIED_WRITE_TOOLS`): they get the Grafana part of the
computed MCP access. The definition carries credentials, so the config file stays under `<data dir>/.secrets/` and only its path is
returned. No definition found (or an unusable one) = "" path. Stdlib only.
"""
import os

import mcp_access

SERVER_NAME = mcp_access.GRAFANA_SERVER
CLAUDE_CONFIG_VARIABLE = mcp_access.CLAUDE_CONFIG_VARIABLE
SECRETS_FOLDER_NAME = mcp_access.SECRETS_FOLDER_NAME
MCP_CONFIG_FILE_NAME = "grafana-mcp.json"
TOOL_PREFIX = mcp_access.tool_prefix(SERVER_NAME)
READ_TOOL_NAMES = mcp_access.GRAFANA_READ_TOOL_NAMES
READ_TOOLS = tuple(mcp_access.read_tools(SERVER_NAME))
DENIED_WRITE_TOOL_NAMES = mcp_access.GRAFANA_DENIED_WRITE_TOOL_NAMES
DENIED_WRITE_TOOLS = tuple(TOOL_PREFIX + name for name in DENIED_WRITE_TOOL_NAMES)


def claude_config_path():
    return mcp_access.claude_config_path()


def read_server_definition(config_path=None):
    """The user-scope `grafana` server definition (a dict with a `url` or a `command`), or None. Never prints it."""
    return mcp_access.read_server_definitions(config_path).get(SERVER_NAME)


def mcp_config_path(data_dir):
    return os.path.join(data_dir, SECRETS_FOLDER_NAME, MCP_CONFIG_FILE_NAME)


def write_mcp_config(data_dir, config_path=None):
    """Writes the minimal Grafana-only config and returns its path, or '' when the server cannot be built reliably."""
    if not data_dir:
        return ""
    return mcp_access.write_strict_config(mcp_config_path(data_dir), [SERVER_NAME], config_path)
