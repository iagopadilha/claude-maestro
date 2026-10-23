"""The app's HTTP server: 127.0.0.1 only, Host check (DNS rebinding), token on the methods that change state, body limit per
route and serving of the front end (SPA) with the token injected. It knows no business rule: everything goes through the Router."""
import json
import mimetypes
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .router import Request, Response, normalize_response

BODY_DISCARD_LIMIT_BYTES = 16 * 1024 * 1024
SPA_PAGE_PATTERN = re.compile(r"^/(p/[^/]+/?)?$")  # "/" (hub) and "/p/<id>/" (project panel) serve the same index.html


def inject_token_into_page(html, token):
    tag = '<meta name="monitor-token" content="%s">' % token
    replaced, count = re.subn(r"<meta\s+name=[\"']monitor-token[\"'][^>]*>", tag, html, count=1, flags=re.I)
    if count:
        return replaced
    return re.sub(r"</head>", "  " + tag + "\n</head>", html, count=1, flags=re.I) if re.search(r"</head>", html, re.I) else tag + html


class AppHttpServer:
    def __init__(self, router, frontend_dir, activity_monitor, log, token=None):
        self.router = router
        self.frontend_dir = os.path.abspath(frontend_dir)
        self.activity_monitor = activity_monitor
        self.log = log
        self.token = token or secrets.token_hex(32)
        self.server = None
        self.port = None
        self.url = None
        self.stopping = threading.Event()

    def open_port(self, port_range, fixed_port=0):
        class Server(ThreadingHTTPServer):
            allow_reuse_address = False  # on Windows, reuse would leave two processes on the same port
            daemon_threads = True

        ports = [fixed_port] if fixed_port else list(range(port_range[0], port_range[1] + 1))
        for port in ports:
            try:
                self.server = Server(("127.0.0.1", port), create_http_handler(self))
            except OSError:
                continue
            self.port = self.server.server_address[1]
            self.url = "http://localhost:%d/" % self.port  # bound to 127.0.0.1; the Host check accepts localhost
            return self.url
        raise OSError("port %d is busy" % fixed_port if fixed_port else "no free port in %d..%d" % port_range)

    def serve_until_stopped(self):
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            try:
                self.server.server_close()
            except OSError:
                pass

    def request_shutdown(self):
        if self.server and not self.stopping.is_set():
            self.stopping.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def create_http_handler(app):
    class HttpHandler(BaseHTTPRequestHandler):
        server_version = "LoopHarness"

        def log_message(self, *args):
            pass

        def send_bytes(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_app_response(self, response):
            if isinstance(response.body, (bytes, bytearray)):
                return self.send_bytes(response.status, bytes(response.body), response.content_type)
            body = json.dumps(response.body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_bytes(response.status, body, "application/json; charset=utf-8")

        def is_host_allowed(self):
            host = (self.headers.get("Host") or "").strip().lower()
            return host in ("127.0.0.1:%d" % app.port, "localhost:%d" % app.port)

        def do_GET(self):
            self.serve_request("GET")

        def do_POST(self):
            self.serve_request("POST")

        def do_PUT(self):
            self.serve_request("PUT")

        def do_PATCH(self):
            self.serve_request("PATCH")

        def do_DELETE(self):
            self.serve_request("DELETE")

        def serve_request(self, method):
            if not self.is_host_allowed():
                return self.send_app_response(Response.error(403, "invalid Host"))
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            if method == "GET" and SPA_PAGE_PATTERN.match(path):
                return self.serve_spa_page()
            if method == "GET" and path.startswith("/static/"):
                return self.serve_static_file(path[len("/static/"):])
            route, params = app.router.find_route(method, path)
            if route is None:
                return self.send_app_response(Response.error(405 if params == {} else 404, "method not allowed" if params == {} else "route not found"))
            if route.counts_as_activity or (method == "GET" and parse_qs(parts.query).get("visible", [""])[0] == "1"):
                app.activity_monitor.record_activity()
            if route.requires_token and not secrets.compare_digest(self.headers.get("X-Monitor-Token") or "", app.token):
                return self.send_app_response(Response.error(403, "missing or invalid token"))
            body, error = self.read_json_body(method, route.body_limit_bytes)
            if error:
                return self.send_app_response(error)
            request = Request(method, path, params, parse_qs(parts.query), body, dict(self.headers.items()))
            try:
                response = normalize_response(route.handler(request))
            except Exception as failure:  # the API never brings the server down; the front end shows the banner
                app.log.error("route_failed", route=route.pattern, type=type(failure).__name__, message=str(failure)[:300])
                response = Response.error(500, "internal error: %s" % type(failure).__name__)
            self.send_app_response(response)

        def read_json_body(self, method, limit_bytes):
            if method == "GET":
                return {}, None
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None, Response.error(400, "invalid Content-Length")
            if size > limit_bytes:
                # discard the body (with a cap) so the client gets the 413; without reading it all, Windows resets the connection before the response
                remaining = min(size, max(4 * limit_bytes, BODY_DISCARD_LIMIT_BYTES))
                while remaining > 0:
                    piece = self.rfile.read(min(65536, remaining))
                    if not piece:
                        break
                    remaining -= len(piece)
                return None, Response.error(413, "body larger than %d KB" % (limit_bytes // 1024))
            raw = self.rfile.read(size) if size else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except ValueError:
                return None, Response.error(400, "body is not valid JSON")
            if not isinstance(body, dict):
                return None, Response.error(400, "body must be a JSON object")
            return body, None

        def serve_spa_page(self):
            app.activity_monitor.record_activity()
            try:
                with open(os.path.join(app.frontend_dir, "index.html"), encoding="utf-8") as handle:
                    html = handle.read()
            except OSError:
                return self.send_app_response(Response.error(404, "frontend/index.html does not exist yet"))
            self.send_bytes(200, inject_token_into_page(html, app.token).encode("utf-8"), "text/html; charset=utf-8")

        def serve_static_file(self, relative):
            root = os.path.realpath(app.frontend_dir)
            target = os.path.realpath(os.path.join(root, relative))
            if os.path.commonpath([root, target]) != root or not os.path.isfile(target):
                return self.send_app_response(Response.error(404, "file not found"))
            content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
            if target.endswith((".js", ".mjs")):
                content_type = "text/javascript"
            if content_type.startswith("text/") or content_type in ("application/json", "image/svg+xml"):
                content_type += "; charset=utf-8"
            with open(target, "rb") as handle:
                self.send_bytes(200, handle.read(), content_type)

    return HttpHandler
