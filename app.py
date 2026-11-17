"""DMM Viewer: live readout, graphs and CSV logging for HP/Agilent 34401A meters.

    python app.py                  app window, real meters (needs Keysight or NI VISA)
    python app.py --demo           simulated meters, no hardware needed
    python app.py --demo --browser UI in your web browser; refresh to see CSS/JS edits
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import meters

VERSION = "0.1.0"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_DIR = HERE / "ui"
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "DMM_Viewer"
LOG_DIR = Path.home() / "Documents" / "DMM_Viewer_logs"
DEFAULT_SETTINGS = {
    "extra_scan": "",
    # Must match the meter's front-panel I/O menu. 34401A factory setting: 9600 baud, even parity.
    "serial": {"baud": 9600, "parity": "even"},
    "panels": [],
    "math": {"enabled": True, "op": "sub", "a": None, "b": None, "graph": True},
    "theme": "dark",
}
THEMES = ("dark", "light", "vfd")
MATH_OPS = ("sub", "add", "mul", "div", "pct", "db")     # must match MATH_OPS in ui/app.js


class CsvLogger:
    """Appends every reading from every panel to one CSV file."""

    COLUMNS = ["timestamp", "elapsed_s", "meter", "address", "function", "value", "unit"]

    def __init__(self):
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._t0 = 0.0
        self.path: Path | None = None
        self.rows = 0

    def start(self) -> None:
        with self._lock:
            if self._file:
                return
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.path = LOG_DIR / f"dmm_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
            # utf-8-sig so Excel shows the Ω sign correctly
            self._file = open(self.path, "w", newline="", encoding="utf-8-sig")
            self._writer = csv.writer(self._file)
            self._writer.writerow(self.COLUMNS)
            self._t0 = time.time()
            self.rows = 0

    def write(self, ts: float, meter: meters.Meter, func: str, value: float | None) -> None:
        with self._lock:
            if not self._writer:
                return
            f = meters.FUNCTIONS[func]
            self._writer.writerow([
                datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                f"{ts - self._t0:.3f}",
                meter.info.get("model", ""),
                meters.short_address(meter.resource),
                f["label"],
                "OL" if value is None else f"{value:.10g}",
                f["unit"],
            ])
            self.rows += 1
            if self.rows % 25 == 0:
                self._file.flush()

    def stop(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
            self._file = self._writer = None

    def state(self) -> dict:
        return {"active": self._file is not None, "rows": self.rows,
                "path": str(self.path) if self.path else ""}


class Api:
    """Everything the UI can call. Public methods are exposed to JavaScript;
    names starting with _ are not."""

    def __init__(self, demo: bool):
        self._demo = demo
        self._lock = threading.RLock()
        self._data_lock = threading.Lock()
        self._seq = 0
        self._next_id = 1
        self._settings_path = DATA_DIR / ("settings_demo.json" if demo else "settings.json")
        self._settings = self._load_settings()
        self._restoring = False
        self._rm = None
        self._backend = "demo" if demo else ""
        self._found: dict[str, dict] = {}
        self._meters: dict[str, meters.Meter] = {}
        self._bus_locks: dict[str, threading.RLock] = {}
        self._log = CsvLogger()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            saved = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update({k: v for k, v in saved.items() if k in settings})
        except (OSError, ValueError):
            pass
        return settings

    def _save_settings(self) -> None:
        if self._restoring:
            return
        with self._lock:
            self._settings["panels"] = [{"resource": m.resource, "config": m.config, "view": m.view}
                                        for m in self._meters.values()]
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = self._settings_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._settings, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._settings_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ VISA plumbing
    def _get_rm(self):
        if self._rm is None:
            import pyvisa
            problems = []
            for backend in ("", "@py"):        # installed VISA first, then pure-Python fallback
                try:
                    self._rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
                    self._backend = "pyvisa-py" if backend else "VISA"
                    break
                except Exception as e:
                    problems.append(meters.error_text(e))
            if self._rm is None:
                raise RuntimeError("No VISA library found. Install Keysight IO Libraries Suite. "
                                   + " / ".join(problems))
        return self._rm

    def _open_transport(self, resource: str):
        if self._demo:
            return meters.FakeTransport(resource)
        return meters.VisaTransport(self._get_rm(), resource, self._settings.get("serial"))

    def _bus_lock(self, resource: str) -> threading.RLock:
        with self._lock:
            return self._bus_locks.setdefault(meters.bus_key(resource), threading.RLock())

    def _serial_resource(self, port: int) -> str:
        return f"ASRLCOM{port}::INSTR" if self._backend == "pyvisa-py" else f"ASRL{port}::INSTR"

    def _parse_extra(self, text: str) -> list[str]:
        """'COM3, 9, GPIB0::5::INSTR' -> VISA resource names."""
        out = []
        for token in re.split(r"[,;\s]+", text or ""):
            if not token:
                continue
            up = token.upper()
            if m := re.fullmatch(r"COM(\d+)", up):
                res = self._serial_resource(int(m.group(1)))
            elif up.isdigit():
                res = f"GPIB0::{int(up)}::INSTR"
            else:
                res = token
            if res not in out:
                out.append(res)
        return out

    def _probe(self, resource: str) -> dict:
        t = self._open_transport(resource)
        try:
            with self._bus_lock(resource):
                t.set_timeout(1.5)
                t.remote()
                idn = t.query("*IDN?")
                try:
                    t.local()       # leave the front panel usable until a panel is added
                except Exception:
                    pass
        finally:
            t.close()
        info = meters.parse_idn(idn)
        info.update(resource=resource, address=meters.short_address(resource),
                    supported="34401" in info["model"])
        return info

    def _on_reading(self, meter: meters.Meter, ts: float, value, func: str) -> None:
        with self._data_lock:
            self._seq += 1
            meter.readings.append((self._seq, ts, value, func))
        self._log.write(ts, meter, func, value)

    def _meter(self, panel_id: str) -> meters.Meter:
        m = self._meters.get(panel_id)
        if m is None:
            raise ValueError("That panel no longer exists.")
        return m

    # ------------------------------------------------------------------ API used by the UI
    def get_info(self) -> dict:
        functions = {k: {"label": f["label"], "unit": f["unit"], "speed": f["speed"],
                         "ranges": [format(r, "g") for r in f["ranges"]]}
                     for k, f in meters.FUNCTIONS.items()}
        return {"version": VERSION, "demo": self._demo, "functions": functions,
                "speeds": meters.SPEEDS, "extra_scan": self._settings.get("extra_scan", ""),
                "math": self._settings.get("math"), "log_dir": str(LOG_DIR),
                "theme": self._settings.get("theme") if self._settings.get("theme") in THEMES else "dark"}

    def set_theme(self, theme: str) -> bool:
        if theme not in THEMES:
            raise ValueError(f"Unknown theme: {theme}")
        self._settings["theme"] = theme
        self._save_settings()
        return True

    def scan(self, extra=None) -> dict:
        if extra is not None:
            self._settings["extra_scan"] = str(extra).strip()
            self._save_settings()
        problems, candidates = [], []
        if self._demo:
            candidates = list(meters.DEMO_METERS)
        else:
            try:
                rm = self._get_rm()
                try:
                    candidates = [r for r in rm.list_resources("?*::INSTR") if r.upper().startswith("GPIB")]
                except Exception:
                    candidates = []           # VISA raises when nothing is found
            except Exception as e:
                problems.append(meters.error_text(e))
        requested = self._parse_extra(self._settings.get("extra_scan", "")) if (self._demo or self._rm) else []
        candidates += [r for r in requested if r not in candidates]

        in_use = {m.resource: m for m in self._meters.values()}
        found = []
        for res in candidates:
            if res in in_use:
                found.append(dict(in_use[res].info))
                continue
            try:
                found.append(self._probe(res))
            except Exception as e:
                if res in requested:
                    problems.append(f"{meters.short_address(res)}: no answer ({meters.error_text(e)})")
        self._found = {f["resource"]: f for f in found}
        return {"meters": found, "problems": problems, "backend": self._backend}

    def startup(self) -> dict:
        """First scan, then re-open the panels from last time."""
        result = self.scan()
        missing = []
        if not self._meters:
            self._restoring = True
            try:
                for saved in self._settings.get("panels", []):
                    res = saved.get("resource", "")
                    if self._found.get(res, {}).get("supported"):
                        try:
                            self.add_panel(res, saved.get("config"), saved.get("view"))
                            continue
                        except Exception:
                            pass
                    missing.append(meters.short_address(res))
            finally:
                self._restoring = False
        result["panels"] = [m.snapshot() for m in self._meters.values()]
        result["missing"] = missing
        return result

    def add_panel(self, resource: str, config=None, view=None) -> dict:
        with self._lock:
            if any(m.resource == resource for m in self._meters.values()):
                raise ValueError("That meter already has a panel.")
            info = self._found.get(resource) or self._probe(resource)
            if not info.get("supported"):
                raise ValueError(f"{info.get('model') or resource} is not a 34401A.")
            panel_id = f"p{self._next_id}"
            self._next_id += 1
            m = meters.Meter(panel_id, resource, info, self._open_transport,
                             self._bus_lock(resource), self._on_reading, config, view)
            self._meters[panel_id] = m
        self._save_settings()
        return m.snapshot()

    def remove_panel(self, panel_id: str) -> bool:
        with self._lock:
            m = self._meters.pop(panel_id, None)
        if m:
            # stopping waits for any reading in progress; don't block the UI on it
            threading.Thread(target=m.stop, daemon=True).start()
            self._save_settings()
        return True

    def configure(self, panel_id: str, config: dict) -> dict:
        cfg = self._meter(panel_id).configure(config)
        self._save_settings()
        return cfg

    def set_panel_view(self, panel_id: str, view: dict) -> dict:
        m = self._meter(panel_id)
        m.view = meters.normalize_view(view)
        self._save_settings()
        return m.view

    def set_running(self, panel_id: str, running: bool) -> bool:
        self._meter(panel_id).set_running(running)
        return True

    def go_local(self, panel_id: str) -> bool:
        self._meter(panel_id).go_local()
        return True

    def get_updates(self, since=0) -> dict:
        since = int(since or 0)
        readings = {}
        with self._data_lock:
            seq = self._seq
            for panel_id, m in list(self._meters.items()):
                rows = []
                for s, ts, value, func in reversed(m.readings):
                    if s <= since:
                        break
                    rows.append([round(ts, 4), value, func])
                if rows:
                    rows.reverse()
                    readings[panel_id] = rows
        panels = {pid: m.status_snapshot() for pid, m in list(self._meters.items())}
        return {"seq": seq, "readings": readings, "panels": panels, "log": self._log.state()}

    def set_math(self, state: dict) -> bool:
        """Math strip: shown or hidden, operation, and which meters are A and B."""
        state = state if isinstance(state, dict) else {}
        op = state.get("op")
        self._settings["math"] = {
            "enabled": bool(state.get("enabled", True)),
            "op": op if op in MATH_OPS else "sub",
            "a": state.get("a") if isinstance(state.get("a"), str) else None,
            "b": state.get("b") if isinstance(state.get("b"), str) else None,
            "graph": bool(state.get("graph", True)),
        }
        self._save_settings()
        return True

    def start_log(self) -> dict:
        self._log.start()
        return self._log.state()

    def stop_log(self) -> dict:
        self._log.stop()
        return self._log.state()

    def open_log_folder(self) -> bool:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(LOG_DIR)
        return True

    # ------------------------------------------------------------------ shutdown
    def _shutdown(self) -> None:
        self._log.stop()
        with self._lock:
            ms = list(self._meters.values())
        threads = [threading.Thread(target=m.stop) for m in ms]   # each returns its meter to local
        for th in threads:
            th.start()
        for th in threads:
            th.join(8)


# ---------------------------------------------------------------------- browser mode

def make_http_server(api: Api, port: int) -> ThreadingHTTPServer:
    """Serves ui/ and forwards /api/<name> calls to Api. Bound to localhost only."""
    types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
             ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
             ".woff2": "font/woff2", ".txt": "text/plain"}
    ui_root = UI_DIR.resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")     # edits show on refresh
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            target = (ui_root / (path.lstrip("/") or "index.html")).resolve()
            if ui_root not in target.parents or not target.is_file():
                self._send(404, b"Not found", "text/plain")
                return
            self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

        def do_POST(self):
            name = self.path.removeprefix("/api/")
            fn = None if name.startswith("_") else getattr(api, name, None)
            if not callable(fn):
                self._send(404, b'{"error": "unknown call"}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                args = json.loads(self.rfile.read(length) or b"[]")
                reply = {"result": fn(*args)}
            except Exception as e:
                reply = {"error": meters.error_text(e)}
            self._send(200, json.dumps(reply).encode(), "application/json")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live viewer for 34401A multimeters.")
    parser.add_argument("--demo", action="store_true", help="use simulated meters (no hardware)")
    parser.add_argument("--browser", action="store_true", help="open the UI in a web browser instead of a window")
    parser.add_argument("--port", type=int, default=8765, help="port for --browser (default 8765)")
    parser.add_argument("--no-open", action="store_true", help="with --browser: don't open a browser tab")
    parser.add_argument("--debug", action="store_true", help="enable the window's developer tools")
    args = parser.parse_args()

    api = Api(demo=args.demo)
    try:
        if args.browser:
            server = make_http_server(api, args.port)
            url = f"http://127.0.0.1:{args.port}/?http=1"
            print(f"DMM Viewer{' (demo)' if args.demo else ''} at {url}   Ctrl+C to stop")
            if not args.no_open:
                webbrowser.open(url)
            server.serve_forever()
        else:
            import webview
            webview.create_window("DMM Viewer" + (" (demo)" if args.demo else ""),
                                  str(UI_DIR / "index.html"), js_api=api,
                                  width=1320, height=880, min_size=(760, 560),
                                  background_color="#0f1115")
            webview.start(debug=args.debug)
    except KeyboardInterrupt:
        pass
    finally:
        api._shutdown()


if __name__ == "__main__":
    main()
