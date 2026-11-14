"""A small web server for pages only the user at this computer opens (the Portward page; #62's password page can use
it too).

What keeps other programs and websites out:

- It listens on 127.0.0.1 only, on a port the OS picks, so nothing on the network can reach it.
- Every address starts with a per-run token (`secrets.token_urlsafe`, 256 bits). The server opens the user's browser
  on it itself; the AI is never given it. Without the token every path is a 404.
- It answers only when the Host header is its own (127.0.0.1 or localhost on its port). A website that rebinds its
  own name to 127.0.0.1 (DNS rebinding) sends its own name as Host, and is refused.
- A change (POST) needs the page's form token (`form_token`, a second secret, only in the page's own forms), an
  Origin that is the page's own, and no cross-site Sec-Fetch-Site. A website can't read the page, so it can't learn
  either token, and a form it submits carries its own Origin (CSRF).
- No cookies (127.0.0.1 cookies are shared with every other local port), no scripts allowed by the CSP, no framing,
  referrer to other sites, nothing cached.

Handlers run one at a time (`App` implementations needn't be thread-safe).

For a one-time page (#62's password page): `expires_in` closes the server after that many seconds, and a response
with `close_after=True` closes it once that response is sent. A closed server listens no more.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

LOOPBACK = "127.0.0.1"
MAX_FORM_BYTES = 64 * 1024
FORM_TOKEN_FIELD = "form_token"
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
                                "base-uri 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",  # no-referrer would make the browser post our own forms with Origin: null
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@dataclass(frozen=True)
class Request:
    method: str  # GET or POST
    path: str  # after the token, e.g. "/" or "/sent/abc"
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)  # a POST's fields, the form token already checked
    pairs: tuple[tuple[str, str], ...] = ()  # every field in order, repeats kept (ticked checkboxes), token removed

    def every(self, name: str) -> tuple[str, ...]:
        """Every value sent for `name` (a group of checkboxes)."""
        return tuple(v for k, v in self.pairs if k == name)


@dataclass(frozen=True)
class Response:
    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    location: str | None = None  # for a redirect (303 after a POST)
    close_after: bool = False  # close the server once this response is sent (a one-time page's last answer)

    @classmethod
    def redirect(cls, location: str) -> Response:
        return cls(HTTPStatus.SEE_OTHER, "", "text/plain; charset=utf-8", location)

    @classmethod
    def text(cls, status: int, body: str) -> Response:
        return cls(status, body, "text/plain; charset=utf-8")


class App(Protocol):
    def __call__(self, request: Request, web: LocalWeb) -> Response: ...


class LocalWeb:
    """One run of a local page server. `start()` binds 127.0.0.1 and serves on a background thread."""

    def __init__(self, app: App, *, expires_in: float | None = None) -> None:
        self._app = app
        self.token = secrets.token_urlsafe(32)
        self.form_token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._expires_in = expires_in
        self._closed = threading.Event()

    @property
    def port(self) -> int:
        assert self._server is not None, "not started"
        return self._server.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def link(self, path: str = "/") -> str:
        """A path on this page, token included, for the page's own links and forms."""
        return f"/{self.token}{path}"

    def url(self, path: str = "/") -> str:
        """The full address to open in the user's browser. Never hand it to the AI."""
        return self.origin + self.link(path)

    def bind(self) -> LocalWeb:
        """Listen on 127.0.0.1, on a port the OS picks (nothing is served until start or serve_forever)."""
        if self._server is None:
            self._server = _QuietServer((LOOPBACK, 0), _handler_for(self))
            if self._expires_in is not None:
                timer = threading.Timer(max(self._expires_in, 0.0), self.close)
                timer.daemon = True
                timer.start()
        return self

    def start(self) -> LocalWeb:
        """Serve on a background thread (inside the MCP server), for as long as the process runs."""
        if self._server is None:
            self.bind()
            assert self._server is not None
            threading.Thread(target=self._server.serve_forever, name="portward-page", daemon=True).start()
        return self

    def serve_forever(self) -> None:
        """Serve on this thread (the `portward-free page` command) until interrupted."""
        self.bind()
        assert self._server is not None
        self._server.serve_forever()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        """Stop serving, from any thread (a handler's own included): nothing answers on the port afterwards."""
        if not self._closed.is_set():
            self._closed.set()
            threading.Thread(target=self.stop, name="portward-page-close", daemon=True).start()

    def stop(self) -> None:
        self._closed.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # the checks, in the order a request meets them

    def _host_ok(self, host: str | None) -> bool:
        return host in {f"{LOOPBACK}:{self.port}", f"localhost:{self.port}"}

    def _inside(self, raw_path: str) -> str | None:
        """The path after the token, or None when the token is missing or wrong."""
        prefix, _, rest = raw_path.lstrip("/").partition("/")
        if not hmac.compare_digest(prefix.encode(), self.token.encode()):
            return None
        return "/" + rest

    def _same_origin(self, origin: str | None, fetch_site: str | None) -> bool:
        if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
            return False
        return origin in {self.origin, f"http://localhost:{self.port}"}

    def _handle(self, request: Request) -> Response:
        with self._lock:
            return self._app(request, self)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # the stdlib prints the traceback to stderr, and a handler's locals may hold what the user typed


class _Handler(BaseHTTPRequestHandler):
    """One request to a LocalWeb: the checks (closed, Host, token, and for a change Origin, form type and form token)
    in the order a request meets them, then the app. `web` is set per server by `_handler_for`."""

    web: LocalWeb
    server_version = "Portward"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib's name
        pass  # addresses carry the token: never write them to a log

    def do_GET(self) -> None:  # noqa: N802 - stdlib's name
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib's name
        self._serve("POST")

    def _serve(self, method: str) -> None:
        request = self._checked(method)
        if isinstance(request, Response):
            return self._send(request)
        try:
            response = self.web._handle(request)
        except Exception:  # never shown or printed: it may hold what the user typed
            response = Response.text(HTTPStatus.INTERNAL_SERVER_ERROR, "Something went wrong on this page.")
        self._send(response)
        if response.close_after:
            self.web.close()

    def _checked(self, method: str) -> Request | Response:
        """The request for the app, or the refusal to send instead."""
        web = self.web
        if web.closed:
            return Response.text(HTTPStatus.GONE, "This page has closed.")
        if not web._host_ok(self.headers.get("Host")):
            return Response.text(HTTPStatus.MISDIRECTED_REQUEST, "This page answers only on its own address.")
        split = urllib.parse.urlsplit(self.path)
        inside = web._inside(split.path)
        if inside is None:
            return Response.text(HTTPStatus.NOT_FOUND, "Not found.")
        pairs = self._checked_form() if method == "POST" else []
        if isinstance(pairs, Response):
            return pairs
        return Request(method, inside, dict(urllib.parse.parse_qsl(split.query)), dict(pairs), tuple(pairs))

    def _checked_form(self) -> list[tuple[str, str]] | Response:
        """A change's fields, once they passed the Origin, type and form-token checks."""
        refused = self._refused_post()
        if refused is not None:
            return refused
        pairs = self._form()
        if pairs is None:
            return Response.text(HTTPStatus.BAD_REQUEST, "That form couldn't be read.")
        token = dict(pairs).get(FORM_TOKEN_FIELD, "")
        if not hmac.compare_digest(token.encode(), self.web.form_token.encode()):
            return Response.text(HTTPStatus.FORBIDDEN, "That change didn't come from this page.")
        return [(k, v) for k, v in pairs if k != FORM_TOKEN_FIELD]

    def _refused_post(self) -> Response | None:
        if not self.web._same_origin(self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")):
            return Response.text(HTTPStatus.FORBIDDEN, "That change didn't come from this page.")
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "application/x-www-form-urlencoded":
            return Response.text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Changes are sent as a form.")
        return None

    def _form(self) -> list[tuple[str, str]] | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length < 0 or length > MAX_FORM_BYTES:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qsl(raw, keep_blank_values=True)

    def _send(self, response: Response) -> None:
        body = response.body.encode("utf-8")
        self.send_response(response.status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        if response.location is not None:
            self.send_header("Location", response.location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _handler_for(web: LocalWeb) -> Callable[..., BaseHTTPRequestHandler]:
    """The request handler class for one server (the stdlib makes one handler per request from it)."""
    return type("Handler", (_Handler,), {"web": web})
