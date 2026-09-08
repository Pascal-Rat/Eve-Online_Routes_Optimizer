"""Loopback HTTP transport and static assets for the courier planner."""

from __future__ import annotations

import json
import logging
import mimetypes
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from eve_courier_optimizer import __version__
from eve_courier_optimizer.application.file_lock import WorkspaceConflict
from eve_courier_optimizer.eve.esi import EsiClient, EsiError
from eve_courier_optimizer.eve.http import ResponseCache
from eve_courier_optimizer.eve.zkill import ZkillClient, ZkillError
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.background_jobs import BackgroundJobs
from eve_courier_optimizer.web.contracts import MutationResponse
from eve_courier_optimizer.web.workspace import PlanningWorkspace, default_workspace_path

JsonObject = dict[str, object]
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_DOWNLOAD_FILES = frozenset({"snapshot.json", "plan.json", "execution.json"})
_MAX_REQUEST_BYTES = 1_048_576


def asset(name: str) -> tuple[bytes, str]:
    if name not in {
        "index.html",
        "styles.css",
        "app.js",
        "api.js",
        "contract_schemas.js",
        "contract_validation.js",
        "display.js",
        "autocomplete.js",
        "planner_form.js",
        "route_view.js",
    }:
        raise FileNotFoundError(name)
    resource = files("eve_courier_optimizer.web").joinpath("assets", name)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return resource.read_bytes(), content_type


def _handler_type(
    app: PlanningWorkspace,
    jobs: BackgroundJobs,
    state_lock: threading.RLock,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"EveCourierLocal/{__version__}"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)
            self._read_deadline = threading.Timer(10, self._expire_read)
            self._read_deadline.daemon = True
            self._read_deadline.start()

        def _expire_read(self) -> None:
            # A peer may have closed before the read deadline fired.
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

        def handle(self) -> None:
            with suppress(BrokenPipeError, ConnectionResetError):
                super().handle()

        def finish(self) -> None:
            self._read_deadline.cancel()
            with suppress(BrokenPipeError, ConnectionResetError):
                super().finish()

        def log_message(self, format: str, *args: object) -> None:
            print(f"web: {format % args}", file=sys.stderr)

        def _local_request_allowed(self) -> bool:
            host = self.headers.get("Host", "").split(":", maxsplit=1)[0].casefold()
            if host not in _LOCAL_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlsplit(origin).hostname
                if origin_host is None or origin_host.casefold() not in _LOCAL_HOSTS:
                    return False
            return True

        def _common_headers(self, *, cache: str = "no-store") -> None:
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'",
            )

        def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status)

        def _body(self) -> JsonObject:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                raise ValueError("POST requests must use application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                raise ValueError("request body must be between 1 byte and 1 MiB")
            decoded = json.loads(self.rfile.read(length))
            if not isinstance(decoded, dict):
                raise ValueError("JSON request body must be an object")
            return cast(JsonObject, decoded)

        def _download(self, filename: str) -> None:
            if filename not in _DOWNLOAD_FILES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = app.artifact(filename)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            self._read_deadline.cancel()
            try:
                with state_lock:
                    self._get()
            except WorkspaceConflict as error:
                self._send_error_json(HTTPStatus.CONFLICT, str(error))
            except (ValueError, OSError, RuntimeError) as error:
                logging.getLogger(__name__).exception("Local read failed: %s", self.path)
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def _get(self) -> None:
            if not self._local_request_allowed():
                self._send_error_json(HTTPStatus.FORBIDDEN, "local requests only")
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api/status":
                job = jobs.status()
                self._send_json({**app.status(), "job": job})
                return
            if parsed.path.startswith("/api/jobs/"):
                try:
                    self._send_json({"job": jobs.status(parsed.path.removeprefix("/api/jobs/"))})
                except ValueError as error:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(error))
                return
            if parsed.path == "/api/regions":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._send_json(app.region_matches(query))
                return
            if parsed.path == "/api/systems":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._send_json(app.system_matches(query))
                return
            if parsed.path.startswith("/download/"):
                self._download(parsed.path.removeprefix("/download/"))
                return
            asset_name = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path[1:]
            try:
                data, content_type = asset(asset_name)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._common_headers(cache="no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if not self._local_request_allowed():
                self._send_error_json(HTTPStatus.FORBIDDEN, "local requests only")
                return
            try:
                body = self._body()
                self._read_deadline.cancel()
                with state_lock:
                    self._post(body)
            except json.JSONDecodeError:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "request body is not valid JSON")
            except WorkspaceConflict as error:
                self._send_error_json(HTTPStatus.CONFLICT, str(error))
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            except (EsiError, ZkillError) as error:
                self._send_error_json(HTTPStatus.BAD_GATEWAY, str(error))
            except (OSError, RuntimeError) as error:
                logging.getLogger(__name__).exception("Local operation failed: %s", self.path)
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def _post(self, body: JsonObject) -> None:
            if self.path == "/api/jobs":
                job_body = body.get("input")
                if not isinstance(job_body, dict):
                    raise ValueError("job input must be an object")
                self._send_json(
                    {
                        "job": jobs.start(
                            str(body.get("operation", "")), cast(dict[str, object], job_body)
                        )
                    },
                    HTTPStatus.ACCEPTED,
                )
                return
            if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
                job_id = self.path.removeprefix("/api/jobs/").removesuffix("/cancel")
                self._send_json({"job": jobs.cancel(job_id)})
                return
            if jobs.running:
                self._send_error_json(
                    HTTPStatus.CONFLICT,
                    "wait for the background job or cancel it first",
                )
                return
            routes: dict[str, Callable[[JsonObject], MutationResponse]] = {
                "/api/scan": app.scan,
                "/api/rank": app.rank,
                "/api/solve": app.solve,
                "/api/execution/start": app.start_execution,
                "/api/action": app.record_action,
                "/api/replan": app.replan,
                "/api/execution/extend": app.extend_horizon,
            }
            payload: MutationResponse
            if self.path == "/api/execution/reset":
                payload = app.reset_execution(body)
            else:
                action = routes.get(self.path)
                if action is None:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "unknown API route")
                    return
                payload = action(body)
            self._send_json(payload)

    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    def __init__(self, app: PlanningWorkspace, port: int) -> None:
        self.jobs = BackgroundJobs(app)
        self.state_lock = threading.RLock()
        self._connections = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", port), _handler_type(app, self.jobs, self.state_lock))

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("local HTTP server requires a TCP socket")
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def server_close(self) -> None:
        with self.state_lock:
            self.jobs.close()
        super().server_close()


def create_http_server(app: PlanningWorkspace, *, port: int = 8765) -> HTTPServer:
    """Create, but do not run, a loopback-only server. ``port=0`` is useful in tests."""

    if port < 0 or port > 65_535:
        raise ValueError("port must be between 0 and 65535")
    return LocalHTTPServer(app, port)


def run_local_web_ui(
    graph: UniverseGraph,
    *,
    port: int = 8765,
    workspace: Path | None = None,
    open_browser: bool = True,
) -> int:
    """Run the local control deck until interrupted."""

    if port <= 0 or port > 65_535:
        raise ValueError("port must be between 1 and 65535")
    root = workspace or default_workspace_path()
    client = EsiClient(cache=ResponseCache(root / "esi-cache.sqlite3"))
    zkill = ZkillClient(cache=ResponseCache(root / "zkill-cache.sqlite3"))
    app = PlanningWorkspace(graph, client, root, zkill)
    server = create_http_server(app, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"local web UI: {url}")
    print(f"session files: {root}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
