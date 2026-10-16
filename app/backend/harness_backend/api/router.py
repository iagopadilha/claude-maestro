"""Minimal HTTP router (stdlib). Each controller registers its routes in `register_routes(router)`.

Route pattern with parameters in braces: `/p/{project_id}/api/state`. A parameter matches one segment (no `/`), except
when it ends in `*` (`{path*}`), which matches the rest of the path.
"""
import re
from dataclasses import dataclass, field

DEFAULT_BODY_LIMIT_BYTES = 64 * 1024


@dataclass
class Request:
    method: str
    path: str
    route_params: dict
    query: dict  # parse_qs: {"key": ["value", ...]}
    body: dict
    headers: dict = field(default_factory=dict)

    def get_query(self, key, default=""):
        values = self.query.get(key) or [default]
        return values[0]


@dataclass
class Response:
    status: int
    body: object = None  # dict/list becomes JSON; bytes go raw with `content_type`
    content_type: str = "application/json; charset=utf-8"

    @staticmethod
    def json(status, data):
        return Response(status, data)

    @staticmethod
    def error(status, message, **extras):
        body = {"ok": False, "error": message}
        body.update(extras)
        return Response(status, body)


@dataclass
class Route:
    method: str
    pattern: str
    expression: object
    handler: object
    requires_token: bool
    body_limit_bytes: int
    counts_as_activity: bool


def compile_route_pattern(pattern):
    parts = []
    for piece in re.split(r"(\{[a-z_]+\*?\})", pattern):
        if piece.startswith("{") and piece.endswith("}"):
            name = piece[1:-1]
            parts.append("(?P<%s>.+)" % name[:-1] if name.endswith("*") else "(?P<%s>[^/]+)" % name)
        else:
            parts.append(re.escape(piece))
    return re.compile("^" + "".join(parts) + "$")


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, requires_token=None, body_limit_bytes=DEFAULT_BODY_LIMIT_BYTES,
            counts_as_activity=None):
        """`handler(request) -> Response | (status, dict)`. Token and activity apply by default to POST/PUT/DELETE."""
        mutating = method.upper() != "GET"
        self.routes.append(Route(method.upper(), pattern, compile_route_pattern(pattern), handler,
                                 mutating if requires_token is None else requires_token, body_limit_bytes,
                                 mutating if counts_as_activity is None else counts_as_activity))

    def find_route(self, method, path):
        """(route, params) or (None, None). An existing path with another method returns (None, {}) to become a 405."""
        path_exists = False
        for route in self.routes:
            found = route.expression.match(path)
            if not found:
                continue
            if route.method == method.upper():
                return route, found.groupdict()
            path_exists = True
        return None, ({} if path_exists else None)

    def largest_body_limit(self, method, path):
        route, _ = self.find_route(method, path)
        return route.body_limit_bytes if route else DEFAULT_BODY_LIMIT_BYTES


def normalize_response(returned):
    if isinstance(returned, Response):
        return returned
    if isinstance(returned, tuple) and len(returned) == 2:
        return Response(returned[0], returned[1])
    return Response(200, returned)
