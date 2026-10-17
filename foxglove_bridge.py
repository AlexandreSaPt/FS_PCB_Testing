#!/usr/bin/env python3
"""
foxglove_bridge.py - read FoxgloveDebug frames from a Teensy over serial and
serve them to Foxglove via a WebSocket server (optionally recording an MCAP).

Usage:
    python foxglove_bridge.py --list
    python foxglove_bridge.py                       # pick the port in the UI dropdown
    python foxglove_bridge.py -p COM5               # Windows
    python foxglove_bridge.py -p /dev/ttyACM0 -b 115200 --record run.mcap

Then in Foxglove: Open connection -> Foxglove WebSocket -> ws://localhost:8765
Control UI (sliders, buttons, ... that send signals to the MCU): http://localhost:8080
UI presets are .json files in the ./layouts folder; pick, create and save them from the UI.
"""

import argparse
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path

import serial
import serial.tools.list_ports
import foxglove

from control_server import ControlServer

MSG_DECLARE = 0x01
MSG_DATA = 0x02
MSG_SET = 0x10  # PC -> MCU: name_len:u8, name, value:f64
MAX_NAME_LEN = 24

# field type id -> (struct code, JSON schema type)
FIELD_TYPES = {
    0: ("?", "boolean"),  # BOOL
    1: ("b", "integer"),  # INT8
    2: ("B", "integer"),  # UINT8
    3: ("h", "integer"),  # INT16
    4: ("H", "integer"),  # UINT16
    5: ("i", "integer"),  # INT32
    6: ("I", "integer"),  # UINT32
    7: ("f", "number"),   # FLOAT32
    8: ("d", "number"),   # FLOAT64
}


# ----------------------------------------------------------------- protocol
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError("zero byte inside COBS block")
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError("COBS block overruns frame")
        out += data[i:end]
        i = end
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx, code = 0, 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def encode_frame(payload: bytes) -> bytes:
    """payload (type | body) -> COBS(payload | crc16) + 0x00 delimiter."""
    crc = crc16(payload)
    return cobs_encode(payload + bytes([crc & 0xFF, crc >> 8])) + b"\x00"


def read_frames(ser: serial.Serial, stop=lambda: False):
    """Yield raw (still COBS-encoded) frames split on 0x00. Returns when stop() is true."""
    buf = bytearray()
    while not stop():
        data = ser.read(max(1, ser.in_waiting))
        if not data:
            continue
        buf += data
        while True:
            idx = buf.find(b"\x00")
            if idx < 0:
                break
            frame = bytes(buf[:idx])
            del buf[: idx + 1]
            if frame:
                yield frame
        if len(buf) > 4096:  # garbage / plain text with no delimiter
            buf.clear()


# ----------------------------------------------------------------- time sync
class DeviceClock:
    """Unwraps the Teensy's 32-bit micros() and maps it onto host time (ns)."""

    def __init__(self):
        self.last = None
        self.unwrapped_us = 0
        self.offset_ns = None

    def to_host_ns(self, us32: int) -> int:
        if self.last is None:
            self.unwrapped_us = us32
        else:
            self.unwrapped_us += (us32 - self.last) & 0xFFFFFFFF
        self.last = us32
        dev_ns = self.unwrapped_us * 1000
        now = time.time_ns()
        # (Re)sync on first sample, or if we drifted > 2 s (e.g. MCU rebooted)
        if self.offset_ns is None or abs(dev_ns + self.offset_ns - now) > 2_000_000_000:
            self.offset_ns = now - dev_ns
        return dev_ns + self.offset_ns


# ----------------------------------------------------------------- topics
class TopicState:
    def __init__(self, topic_id, name, fields):
        self.id = topic_id
        self.name = name if name.startswith("/") else "/" + name
        self.fields = fields  # list of (name, type_id)
        self.signature = (name, tuple(fields))
        self.struct = struct.Struct("<I" + "".join(FIELD_TYPES[t][0] for _, t in fields))

        schema = {
            "type": "object",
            "properties": {n: {"type": FIELD_TYPES[t][1]} for n, t in fields},
        }
        self.channel = foxglove.Channel(
            topic=self.name,
            message_encoding="json",
            schema=foxglove.Schema(
                name=self.name.strip("/").replace("/", "_") or "msg",
                encoding="jsonschema",
                data=json.dumps(schema).encode(),
            ),
        )

    def log(self, values, log_time_ns):
        msg = {}
        for (name, _), v in zip(self.fields, values):
            if isinstance(v, float) and not math.isfinite(v):
                v = None  # NaN/inf are not valid JSON
            msg[name] = v
        self.channel.log(msg, log_time=log_time_ns)


class Bridge:
    def __init__(self, verbose=False):
        self.topics: dict[int, TopicState] = {}
        self.clock = DeviceClock()
        self.verbose = verbose
        self.bad_frames = 0

    def handle_frame(self, encoded: bytes):
        try:
            raw = cobs_decode(encoded)
            if len(raw) < 3:
                raise ValueError("frame too short")
            payload, crc = raw[:-2], raw[-2] | (raw[-1] << 8)
            if crc16(payload) != crc:
                raise ValueError("CRC mismatch")
        except ValueError as e:
            self.bad_frames += 1
            if self.verbose:
                print(f"[warn] dropped frame: {e}")
            return

        msg_type, body = payload[0], payload[1:]
        if msg_type == MSG_DECLARE:
            self.handle_declare(body)
        elif msg_type == MSG_DATA:
            self.handle_data(body)

    def handle_declare(self, body: bytes):
        try:
            topic_id = body[0]
            nlen = body[1]
            name = body[2 : 2 + nlen].decode()
            p = 2 + nlen
            nfields = body[p]
            p += 1
            fields = []
            for _ in range(nfields):
                ftype, flen = body[p], body[p + 1]
                p += 2
                if ftype not in FIELD_TYPES:
                    raise ValueError(f"unknown field type {ftype}")
                fields.append((body[p : p + flen].decode(), ftype))
                p += flen
        except (IndexError, ValueError, UnicodeDecodeError) as e:
            if self.verbose:
                print(f"[warn] bad declaration: {e}")
            return

        sig = (name, tuple(fields))
        existing = self.topics.get(topic_id)
        if existing:
            if existing.signature != sig:
                print(f"[warn] topic id {topic_id} redeclared with a different schema; "
                      "restart the bridge to pick up the change")
            return
        state = TopicState(topic_id, name, fields)
        self.topics[topic_id] = state
        cols = ", ".join(f"{n}" for n, _ in fields)
        print(f"[info] topic {state.name}: {cols}")

    def handle_data(self, body: bytes):
        if not body:
            return
        state = self.topics.get(body[0])
        if state is None:
            return  # not declared yet; declarations are re-sent every second
        if len(body) - 1 != state.struct.size:
            self.bad_frames += 1
            if self.verbose:
                print(f"[warn] size mismatch on {state.name}")
            return
        us, *values = state.struct.unpack(body[1:])
        state.log(values, self.clock.to_host_ns(us))


# ----------------------------------------------------------------- PC -> MCU
class SerialLink:
    """Thread-safe handle to the open serial port, used by the control UI."""

    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()
        self.port = None       # port currently open (None if not connected)
        self.requested = None  # port the user wants (set from the CLI or the UI dropdown)
        self.error = None      # last error for the requested port

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def request_port(self, port):
        """Ask the bridge loop to (re)connect to `port`; None disconnects."""
        self.error = None
        self.requested = port or None

    def attach(self, ser, port):
        with self._lock:
            self._ser, self.port, self.error = ser, port, None

    def detach(self):
        with self._lock:
            self._ser, self.port = None, None

    def send_signal(self, name: str, value: float) -> bool:
        name_b = name.encode()
        if not 0 < len(name_b) <= MAX_NAME_LEN:
            return False
        payload = bytes([MSG_SET, len(name_b)]) + name_b + struct.pack("<d", float(value))
        frame = encode_frame(payload)
        with self._lock:
            if self._ser is None:
                return False
            try:
                self._ser.write(frame)
                return True
            except (serial.SerialException, OSError):
                return False


# ----------------------------------------------------------------- CLI
def list_ports():
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)


def print_ports(ports):
    if not ports:
        print("No serial ports found.")
        return
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device:<20} {p.description}")


def choose_port_interactively() -> str:
    ports = list_ports()
    if not ports:
        sys.exit("No serial ports found. Is the Teensy plugged in?")
    print("Available serial ports:")
    print_ports(ports)
    while True:
        choice = input("Select port number (or type a port name): ").strip()
        if choice.isdigit() and int(choice) < len(ports):
            return ports[int(choice)].device
        if choice:
            return choice


def parse_args():
    ap = argparse.ArgumentParser(description="Teensy serial -> Foxglove bridge")
    ap.add_argument("-p", "--port", help="serial port (e.g. COM5, /dev/ttyACM0). Omit to choose it in the UI dropdown (or in the terminal with --no-ui)")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="baud rate (ignored by Teensy USB serial)")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--host", default="127.0.0.1", help="WebSocket bind address (default 127.0.0.1)")
    ap.add_argument("--ws-port", type=int, default=8765, help="WebSocket port (default 8765)")
    ap.add_argument("--ui-port", type=int, default=8080, help="control UI HTTP port (default 8080)")
    ap.add_argument("--ui-host", default="127.0.0.1",
                    help="control UI bind address (default 127.0.0.1; 0.0.0.0 exposes it to your network!)")
    ap.add_argument("--no-ui", action="store_true", help="don't start the control UI")
    ap.add_argument("--layouts-dir", default=str(Path(__file__).resolve().parent / "layouts"),
                    help="folder with the control UI presets, one .json file each (default: ./layouts)")
    ap.add_argument("--layout", metavar="NAME",
                    help="preset to open at startup, e.g. teensy40 (default: the one used last time)")
    ap.add_argument("--record", metavar="FILE.mcap", help="also record everything to an MCAP file")
    ap.add_argument("-v", "--verbose", action="store_true", help="print dropped/invalid frames")
    return ap.parse_args()


def main():
    args = parse_args()

    if args.list:
        print_ports(list_ports())
        return

    # With the UI enabled you can pick the port from its dropdown; without it, ask here.
    port = args.port
    if not port and args.no_ui:
        port = choose_port_interactively()

    server = foxglove.start_server(name="Teensy", host=args.host, port=args.ws_port)
    print(f"[info] Foxglove WebSocket server: ws://{args.host}:{args.ws_port}")
    writer = foxglove.open_mcap(args.record, allow_overwrite=True) if args.record else None
    if writer:
        print(f"[info] Recording to {args.record}")

    link = SerialLink()
    link.request_port(port)
    ui = None
    if not args.no_ui:
        try:
            ui = ControlServer(link, args.ui_host, args.ui_port, args.layouts_dir, args.layout)
            ui.start()
            print(f"[info] Control UI: http://{args.ui_host}:{args.ui_port}")
            print(f"[info] Presets: {ui.store.dir} ({len(ui.store.list())} found)")
            if args.layout and ui.store.active is None:
                print(f"[warn] Preset '{args.layout}' not found in {ui.store.dir}")
        except OSError as e:
            print(f"[warn] Could not start control UI ({e}); continuing without it")

    bridge = Bridge(verbose=args.verbose)
    if not port:
        print("[info] No serial port selected yet - choose one in the control UI")
    try:
        while True:
            port = link.requested
            if not port:  # disconnected / waiting for the user to pick a port
                time.sleep(0.1)
                continue
            try:
                with serial.Serial(port, args.baud, timeout=0.1, write_timeout=1.0) as ser:
                    print(f"[info] Connected to {port}")
                    bridge.clock = DeviceClock()
                    link.attach(ser, port)
                    try:
                        # returns when the user picks another port (or disconnects)
                        for frame in read_frames(ser, stop=lambda p=port: link.requested != p):
                            bridge.handle_frame(frame)
                    finally:
                        link.detach()
                print(f"[info] Closed {port}")
            except (serial.SerialException, OSError) as e:
                link.error = str(e)
                print(f"[warn] Serial error on {port} ({e}); retrying in 1 s...")
                for _ in range(10):  # retry in 1 s, but react quickly to a port change
                    if link.requested != port:
                        break
                    time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[info] Shutting down")
    finally:
        if ui:
            ui.stop()
        if writer:
            writer.close()
        server.stop()


if __name__ == "__main__":
    main()
