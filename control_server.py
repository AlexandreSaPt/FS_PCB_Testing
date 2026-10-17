"""
control_server.py - tiny local web server for the Teensy control UI.

Serves control_ui.html and exposes:
    GET    /api/status          -> {"connected", "port", "requested", "error"}
    GET    /api/ports           -> [{"device": "COM5", "description": "..."}]
    POST   /api/port            -> {"port": "COM5"} selects the serial port (null disconnects)
    POST   /api/send            -> {"name": "kp", "value": 1.5} forwarded to the MCU

    GET    /api/layouts         -> {"layouts": [{"name", "controls", "modified"}], "active": name|null}
    GET    /api/layouts/<name>  -> the preset (JSON)
    POST   /api/layouts/<name>  -> save a preset (?new=1 -> 409 if it already exists)
    DELETE /api/layouts/<name>  -> delete a preset
    POST   /api/active          -> {"name": "teensy40"} remember which preset is open

Presets are plain .json files in the layouts folder (one file per preset).
Used by foxglove_bridge.py (which owns the serial port). Needs only pyserial.
"""

import json
import math
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import serial.tools.list_ports

MAX_BODY = 1_000_000
MAX_CONTROLS = 500
NAME_RE = re.compile(r"^[A-Za-z0-9_./-]{1,24}$")  # signal names; 24 = MAX_NAME_LEN on the MCU
# Preset names become file names: no path separators, no trailing dot/space (Windows)
LAYOUT_NAME_RE = re.compile(r"^\w(?:[\w .()+-]{0,58}[\w)+-])?$")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


# ----------------------------------------------------------------- presets
class LayoutStore:
    """One JSON file per UI preset inside `directory`."""

    def __init__(self, directory, initial=None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.active = None
        remembered = None
        try:
            remembered = (self.dir / ".last").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        for cand in (self._strip(initial), remembered):  # command line beats the remembered one
            if cand and self.exists(cand):
                self.active = cand
                break

    @staticmethod
    def _strip(name):
        if name and name.lower().endswith(".json"):
            name = name[:-5]
        return name or None

    @staticmethod
    def valid_name(name) -> bool:
        return (isinstance(name, str) and bool(LAYOUT_NAME_RE.match(name))
                and name.split(".")[0].lower() not in RESERVED)

    def path(self, name) -> Path:
        if not self.valid_name(name):
            raise ValueError("invalid preset name")
        return self.dir / f"{name}.json"

    def exists(self, name) -> bool:
        try:
            return self.path(name).is_file()
        except ValueError:
            return False

    def list(self):
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda p: p.stem.casefold()):
            if not self.valid_name(p.stem):
                continue
            try:
                n = len(json.loads(p.read_text(encoding="utf-8")).get("controls", []))
            except (OSError, ValueError, AttributeError, TypeError):
                n = None  # unreadable / not a layout file
            out.append({"name": p.stem, "controls": n, "modified": p.stat().st_mtime})
        return out

    def read(self, name):
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def write(self, name, layout, create_only=False):
        path = self.path(name)
        with self._lock:
            if create_only and path.exists():
                raise FileExistsError(name)
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(layout, f, indent=2, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def delete(self, name):
        self.path(name).unlink()
        if self.active == name:
            self.set_active(None)

    def set_active(self, name):
        self.active = name
        try:
            (self.dir / ".last").write_text(name or "", encoding="utf-8")
        except OSError:
            pass


def validate_layout(body):
    if not isinstance(body, dict) or not isinstance(body.get("controls"), list):
        return "layout must be an object with a controls list"
    if len(body["controls"]) > MAX_CONTROLS or not all(isinstance(c, dict) for c in body["controls"]):
        return "invalid controls list"
    return None


# ----------------------------------------------------------------- ports
def available_ports():
    """Serial ports currently present, sorted naturally (COM2 before COM10)."""
    def key(p):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.device)]
    return [{"device": p.device, "description": p.description or ""}
            for p in sorted(serial.tools.list_ports.comports(), key=key)]


# ----------------------------------------------------------------- HTTP
def make_handler(link, store: LayoutStore, html_path: Path, bind_host: str):
    local_only = bind_host in LOCAL_HOSTS
    LAYOUTS = "/api/layouts/"

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

        def _err(self, code, msg):
            self._reply(code, {"ok": False, "error": msg})

        def _request_allowed(self) -> bool:
            """Block DNS-rebinding and cross-site requests to the local server."""
            host = self.headers.get("Host", "")
            if local_only and urlsplit("//" + host).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{host}"

        def _read_json(self):
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._err(415, "expected application/json")
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._err(413, "body too large")
                return None
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                self._err(400, "invalid JSON")
                return None

        def _parsed(self):
            parts = urlsplit(self.path)
            return unquote(parts.path), parse_qs(parts.query)

        # -- routes
        def do_GET(self):
            if not self._request_allowed():
                return self._err(403, "forbidden")
            path, _ = self._parsed()
            if path in ("/", "/index.html"):
                try:
                    return self._reply(200, html_path.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self._err(500, f"missing {html_path.name}")
            if path == "/api/status":
                return self._reply(200, {"connected": link.connected, "port": link.port,
                                         "requested": link.requested, "error": link.error})
            if path == "/api/ports":
                return self._reply(200, available_ports())
            if path == "/api/layouts":
                return self._reply(200, {"layouts": store.list(), "active": store.active})
            if path.startswith(LAYOUTS):
                try:
                    return self._reply(200, store.read(path[len(LAYOUTS):]))
                except json.JSONDecodeError:
                    return self._err(422, "preset file is not valid JSON")
                except ValueError:
                    return self._err(400, "invalid preset name")
                except FileNotFoundError:
                    return self._err(404, "preset not found")
                except OSError as e:
                    return self._err(500, str(e))
            self._err(404, "not found")

        def do_POST(self):
            if not self._request_allowed():
                return self._err(403, "forbidden")
            path, query = self._parsed()
            body = self._read_json()
            if body is None:
                return

            if path == "/api/send":
                name = body.get("name") if isinstance(body, dict) else None
                value = body.get("value") if isinstance(body, dict) else None
                if not isinstance(name, str) or not NAME_RE.match(name):
                    return self._err(400, "invalid signal name")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return self._err(400, "value must be a finite number")
                if not link.send_signal(name, value):
                    return self._err(503, "MCU not connected")
                return self._reply(200, {"ok": True})

            if path == "/api/port":
                port = body.get("port") if isinstance(body, dict) else None
                if port:  # only ports the OS actually lists can be opened from the UI
                    if not isinstance(port, str) or port not in {p["device"] for p in available_ports()}:
                        return self._err(400, "unknown serial port")
                link.request_port(port)
                return self._reply(200, {"ok": True})

            if path == "/api/active":
                name = body.get("name") if isinstance(body, dict) else None
                if name and not store.exists(name):
                    return self._err(404, "preset not found")
                store.set_active(name or None)
                return self._reply(200, {"ok": True})

            if path.startswith(LAYOUTS):
                name = path[len(LAYOUTS):]
                if not store.valid_name(name):
                    return self._err(400, "invalid preset name (letters, digits, space, _ . ( ) + - ; max 60)")
                problem = validate_layout(body)
                if problem:
                    return self._err(400, problem)
                try:
                    store.write(name, body, create_only="new" in query)
                except FileExistsError:
                    return self._err(409, f"a preset named “{name}” already exists")
                except OSError as e:
                    return self._err(500, str(e))
                return self._reply(200, {"ok": True, "name": name})

            self._err(404, "not found")

        def do_DELETE(self):
            if not self._request_allowed():
                return self._err(403, "forbidden")
            path, _ = self._parsed()
            if path.startswith(LAYOUTS):
                try:
                    store.delete(path[len(LAYOUTS):])
                except ValueError:
                    return self._err(400, "invalid preset name")
                except FileNotFoundError:
                    return self._err(404, "preset not found")
                except OSError as e:
                    return self._err(500, str(e))
                return self._reply(200, {"ok": True})
            self._err(404, "not found")

    return Handler


class ControlServer:
    def __init__(self, link, host="127.0.0.1", port=8080, layouts_dir=None, initial_layout=None):
        here = Path(__file__).resolve().parent
        self.store = LayoutStore(layouts_dir or here / "layouts", initial_layout)
        self.html_path = here / "control_ui.html"
        self.host, self.port = host, port
        self.httpd = ThreadingHTTPServer(
            (host, port), make_handler(link, self.store, self.html_path, host)
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
