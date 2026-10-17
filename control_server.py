"""
control_server.py - tiny local web server for the Teensy control UI.

Serves control_ui.html and exposes:
    GET  /api/status   -> {"connected": bool, "port": str|null, "requested": str|null, "error": str|null}
    GET  /api/ports    -> [{"device": "COM5", "description": "..."}]
    POST /api/port     -> {"port": "COM5"} selects the serial port (null disconnects)
    GET  /api/layout   -> saved UI layout (JSON)
    POST /api/layout   -> save UI layout
    POST /api/send     -> {"name": "kp", "value": 1.5}  -> forwarded to the MCU

Used by foxglove_bridge.py (which owns the serial port). Standard library only.
"""

import json
import math
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import serial.tools.list_ports

MAX_BODY = 1_000_000
NAME_RE = re.compile(r"^[A-Za-z0-9_./-]{1,24}$")  # 24 = MAX_NAME_LEN on the MCU
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def available_ports():
    """Serial ports currently present, sorted naturally (COM2 before COM10)."""
    def key(p):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.device)]
    return [{"device": p.device, "description": p.description or ""}
            for p in sorted(serial.tools.list_ports.comports(), key=key)]


def make_handler(link, layout_path: Path, html_path: Path, bind_host: str):
    local_only = bind_host in LOCAL_HOSTS

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        # -- helpers
        def _reply(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _request_allowed(self) -> bool:
            """Block DNS-rebinding and cross-site requests to the local server."""
            host = self.headers.get("Host", "")
            if local_only and urlsplit("//" + host).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{host}"

        def _read_json(self):
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._reply(415, {"ok": False, "error": "expected application/json"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._reply(413, {"ok": False, "error": "body too large"})
                return None
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                self._reply(400, {"ok": False, "error": "invalid JSON"})
                return None

        # -- routes
        def do_GET(self):
            if not self._request_allowed():
                return self._reply(403, {"ok": False, "error": "forbidden"})
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                try:
                    return self._reply(200, html_path.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self._reply(500, {"ok": False, "error": f"missing {html_path.name}"})
            if path == "/api/status":
                return self._reply(200, {"connected": link.connected, "port": link.port,
                                         "requested": link.requested, "error": link.error})
            if path == "/api/ports":
                return self._reply(200, available_ports())
            if path == "/api/layout":
                try:
                    return self._reply(200, layout_path.read_bytes())
                except OSError:
                    return self._reply(200, {"controls": []})
            self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._request_allowed():
                return self._reply(403, {"ok": False, "error": "forbidden"})
            path = urlsplit(self.path).path
            body = self._read_json()
            if body is None:
                return

            if path == "/api/send":
                name = body.get("name") if isinstance(body, dict) else None
                value = body.get("value") if isinstance(body, dict) else None
                if not isinstance(name, str) or not NAME_RE.match(name):
                    return self._reply(400, {"ok": False, "error": "invalid signal name"})
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return self._reply(400, {"ok": False, "error": "value must be a finite number"})
                if not link.send_signal(name, value):
                    return self._reply(503, {"ok": False, "error": "MCU not connected"})
                return self._reply(200, {"ok": True})

            if path == "/api/port":
                port = body.get("port") if isinstance(body, dict) else None
                if port:  # only ports the OS actually lists can be opened from the UI
                    if not isinstance(port, str) or port not in {p["device"] for p in available_ports()}:
                        return self._reply(400, {"ok": False, "error": "unknown serial port"})
                link.request_port(port)
                return self._reply(200, {"ok": True})

            if path == "/api/layout":
                if not isinstance(body, dict) or not isinstance(body.get("controls"), list):
                    return self._reply(400, {"ok": False, "error": "layout must contain a controls list"})
                try:
                    fd, tmp = tempfile.mkstemp(dir=layout_path.parent, suffix=".tmp")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(body, f, indent=2)
                    os.replace(tmp, layout_path)
                except OSError as e:
                    return self._reply(500, {"ok": False, "error": str(e)})
                return self._reply(200, {"ok": True})

            self._reply(404, {"ok": False, "error": "not found"})

    return Handler


class ControlServer:
    def __init__(self, link, host="127.0.0.1", port=8080, layout_path=None):
        here = Path(__file__).resolve().parent
        self.layout_path = Path(layout_path) if layout_path else here / "ui_layout.json"
        self.html_path = here / "control_ui.html"
        self.host, self.port = host, port
        self.httpd = ThreadingHTTPServer(
            (host, port), make_handler(link, self.layout_path, self.html_path, host)
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
