"""Instrument layer for DMM Viewer.

Talks to HP/Agilent 34401A multimeters over GPIB or RS-232 through PyVISA, and
provides a simulated 34401A (FakeTransport) so the app runs without hardware.

Each meter gets its own reader thread. Meters on the same GPIB board share a
lock, so only one of them uses the bus at a time. A reading is taken as
INIT -> wait -> FETC?, and the wait happens *outside* the lock, so two meters
on one bus integrate at the same time instead of taking turns.
"""
from __future__ import annotations

import math
import random
import re
import threading
import time
from collections import deque

OVERLOAD = 9.0e37            # the 34401A reports +9.90000000E+37 on overload
OVERLOAD_VALUE = 9.9e37
LINE_FREQ_HINT = 60.0        # only used to estimate how long a reading takes

FUNCTIONS = {
    "DCV":  {"label": "DC Voltage", "scpi": "VOLT:DC", "unit": "V", "speed": "nplc",
             "ranges": [0.1, 1, 10, 100, 1000]},
    "ACV":  {"label": "AC Voltage", "scpi": "VOLT:AC", "unit": "V", "speed": "acfilter",
             "ranges": [0.1, 1, 10, 100, 750]},
    "DCI":  {"label": "DC Current", "scpi": "CURR:DC", "unit": "A", "speed": "nplc",
             "ranges": [0.01, 0.1, 1, 3]},
    "ACI":  {"label": "AC Current", "scpi": "CURR:AC", "unit": "A", "speed": "acfilter",
             "ranges": [1, 3]},
    "OHM2": {"label": "Resistance 2-wire", "scpi": "RES", "unit": "Ω", "speed": "nplc",
             "ranges": [100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6]},
    "OHM4": {"label": "Resistance 4-wire", "scpi": "FRES", "unit": "Ω", "speed": "nplc",
             "ranges": [100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6]},
    "FREQ": {"label": "Frequency", "scpi": "FREQ", "unit": "Hz", "speed": "aperture", "ranges": []},
    "PER":  {"label": "Period", "scpi": "PER", "unit": "s", "speed": "aperture", "ranges": []},
}
SCPI_TO_FUNC = {f["scpi"]: key for key, f in FUNCTIONS.items()}

# "value" is what gets sent to the meter; "digits" is how many significant
# digits the UI shows for that setting.
SPEEDS = {
    "nplc": [
        {"value": "100", "label": "100 PLC · 6½ digits · slowest", "digits": 7},
        {"value": "10", "label": "10 PLC · 6½ digits", "digits": 7},
        {"value": "1", "label": "1 PLC · 5½ digits", "digits": 6},
        {"value": "0.2", "label": "0.2 PLC · 5½ digits", "digits": 6},
        {"value": "0.02", "label": "0.02 PLC · 4½ digits · fastest", "digits": 5},
    ],
    "acfilter": [
        {"value": "3", "label": "Slow filter · signals ≥ 3 Hz", "digits": 6},
        {"value": "20", "label": "Medium filter · signals ≥ 20 Hz", "digits": 6},
        {"value": "200", "label": "Fast filter · signals ≥ 200 Hz", "digits": 6},
    ],
    "aperture": [
        {"value": "1", "label": "1 s gate · 6½ digits", "digits": 7},
        {"value": "0.1", "label": "0.1 s gate · 5½ digits", "digits": 6},
        {"value": "0.01", "label": "10 ms gate · 4½ digits", "digits": 5},
    ],
}
DEFAULT_SPEED = {"nplc": "10", "acfilter": "20", "aperture": "0.1"}
AC_FILTER_SECONDS = {"3": 7.0, "20": 1.0, "200": 0.6}


# --------------------------------------------------------------------------- helpers

def normalize_config(cfg: dict | None) -> dict:
    """Return a valid {func, range, speed} config, falling back to defaults."""
    cfg = dict(cfg or {})
    func = cfg.get("func") if cfg.get("func") in FUNCTIONS else "DCV"
    f = FUNCTIONS[func]
    rng = str(cfg.get("range") or "AUTO").upper()
    if rng != "AUTO":
        try:
            value = float(rng)
        except ValueError:
            value = None
        rng = format(value, "g") if value in f["ranges"] else "AUTO"
    speed = str(cfg.get("speed"))
    if speed not in [s["value"] for s in SPEEDS[f["speed"]]]:
        speed = DEFAULT_SPEED[f["speed"]]
    return {"func": func, "range": rng, "speed": speed}


def normalize_view(view: dict | None) -> dict:
    """Panel display options that don't touch the meter."""
    view = view if isinstance(view, dict) else {}
    return {"graph": bool(view.get("graph", True))}


def config_commands(cfg: dict) -> list[str]:
    """SCPI commands that put a 34401A into the given configuration."""
    f = FUNCTIONS[cfg["func"]]
    scpi = f["scpi"]
    cmds = [f"CONF:{scpi}"]   # also sets trigger source IMM and sample count 1
    if f["ranges"]:
        cmds.append(f"{scpi}:RANG:AUTO ON" if cfg["range"] == "AUTO" else f"{scpi}:RANG {cfg['range']}")
    if f["speed"] == "nplc":
        cmds.append(f"{scpi}:NPLC {cfg['speed']}")
    elif f["speed"] == "acfilter":
        cmds.append(f"DET:BAND {cfg['speed']}")
    else:
        cmds.append(f"{scpi}:APER {cfg['speed']}")
    return cmds


def estimate_seconds(cfg: dict) -> float:
    """Rough time for one reading. Only used to schedule bus access."""
    kind = FUNCTIONS[cfg["func"]]["speed"]
    if kind == "nplc":
        return 2 * float(cfg["speed"]) / LINE_FREQ_HINT + 0.004   # autozero doubles it
    if kind == "acfilter":
        return AC_FILTER_SECONDS.get(cfg["speed"], 1.0)
    return float(cfg["speed"]) + 0.01


def parse_idn(idn: str) -> dict:
    parts = [p.strip() for p in idn.strip().split(",")] + ["", "", "", ""]
    return {"manufacturer": parts[0], "model": parts[1], "serial": parts[2],
            "firmware": parts[3], "idn": idn.strip()}


def is_serial(resource: str) -> bool:
    return resource.upper().startswith("ASRL")


def bus_key(resource: str) -> str:
    """Meters with the same key share one lock (same GPIB board)."""
    r = resource.upper()
    return r.split("::")[0] if r.startswith("GPIB") else r


def short_address(resource: str) -> str:
    r = resource.upper()
    m = re.match(r"GPIB(\d*)::(\d+)", r)
    if m:
        board = m.group(1)
        return f"GPIB {m.group(2)}" + (f" (board {board})" if board not in ("", "0") else "")
    m = re.match(r"ASRL(?:COM)?(\d+)", r)
    if m:
        return f"COM{m.group(1)}"
    return resource


def error_text(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) < 220 else text[:217] + "…"


# --------------------------------------------------------------------------- transports

class VisaTransport:
    """A real 34401A on GPIB or RS-232."""

    def __init__(self, rm, resource: str, serial_cfg: dict | None = None):
        self.resource = resource
        self.is_serial = is_serial(resource)
        self.inst = rm.open_resource(resource)
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"       # RS-232 replies end in CR LF; strip() drops the CR
        self.inst.timeout = 3000
        if self.is_serial:
            self._setup_serial(serial_cfg or {})

    def _setup_serial(self, cfg: dict) -> None:
        from pyvisa import constants as C
        parity = str(cfg.get("parity", "even")).lower()
        self.inst.baud_rate = int(cfg.get("baud", 9600))
        self.inst.parity = {"none": C.Parity.none, "even": C.Parity.even, "odd": C.Parity.odd}[parity]
        self.inst.data_bits = 8 if parity == "none" else 7
        self.inst.stop_bits = C.StopBits.two
        try:
            self.inst.flow_control = C.ControlFlow.dtr_dsr   # the 34401A's handshake
        except Exception:
            pass

    def set_timeout(self, seconds: float) -> None:
        self.inst.timeout = int(seconds * 1000)

    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip()

    def remote(self) -> None:
        # Over RS-232 the meter must be told to go remote before anything else.
        # Over GPIB it goes remote by itself when addressed.
        if self.is_serial:
            self.write("SYST:REM")

    def local(self) -> None:
        if self.is_serial:
            self.write("SYST:LOC")              # RS-232 only on the 34401A
        else:
            from pyvisa import constants as C
            self.inst.control_ren(C.RENLineOperation.address_gtl)   # GPIB Go To Local

    def close(self) -> None:
        try:
            self.inst.close()
        except Exception:
            pass


# Demo meters. Address 24 has older firmware, like the real bench.
DEMO_METERS = {
    "GPIB0::22::INSTR": {"idn": "HEWLETT-PACKARD,34401A,0,11-5-2", "terminals": "FRON",
                         "offset": 0.0, "phase": 0.0},
    "GPIB0::24::INSTR": {"idn": "HEWLETT-PACKARD,34401A,0,7-5-2", "terminals": "FRON",
                         "offset": -9e-5, "phase": 1.3},
    "ASRL3::INSTR":     {"idn": "Agilent Technologies,34401A,0,11-5-3", "terminals": "REAR",
                         "offset": 4e-5, "phase": 2.6},
}
DEMO_SIGNALS = {"DCV": 5.0, "ACV": 1.2, "DCI": 0.0123, "ACI": 0.25,
                "OHM2": 1000.4, "OHM4": 999.85, "FREQ": 1000.0, "PER": 1e-3}
DEMO_FAULT_RATE = 1 / 1500       # chance per reading of a simulated timeout
DEMO_SPIKE_RATE = 1 / 500        # chance per reading of an overload


class FakeTransport:
    """Simulated 34401A that understands the SCPI subset this app uses.

    Unknown or malformed commands go into the error queue, like on the real
    meter, so a typo in the app shows up in demo mode too.
    """

    def __init__(self, resource: str):
        if resource not in DEMO_METERS:
            raise IOError(f"No demo meter at {resource}")
        self.resource = resource
        self.is_serial = is_serial(resource)
        self._p = DEMO_METERS[resource]
        self._errors: deque[str] = deque(maxlen=20)
        self._remote = not self.is_serial
        self._cfg = normalize_config(None)
        self._init_at: float | None = None
        self.timeout = 3.0

    # same interface as VisaTransport
    def set_timeout(self, seconds): self.timeout = seconds
    def write(self, cmd): self._handle(cmd)
    def remote(self): self.is_serial and self.write("SYST:REM")
    def local(self): self.is_serial and self.write("SYST:LOC")
    def close(self): pass

    def query(self, cmd):
        reply = self._handle(cmd)
        if reply is None:
            time.sleep(min(self.timeout, 1.0))
            raise IOError(f"VI_ERROR_TMO: no reply to {cmd!r} (simulated)")
        return reply

    def _err(self, code: int, msg: str) -> None:
        self._errors.append(f'{code:+d},"{msg}"')

    def _handle(self, cmd: str):
        c = " ".join(cmd.strip().upper().split())
        time.sleep(0.02 if self.is_serial else 0.002)       # transfer time
        if c in ("SYST:REM", "SYST:LOC"):
            if self.is_serial:
                self._remote = c == "SYST:REM"
            else:
                self._err(514, "Command allowed only with RS-232")
            return None
        if self.is_serial and not self._remote:
            raise IOError("Meter not in remote mode: send SYST:REM first (simulated)")
        if c == "*IDN?":
            return self._p["idn"]
        if c == "*CLS":
            self._errors.clear()
            return None
        if c == "SYST:ERR?":
            return self._errors.popleft() if self._errors else '+0,"No error"'
        if c == "ROUT:TERM?":
            return self._p["terminals"]
        if c == "INIT":
            self._init_at = time.time()
            return None
        if c == "FETC?":
            return self._fetch()

        m = re.fullmatch(r"CONF:([A-Z:]+)", c)
        if m:
            if m.group(1) not in SCPI_TO_FUNC:
                self._err(-113, "Undefined header")
            else:
                self._cfg = normalize_config({"func": SCPI_TO_FUNC[m.group(1)]})
                self._init_at = None
            return None
        m = re.fullmatch(r"([A-Z:]+):(RANG:AUTO|RANG|NPLC|APER) (\S+)", c) or \
            re.fullmatch(r"(DET):(BAND) (\S+)", c)
        if not m:
            self._err(-113, "Undefined header")
            return None
        target, setting, arg = m.groups()
        if target == "DET":
            target = FUNCTIONS[self._cfg["func"]]["scpi"]
        if target not in SCPI_TO_FUNC:
            self._err(-113, "Undefined header")
            return None
        func = SCPI_TO_FUNC[target]
        f = FUNCTIONS[func]
        if func != self._cfg["func"]:
            return None          # real meter stores it for later; nothing to show here
        if setting == "RANG:AUTO":
            if arg not in ("ON", "OFF", "1", "0"):
                self._err(-224, "Illegal parameter value")
            elif arg in ("ON", "1"):
                self._cfg["range"] = "AUTO"
        elif setting == "RANG":
            try:
                want = float(arg)
                fits = [r for r in f["ranges"] if r >= want * 0.9999]
                if not fits:
                    raise ValueError
                self._cfg["range"] = format(fits[0], "g")
            except ValueError:
                self._err(-222, "Data out of range")
        else:   # NPLC, APER, BAND
            kind = {"NPLC": "nplc", "APER": "aperture", "BAND": "acfilter"}[setting]
            allowed = {s["value"]: s["value"] for s in SPEEDS[kind]}
            try:
                key = format(float(arg), "g")
            except ValueError:
                key = None
            if f["speed"] != kind or key not in allowed:
                self._err(-224, "Illegal parameter value")
            else:
                self._cfg["speed"] = key
        return None

    def _fetch(self):
        if self._init_at is None:
            self._err(-230, "Data stale")
            return None
        ready_at = self._init_at + estimate_seconds(self._cfg)
        wait = ready_at - time.time()
        if wait > self.timeout:
            time.sleep(self.timeout)
            raise IOError("VI_ERROR_TMO: timeout expired (simulated)")
        if wait > 0:
            time.sleep(wait)
        self._init_at = None
        if random.random() < DEMO_FAULT_RATE:
            raise IOError("VI_ERROR_TMO: timeout expired (simulated fault)")
        return f"{self._value():+.8E}"

    def _value(self) -> float:
        cfg, p = self._cfg, self._p
        func = cfg["func"]
        kind = FUNCTIONS[func]["speed"]
        noise = {"nplc": lambda: 4e-6 / math.sqrt(float(cfg["speed"])),
                 "acfilter": lambda: 6e-5,
                 "aperture": lambda: 2e-6 / float(cfg["speed"])}[kind]()
        t = time.time()
        wander = 3e-5 * math.sin(t / 23 + p["phase"]) + 1.5e-5 * math.sin(t / 4.1 + 2 * p["phase"])
        value = DEMO_SIGNALS[func] * (1 + p["offset"] + wander + random.gauss(0, noise))
        if random.random() < DEMO_SPIKE_RATE:
            return OVERLOAD_VALUE
        if cfg["range"] != "AUTO" and abs(value) > 1.2 * float(cfg["range"]):
            return OVERLOAD_VALUE
        return value


# --------------------------------------------------------------------------- meter + reader thread

class Meter:
    """One connected meter, driven by its own reader thread.

    All instrument I/O happens on that thread; other threads only set flags
    (new config, pause/resume, return to local) and read the results.
    """

    def __init__(self, panel_id, resource, info, opener, bus_lock, sink, config=None, view=None):
        self.id = panel_id
        self.resource = resource
        self.info = info
        self.view = normalize_view(view)     # UI-only settings (graph shown or not)
        self.readings: deque = deque(maxlen=5000)   # (seq, timestamp, value|None, func)
        self.config = normalize_config(config)
        self.running = True
        self.status = "connecting"
        self.message = ""
        self.terminals = ""
        self._open = opener
        self._lock = bus_lock
        self._sink = sink
        self._pending = True
        self._active = self.config
        self._warning = ""
        self._local_request = False
        self._in_local = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"meter-{panel_id}", daemon=True)
        self._thread.start()

    # ---- called from the UI side
    def configure(self, cfg: dict) -> dict:
        self.config = normalize_config(cfg)
        self._pending = True
        self._wake.set()
        return self.config

    def set_running(self, running: bool) -> None:
        self.running = bool(running)
        self._wake.set()

    def go_local(self) -> None:
        self.running = False
        self._local_request = True
        self._wake.set()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)

    def status_snapshot(self) -> dict:
        return {"status": self.status, "message": self.message,
                "terminals": self.terminals, "running": self.running}

    def snapshot(self) -> dict:
        address = short_address(self.resource)
        return {"id": self.id, "resource": self.resource, "address": address,
                "model": self.info.get("model", ""), "manufacturer": self.info.get("manufacturer", ""),
                "firmware": self.info.get("firmware", ""), "name": f"{self.info.get('model') or 'DMM'} @ {address}",
                "serial": is_serial(self.resource), "config": self.config, "view": self.view,
                **self.status_snapshot()}

    # ---- reader thread
    def _set(self, status: str, message: str = "") -> None:
        self.status, self.message = status, message

    def _apply(self, t, cfg: dict) -> None:
        with self._lock:
            t.set_timeout(max(3.0, estimate_seconds(cfg) * 1.5 + 2))
            for cmd in config_commands(cfg):
                t.write(cmd)
            term = t.query("ROUT:TERM?")
            errors = []
            for _ in range(10):
                err = t.query("SYST:ERR?")
                try:
                    if int(err.split(",")[0]) == 0:
                        break
                except ValueError:
                    pass
                errors.append(err)
        self.terminals = "Rear" if "REAR" in term.upper() else "Front"
        self._warning = ("Meter reported: " + "; ".join(errors)) if errors else ""
        self._active = cfg

    def _run(self) -> None:
        t = None
        while not self._stop.is_set():
            try:
                if t is None:
                    self._set("connecting")
                    with self._lock:
                        t = self._open(self.resource)
                        t.set_timeout(3.0)
                        t.remote()
                        t.write("*CLS")
                    self._in_local = False
                    self._pending = True

                if self._local_request:
                    self._local_request = False
                    try:
                        with self._lock:
                            t.local()
                        self._in_local = True
                        self._set("local", "Front panel unlocked. Press Resume to take control again.")
                    except Exception as e:
                        self._set("paused", f"Return to Local failed: {error_text(e)}")

                if not self.running:
                    if self.status not in ("local", "paused"):
                        self._set("paused")
                    self._wake.wait(0.5)
                    self._wake.clear()
                    continue

                if self._in_local:
                    with self._lock:
                        t.remote()
                    self._in_local = False
                    self._pending = True     # someone may have changed settings on the front panel

                if self._pending:
                    self._pending = False
                    self._apply(t, self.config)

                cfg = self._active
                est = estimate_seconds(cfg)
                with self._lock:
                    t.write("INIT")
                    started = time.time()
                if self._stop.wait(est * 0.9):
                    break
                with self._lock:
                    raw = t.query("FETC?")
                value = float(raw)
                self._sink(self, started + est / 2, None if abs(value) >= OVERLOAD else value, cfg["func"])
                if self.status != "running" or self.message != self._warning:
                    self._set("running", self._warning)
            except Exception as e:
                self._set("error", error_text(e) + " Retrying…")
                if t is not None:
                    t.close()
                t = None
                self._stop.wait(2.0)

        if t is not None:
            try:
                with self._lock:
                    t.local()
            except Exception:
                pass
            t.close()
