"""HTTP server embedded in the GUI process. Listens on 127.0.0.1 only and requires a per-run token."""

import hmac
import importlib
import json
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from katrain.api.protocol import (
    API_VERSION,
    COMMAND_PATH,
    DEFAULT_PORT,
    HEALTH_PATH,
    PORT_SEARCH_RANGE,
    TOKEN_HEADER,
    ApiError,
    remove_discovery,
    write_discovery,
)
from katrain.core.constants import OUTPUT_DEBUG, OUTPUT_ERROR, OUTPUT_INFO, VERSION

MAX_BODY = 16 * 1024 * 1024


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = f"KaTrainAPI/{API_VERSION}"
    api = None  # set on the subclass created per server

    def log_message(self, fmt, *args):
        self.api.katrain.log(f"[api] {self.address_string()} {fmt % args}", OUTPUT_DEBUG)

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.headers.get(TOKEN_HEADER, "")
        return hmac.compare_digest(token, self.api.token)

    def do_GET(self):
        if self.path != HEALTH_PATH:
            return self._reply(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})
        if not self._authorized():
            return self._reply(401, {"ok": False, "error": {"code": "unauthorized", "message": "bad token"}})
        self._reply(200, {"ok": True, "result": self.api.health()})

    def do_POST(self):
        if self.path != COMMAND_PATH:
            return self._reply(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})
        if not self._authorized():
            return self._reply(401, {"ok": False, "error": {"code": "unauthorized", "message": "bad token"}})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                raise ApiError("bad_request", "request too large")
            request = json.loads(self.rfile.read(length) or b"{}")
            command = request.get("command")
            args = request.get("args") or {}
            if not isinstance(command, str) or not isinstance(args, dict):
                raise ApiError("bad_request", "expected {'command': str, 'args': object}")
        except ApiError as e:
            return self._reply(400, {"ok": False, "error": e.to_json()})
        except (ValueError, TypeError) as e:
            return self._reply(400, {"ok": False, "error": {"code": "bad_request", "message": str(e)}})
        self._reply(200, self.api.execute(command, args))


class ApiServer:
    def __init__(self, katrain, port=None):
        from katrain.api.commands import CommandHandler  # imports Kivy-side modules

        importlib.import_module("katrain.api.tsumego")  # registers the tsumego commands
        importlib.import_module("katrain.api.sandbox")  # registers the background sandbox commands
        self.katrain = katrain
        self.token = secrets.token_urlsafe(24)
        self.handler = CommandHandler(katrain)
        self.started = time.time()
        handler_cls = type("KaTrainApiRequestHandler", (_RequestHandler,), {"api": self})
        port = port or DEFAULT_PORT
        last_error = None
        for candidate in range(port, port + PORT_SEARCH_RANGE):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", candidate), handler_cls)
                break
            except OSError as e:
                last_error = e
        else:
            raise OSError(f"no free port in {port}-{port + PORT_SEARCH_RANGE - 1}: {last_error}")
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="katrain-api", daemon=True)

    def start(self):
        self.thread.start()
        write_discovery(
            {
                "api_version": API_VERSION,
                "katrain_version": VERSION,
                "host": "127.0.0.1",
                "port": self.port,
                "token": self.token,
                "pid": os.getpid(),
                "started": self.started,
            }
        )
        self.katrain.log(f"KaTrain API listening on 127.0.0.1:{self.port}", OUTPUT_INFO)

    def stop(self):
        remove_discovery(os.getpid())
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    def health(self):
        return {
            "api_version": API_VERSION,
            "katrain_version": VERSION,
            "pid": os.getpid(),
            "uptime": round(time.time() - self.started, 1),
        }

    def execute(self, command, args):
        try:
            return {"ok": True, "result": self.handler.dispatch(command, args)}
        except ApiError as e:
            return {"ok": False, "error": e.to_json()}
        except Exception as e:
            self.katrain.log(f"[api] command {command} failed: {e}\n{traceback.format_exc()}", OUTPUT_ERROR)
            return {"ok": False, "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}}


def start_api_server(katrain):
    """Start the API unless disabled via config `api/enabled` or env KATRAIN_API=0. Never raises."""
    if os.environ.get("KATRAIN_API", "1") == "0" or not katrain.config("api/enabled", True):
        return None
    try:
        port = int(os.environ.get("KATRAIN_API_PORT") or katrain.config("api/port", DEFAULT_PORT))
        server = ApiServer(katrain, port=port)
        server.start()
        return server
    except Exception as e:
        katrain.log(f"Failed to start KaTrain API: {e}", OUTPUT_ERROR)
        return None
