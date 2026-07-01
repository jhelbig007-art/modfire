#!/usr/bin/env python3
"""
modfire_web.py — Single-file local web app for Modbus TCP.

Run it:
    python3 modfire_web.py
Then open the URL it prints (default http://127.0.0.1:8080) in your browser.

It serves a self-contained, multi-page web UI (HTML/CSS/JS embedded below):

  * Monitor — continuously read Holding Registers (FC03) and watch values
              update in real time.
  * Control — fire coils (FC05): turn ON/OFF or pulse (ON, wait, OFF).
  * Scan    — discover Modbus modules: find gateways on the network (open
              port 502) and probe a gateway for responding RS485 slave IDs.

All updates stream to the page over Server-Sent Events. No third-party
packages required — Python standard library only.
"""

import json
import platform
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Bind to all interfaces so the page is reachable from other devices on the LAN
# (e.g. a tablet or another PC). Set to "127.0.0.1" to restrict to this machine.
HOST = "0.0.0.0"
PORT = 8080

# Filled in at startup with this PC's reachable IP addresses (for display).
SERVER_INFO = {"ips": [], "port": PORT}

# --- Default configuration (matches the original modpoll/modfire scripts) ---
MONITOR_DEFAULTS = {
    "target_ip": "192.168.23.201",
    "target_port": 502,
    "slave_id": 1,
    "register_addr": 0,
    "num_registers": 16,
    "interval": 2.0,
    "timeout": 4.0,
}

CONTROL_DEFAULTS = {
    "target_ip": "192.168.23.200",
    "target_port": 502,
    "slave_id": 1,
    "timeout": 4.0,
    "pulse": 5.0,
    "watch_start": 0,
    "watch_count": 16,
    "watch_interval": 1.0,
    "stale_on_threshold": 10.0,
    "stale_off_threshold": 10.0,
}

SCAN_DEFAULTS = {
    "net_base": "192.168.23",
    "net_start": 1,
    "net_end": 254,
    "net_port": 502,
    "net_timeout": 0.3,
    "dev_ip": "192.168.23.200",
    "dev_port": 502,
    "dev_slave_start": 1,
    "dev_slave_end": 32,
    "dev_register": 0,
    "dev_timeout": 0.6,
}


# ---------------------------------------------------------------------------
# Modbus TCP frame helpers
# ---------------------------------------------------------------------------
def build_read_packet(transaction_id, slave_id, start_register, count):
    """Modbus TCP frame for FC03 (Read Holding Registers)."""
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + b"\x00\x06"
        + bytes([slave_id & 0xFF])
        + b"\x03"
        + start_register.to_bytes(2, "big")
        + count.to_bytes(2, "big")
    )


def build_write_coil_packet(transaction_id, slave_id, coil_address, turn_on):
    """Modbus TCP frame for FC05 (Write Single Coil). FF00 = ON, 0000 = OFF."""
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + b"\x00\x06"
        + bytes([slave_id & 0xFF])
        + b"\x05"
        + coil_address.to_bytes(2, "big")
        + (b"\xff\x00" if turn_on else b"\x00\x00")
    )


def build_read_coils_packet(transaction_id, slave_id, start_coil, count):
    """Modbus TCP frame for FC01 (Read Coils)."""
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + b"\x00\x06"
        + bytes([slave_id & 0xFF])
        + b"\x01"
        + start_coil.to_bytes(2, "big")
        + count.to_bytes(2, "big")
    )


EXCEPTION_MEANINGS = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Slave Device Failure",
    5: "Acknowledge",
    6: "Slave Device Busy",
    7: "Negative Acknowledge",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Failed to Respond",
}


def parse_read_response(response):
    """Return list of registers from an FC03 reply. Raises ValueError on a bad frame."""
    if len(response) < 9:
        raise ValueError(f"Short frame ({len(response)} bytes): {response.hex().upper()}")
    fc = response[7]
    if fc == 0x83:
        code = response[8] if len(response) > 8 else 0
        raise ValueError(f"Modbus exception {code}: {EXCEPTION_MEANINGS.get(code, 'Unknown')}")
    if fc != 0x03:
        raise ValueError(f"Unexpected response (hex): {response.hex().upper()}")
    byte_count = response[8]
    if len(response) < 9 + byte_count:
        raise ValueError(f"Incomplete packet: expected {9 + byte_count} bytes, got {len(response)}")
    return [
        int.from_bytes(response[9 + i:11 + i], "big")
        for i in range(0, byte_count, 2)
    ]


def parse_write_coil_response(response):
    """Return ('ok'|'exception', detail) for an FC05 reply. Raises ValueError if malformed."""
    if len(response) >= 12 and response[7] == 0x05:
        return "ok", None
    if len(response) >= 9 and response[7] == 0x85:
        code = response[8]
        return "exception", f"{code}: {EXCEPTION_MEANINGS.get(code, 'Unknown')}"
    raise ValueError(f"Unexpected response (hex): {response.hex().upper()}")


def parse_read_coils_response(response, count):
    """Return a list of `count` booleans from an FC01 reply. Raises ValueError on a bad frame."""
    if len(response) < 9:
        raise ValueError(f"Short frame ({len(response)} bytes): {response.hex().upper()}")
    fc = response[7]
    if fc == 0x81:
        code = response[8] if len(response) > 8 else 0
        raise ValueError(f"Modbus exception {code}: {EXCEPTION_MEANINGS.get(code, 'Unknown')}")
    if fc != 0x01:
        raise ValueError(f"Unexpected response (hex): {response.hex().upper()}")
    byte_count = response[8]
    if len(response) < 9 + byte_count:
        raise ValueError(f"Incomplete packet: expected {9 + byte_count} bytes, got {len(response)}")
    data = response[9:9 + byte_count]
    return [bool((data[i // 8] >> (i % 8)) & 1) for i in range(count)]


# Standard object IDs returned by FC43 / MEI Type 14 (Read Device Identification).
DEVID_OBJECTS = {
    0x00: "vendor",        # VendorName  (the "make")
    0x01: "product_code",  # ProductCode
    0x02: "revision",      # MajorMinorRevision
    0x03: "vendor_url",
    0x04: "product_name",  # ProductName (often the "model")
    0x05: "model_name",    # ModelName
    0x06: "app_name",      # UserApplicationName
}


def read_device_identification(ip, port, slave, timeout):
    """Best-effort Modbus FC43 / MEI-14 'Read Device Identification'.

    Returns a dict like {"vendor": "...", "product_name": "...", ...}, or {} if
    the device doesn't support it. Uses its own short-lived socket so a failure
    here never disturbs the main scan connection.
    """
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
    except Exception:
        return found
    tid = 0
    try:
        # Object access levels: 0x01 = basic (objs 0-2), 0x02 = regular (objs 3-6).
        for code, start in ((0x01, 0x00), (0x02, 0x03)):
            obj_id = start
            for _ in range(8):  # follow the "more follows" continuation flag
                tid = (tid % 0xFFFF) + 1
                pdu = bytes([0x2B, 0x0E, code, obj_id])
                frame = (tid.to_bytes(2, "big") + b"\x00\x00"
                         + (1 + len(pdu)).to_bytes(2, "big") + bytes([slave & 0xFF]) + pdu)
                try:
                    s.send(frame)
                    resp = s.recv(1024)
                except Exception:
                    return found
                if len(resp) < 8 or resp[7] == 0xAB:  # exception => unsupported level
                    break
                if resp[7] != 0x2B or len(resp) < 14:
                    break
                more, next_id, num = resp[11], resp[12], resp[13]
                idx = 14
                for _ in range(num):
                    if idx + 2 > len(resp):
                        break
                    oid, olen = resp[idx], resp[idx + 1]
                    val = resp[idx + 2:idx + 2 + olen]
                    idx += 2 + olen
                    name = DEVID_OBJECTS.get(oid, f"obj_{oid}")
                    found[name] = val.decode("ascii", "replace").strip()
                if more == 0xFF:
                    obj_id = next_id
                else:
                    break
    finally:
        try:
            s.close()
        except Exception:
            pass
    return found


def derive_make_model(devid):
    """Pick a human 'make' and 'model' from a device-identification dict."""
    make = (devid.get("vendor") or "").strip()
    model = (devid.get("model_name") or devid.get("product_name")
             or devid.get("product_code") or "").strip()
    return make, model


# ---------------------------------------------------------------------------
# Out-of-band identification (for IP devices like gateways that don't do FC43):
# resolve the manufacturer from the MAC address and grab any HTTP banner.
# ---------------------------------------------------------------------------
# A small seed of OUI prefixes common in Ethernet/RS485 gateways. Anything not
# here is looked up online (best effort) and the raw MAC is always shown too.
OUI_DB = {
    "0008dc": "WIZnet",          # W5500 chips used by many RS485-to-ETH modules
    "00e04c": "Realtek",
    "001963": "Atop Technologies",
    "0090e8": "Moxa",
    "00c0a8": "Moxa",
    "001b1b": "Advantech",
    "74fe48": "Espressif",
    "240ac4": "Espressif",
    "30aea4": "Espressif",
    "a4cf12": "Espressif",
    "b827eb": "Raspberry Pi",
    "dca632": "Raspberry Pi",
}
_oui_cache = {}
_oui_lock = threading.Lock()


def get_mac_address(ip):
    """Return the MAC for an IP from the local ARP/neighbour table, or ''."""
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                    return parts[3].lower()
    except Exception:
        pass
    for cmd in (["ip", "neigh", "show", ip], ["arp", "-n", ip]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
            m = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", out)
            if m:
                return m.group(1).lower()
        except Exception:
            pass
    return ""


def lookup_oui_vendor(mac, allow_online=True, timeout=1.5):
    """Resolve a MAC's manufacturer via the seed table, then an online OUI API."""
    if not mac:
        return ""
    prefix = mac.replace(":", "").replace("-", "").lower()[:6]
    if prefix in OUI_DB:
        return OUI_DB[prefix]
    with _oui_lock:
        if prefix in _oui_cache:
            return _oui_cache[prefix]
    vendor = ""
    if allow_online:
        try:
            req = urllib.request.Request("https://api.macvendors.com/" + mac,
                                         headers={"User-Agent": "ModFire"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                name = r.read().decode("utf-8", "replace").strip()
                if name and "error" not in name.lower() and "<" not in name:
                    vendor = name
        except Exception:
            vendor = ""
    with _oui_lock:
        _oui_cache[prefix] = vendor
    return vendor


def http_banner(ip, port=80, timeout=1.0):
    """Best-effort: return a model hint from a device's web UI (title/Server)."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.send(f"GET / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: ModFire\r\n\r\n".encode())
        data = b""
        while len(data) < 8192:
            chunk = s.recv(2048)
            if not chunk:
                break
            data += chunk
        text = data.decode("latin-1", "replace")
        title = re.search(r"(?is)<title>(.*?)</title>", text)
        if title:
            t = re.sub(r"\s+", " ", title.group(1)).strip()
            if t:
                return t
        server = re.search(r"(?im)^Server:\s*(.+)$", text)
        if server:
            return server.group(1).strip()
        return ""
    except Exception:
        return ""
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def identify_ip_device(ip, modbus_port, fc43_timeout):
    """Combine FC43, MAC/OUI and HTTP banner into (make, model, mac)."""
    devid = {}
    for uid in (1, 0, 255):
        devid = read_device_identification(ip, modbus_port, uid, fc43_timeout)
        if devid:
            break
    make, model = derive_make_model(devid)
    mac = get_mac_address(ip)
    if not make and mac:
        make = lookup_oui_vendor(mac)
    if not model:
        model = http_banner(ip)
    return make, model, mac


# ---------------------------------------------------------------------------
# Event hub: broadcasts events to all connected SSE clients, per channel
# ---------------------------------------------------------------------------
class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()
        self._last_status = {}  # channel -> last status event

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
            snapshot = list(self._last_status.values())
        for ev in snapshot:
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event):
        event.setdefault("channel", "monitor")
        if event.get("type") == "status":
            self._last_status[event["channel"]] = event
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass  # Drop for slow/full clients rather than block.


HUB = EventHub()


# ---------------------------------------------------------------------------
# Monitor: poller thread that reads registers on an interval
# ---------------------------------------------------------------------------
class Poller(threading.Thread):
    def __init__(self, config):
        super().__init__(daemon=True)
        self.config = config
        self._stop_event = threading.Event()
        self._tid = 0

    def stop(self):
        self._stop_event.set()

    def _next_tid(self):
        self._tid = (self._tid % 0xFFFF) + 1
        return self._tid

    def run(self):
        cfg = self.config
        ip, port = cfg["target_ip"], int(cfg["target_port"])
        slave, start = int(cfg["slave_id"]), int(cfg["register_addr"])
        count, interval, timeout = int(cfg["num_registers"]), float(cfg["interval"]), float(cfg["timeout"])

        poll_count = success_count = error_count = 0
        sock = None

        HUB.publish({"channel": "monitor", "type": "log", "level": "info",
                     "message": f"Connecting to gateway at {ip}:{port} ..."})
        HUB.publish({"channel": "monitor", "type": "status", "state": "connecting",
                     "message": f"Connecting to {ip}:{port}"})
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            HUB.publish({"channel": "monitor", "type": "log", "level": "ok",
                         "message": "TCP link active. (Waveshare blue light should be SOLID.)"})
            HUB.publish({"channel": "monitor", "type": "status", "state": "running",
                         "message": f"Polling {count} register(s) from addr {start} on slave {slave}"})
        except Exception as e:
            HUB.publish({"channel": "monitor", "type": "log", "level": "error",
                         "message": f"Connection failed: {e}"})
            HUB.publish({"channel": "monitor", "type": "status", "state": "error",
                         "message": f"Connection failed: {e}"})
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            return

        try:
            while not self._stop_event.is_set():
                poll_count += 1
                ts = time.time()
                try:
                    sock.send(build_read_packet(self._next_tid(), slave, start, count))
                    response = sock.recv(1024)
                    if not response:
                        raise ConnectionError("Connection closed by remote host")
                    registers = parse_read_response(response)
                    success_count += 1
                    HUB.publish({"channel": "monitor", "type": "data", "ts": ts, "start": start,
                                 "registers": registers, "poll_count": poll_count,
                                 "success_count": success_count, "error_count": error_count,
                                 "raw": response.hex().upper()})
                except socket.timeout:
                    error_count += 1
                    HUB.publish({"channel": "monitor", "type": "log", "level": "warn", "ts": ts,
                                 "message": "No reply from downstream RS485 device "
                                            "(check baud rate / wiring / slave ID)"})
                    HUB.publish({"channel": "monitor", "type": "stats", "poll_count": poll_count,
                                 "success_count": success_count, "error_count": error_count})
                except (ConnectionError, OSError) as e:
                    error_count += 1
                    HUB.publish({"channel": "monitor", "type": "log", "level": "error", "ts": ts,
                                 "message": f"Link error: {e}. Stopping."})
                    HUB.publish({"channel": "monitor", "type": "status", "state": "error",
                                 "message": f"Link error: {e}"})
                    break
                except ValueError as e:
                    error_count += 1
                    HUB.publish({"channel": "monitor", "type": "log", "level": "error",
                                 "ts": ts, "message": str(e)})
                    HUB.publish({"channel": "monitor", "type": "stats", "poll_count": poll_count,
                                 "success_count": success_count, "error_count": error_count})

                slept = 0.0
                while slept < interval and not self._stop_event.is_set():
                    chunk = min(0.1, interval - slept)
                    time.sleep(chunk)
                    slept += chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass
            HUB.publish({"channel": "monitor", "type": "log", "level": "info",
                         "message": "Socket closed safely."})
            if self._stop_event.is_set():
                HUB.publish({"channel": "monitor", "type": "status", "state": "idle",
                             "message": "Stopped"})


class PollerManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._poller = None

    def start(self, config):
        with self._lock:
            self._stop_locked()
            self._poller = Poller(config)
            self._poller.start()

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        if self._poller and self._poller.is_alive():
            self._poller.stop()
            self._poller.join(timeout=6.0)
        self._poller = None

    def is_running(self):
        with self._lock:
            return bool(self._poller and self._poller.is_alive())


MANAGER = PollerManager()


# ---------------------------------------------------------------------------
# Coil history: remembers each coil's state changes for the last 30 minutes
# ---------------------------------------------------------------------------
COIL_HISTORY_WINDOW = 1800  # seconds


class CoilHistory:
    def __init__(self):
        self._lock = threading.Lock()
        self._hist = {}  # coil -> list of (ts, state_bool), transitions only

    def record(self, coil, state, ts):
        state = bool(state)
        with self._lock:
            lst = self._hist.setdefault(int(coil), [])
            if not lst or lst[-1][1] != state:
                lst.append((ts, state))
            self._prune_locked(int(coil), ts)

    def _prune_locked(self, coil, now):
        cutoff = now - COIL_HISTORY_WINDOW
        lst = self._hist.get(coil)
        if not lst:
            return
        # Keep the last transition before the window (the state at window start)
        # plus everything inside the window.
        last_before = -1
        for i, (ts, _) in enumerate(lst):
            if ts < cutoff:
                last_before = i
            else:
                break
        if last_before > 0:
            self._hist[coil] = lst[last_before:]

    def get(self, coil, now):
        with self._lock:
            self._prune_locked(int(coil), now)
            return list(self._hist.get(int(coil), []))

    def summary(self, now):
        with self._lock:
            for c in list(self._hist):
                self._prune_locked(c, now)
            return {c: len(v) for c, v in self._hist.items() if v}


COILS_HISTORY = CoilHistory()


# ---------------------------------------------------------------------------
# Control: persistent connection used to write coils (FC05)
# ---------------------------------------------------------------------------
class ControlConnection:
    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self._tid = 0
        self.config = {}
        self._watch_thread = None
        self._watch_stop = threading.Event()

    def _next_tid(self):
        self._tid = (self._tid % 0xFFFF) + 1
        return self._tid

    def is_connected(self):
        with self._lock:
            return self._sock is not None

    def is_watching(self):
        t = self._watch_thread
        return bool(t and t.is_alive())

    def connect(self, cfg):
        self.stop_watch()
        ok = False
        with self._lock:
            self._close_locked()
            ip, port = cfg["target_ip"], int(cfg["target_port"])
            timeout = float(cfg["timeout"])
            HUB.publish({"channel": "control", "type": "log", "level": "info",
                         "message": f"Connecting to gateway at {ip}:{port} ..."})
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((ip, port))
                self._sock = s
                self.config = dict(cfg)
                ok = True
                HUB.publish({"channel": "control", "type": "log", "level": "ok",
                             "message": "TCP link active. Ready to fire coils."})
                HUB.publish({"channel": "control", "type": "status", "state": "running",
                             "message": f"Connected to {ip}:{port} (slave {cfg['slave_id']})"})
            except Exception as e:
                self._sock = None
                HUB.publish({"channel": "control", "type": "log", "level": "error",
                             "message": f"Connection failed: {e}"})
                HUB.publish({"channel": "control", "type": "status", "state": "error",
                             "message": f"Connection failed: {e}"})
        if ok:
            # Auto-start watching coil status right away.
            self.start_watch(cfg)
        return ok

    def disconnect(self):
        self.stop_watch()
        with self._lock:
            self._close_locked()
        HUB.publish({"channel": "control", "type": "log", "level": "info",
                     "message": "Socket closed safely."})
        HUB.publish({"channel": "control", "type": "status", "state": "idle",
                     "message": "Disconnected"})

    def _close_locked(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None

    def write_coil(self, coil, turn_on):
        """Send FC05 and publish the result. Returns True on success."""
        action = f"Coil {coil} {'ON' if turn_on else 'OFF'}"
        with self._lock:
            if not self._sock:
                HUB.publish({"channel": "control", "type": "log", "level": "error",
                             "message": f"Not connected — cannot set {action}."})
                return False
            slave = int(self.config.get("slave_id", 1))
            try:
                self._sock.send(build_write_coil_packet(self._next_tid(), slave, coil, turn_on))
                response = self._sock.recv(1024)
                if not response:
                    raise ConnectionError("Connection closed by remote host")
                status, detail = parse_write_coil_response(response)
            except socket.timeout:
                HUB.publish({"channel": "control", "type": "log", "level": "warn",
                             "message": f"{action}: no reply from downstream device "
                                        "(check wiring / baud rate)"})
                return False
            except (ConnectionError, OSError) as e:
                self._close_locked()
                HUB.publish({"channel": "control", "type": "log", "level": "error",
                             "message": f"{action}: link error: {e}"})
                HUB.publish({"channel": "control", "type": "status", "state": "error",
                             "message": f"Link error: {e}"})
                return False
            except ValueError as e:
                HUB.publish({"channel": "control", "type": "log", "level": "error",
                             "message": f"{action}: {e}"})
                return False

        if status == "ok":
            write_ts = time.time()
            COILS_HISTORY.record(coil, turn_on, write_ts)
            HUB.publish({"channel": "control", "type": "log", "level": "ok",
                         "message": f"Success: {action}"})
            HUB.publish({"channel": "control", "type": "coil", "coil": coil,
                         "state": turn_on, "ts": write_ts})
            return True
        HUB.publish({"channel": "control", "type": "log", "level": "error",
                     "message": f"{action}: device rejected (exception {detail})"})
        return False

    def pulse(self, coil, duration):
        """ON, wait `duration` seconds (streaming a countdown), then OFF."""
        def _run():
            if not self.write_coil(coil, True):
                return
            HUB.publish({"channel": "control", "type": "log", "level": "info",
                         "message": f"Coil {coil}: ON — turning OFF in {duration:g}s ..."})
            total = round(duration, 1)
            end = time.monotonic() + duration
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                if not self.is_connected():
                    HUB.publish({"channel": "control", "type": "log", "level": "warn",
                                 "message": f"Coil {coil}: connection lost during pulse."})
                    HUB.publish({"channel": "control", "type": "countdown", "coil": coil,
                                 "remaining": 0, "total": total, "done": True})
                    return
                HUB.publish({"channel": "control", "type": "countdown", "coil": coil,
                             "remaining": round(remaining, 1), "total": total})
                time.sleep(min(0.2, remaining))
            HUB.publish({"channel": "control", "type": "countdown", "coil": coil,
                         "remaining": 0, "total": total, "done": True})
            self.write_coil(coil, False)
        threading.Thread(target=_run, daemon=True).start()

    # --- Coil status watching (FC01 Read Coils) -------------------------
    def start_watch(self, cfg):
        self.stop_watch()
        if not self.is_connected():
            HUB.publish({"channel": "control", "type": "log", "level": "error",
                         "message": "Connect before watching coil status."})
            return False
        start = int(cfg.get("watch_start", 0))
        count = max(1, min(2000, int(cfg.get("watch_count", 16))))
        interval = max(0.1, float(cfg.get("watch_interval", 1.0)))
        self.config.update({"watch_start": start, "watch_count": count,
                            "watch_interval": interval})
        self._watch_stop = threading.Event()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(start, count, interval), daemon=True)
        self._watch_thread.start()
        HUB.publish({"channel": "control", "type": "watch", "watching": True,
                     "start": start, "count": count})
        HUB.publish({"channel": "control", "type": "log", "level": "info",
                     "message": f"Watching coils {start}–{start + count - 1} "
                                f"every {interval:g}s (FC01)."})
        return True

    def stop_watch(self):
        self._watch_stop.set()
        t = self._watch_thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._watch_thread = None

    def _watch_loop(self, start, count, interval):
        last_error = None
        while not self._watch_stop.is_set():
            ts = time.time()
            states = None
            err = None
            fatal = False
            with self._lock:
                if not self._sock:
                    break
                slave = int(self.config.get("slave_id", 1))
                try:
                    self._sock.send(build_read_coils_packet(self._next_tid(), slave, start, count))
                    resp = self._sock.recv(1024)
                    if not resp:
                        raise ConnectionError("Connection closed by remote host")
                    states = parse_read_coils_response(resp, count)
                except socket.timeout:
                    err = "no reply (does this device support Read Coils / FC01?)"
                except (ConnectionError, OSError) as e:
                    err = f"link error: {e}"
                    fatal = True
                    self._close_locked()
                except ValueError as e:
                    err = str(e)
            if states is not None:
                for i, st in enumerate(states):
                    COILS_HISTORY.record(start + i, st, ts)
                HUB.publish({"channel": "control", "type": "coil_status", "start": start,
                             "states": states, "ts": ts})
                last_error = None
            elif err and err != last_error:
                HUB.publish({"channel": "control", "type": "log", "level": "warn",
                             "message": f"Coil watch: {err}"})
                last_error = err
                if fatal:
                    HUB.publish({"channel": "control", "type": "status", "state": "error",
                                 "message": f"Link error: {err}"})
                    break
            slept = 0.0
            while slept < interval and not self._watch_stop.is_set():
                chunk = min(0.1, interval - slept)
                time.sleep(chunk)
                slept += chunk
        HUB.publish({"channel": "control", "type": "watch", "watching": False})


CONTROL = ControlConnection()


# ---------------------------------------------------------------------------
# Scan: discover gateways on the network and slave IDs on a gateway
# ---------------------------------------------------------------------------
class ScanManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()

    def is_running(self):
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._stop_event.set()

    def start_network(self, cfg):
        self._start(self._scan_network, cfg)

    def start_devices(self, cfg):
        self._start(self._scan_devices, cfg)

    def _start(self, target, cfg):
        with self._lock:
            if self._thread and self._thread.is_alive():
                HUB.publish({"channel": "scan", "type": "log", "level": "warn",
                             "message": "A scan is already running."})
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=target, args=(cfg,), daemon=True)
            self._thread.start()

    # --- Network scan: find hosts with port 502 open --------------------
    def _scan_network(self, cfg):
        base = cfg["net_base"].strip().rstrip(".")
        start, end = int(cfg["net_start"]), int(cfg["net_end"])
        port, timeout = int(cfg["net_port"]), float(cfg["net_timeout"])
        hosts = [f"{base}.{i}" for i in range(start, min(end, 254) + 1)]
        total = len(hosts)
        found = 0
        scanned = 0
        lock = threading.Lock()

        HUB.publish({"channel": "scan", "type": "scan_status", "state": "running",
                     "kind": "network",
                     "message": f"Scanning {base}.{start}–{end} port {port} ..."})
        HUB.publish({"channel": "scan", "type": "scan_begin", "kind": "network", "total": total})

        def probe(ip):
            nonlocal found, scanned
            open_port = False
            if not self._stop_event.is_set():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                try:
                    if s.connect_ex((ip, port)) == 0:
                        open_port = True
                except Exception:
                    open_port = False
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
            make = model = mac = ""
            if open_port and not self._stop_event.is_set():
                make, model, mac = identify_ip_device(ip, port, max(timeout, 0.8))
            with lock:
                scanned += 1
                if open_port:
                    found += 1
                    HUB.publish({"channel": "scan", "type": "scan_found", "kind": "network",
                                 "ip": ip, "port": port, "make": make, "model": model, "mac": mac})
                HUB.publish({"channel": "scan", "type": "scan_progress", "kind": "network",
                             "scanned": scanned, "total": total, "found": found})

        # Bounded concurrency for a fast sweep.
        sem = threading.BoundedSemaphore(64)
        threads = []

        def worker(ip):
            with sem:
                probe(ip)

        for ip in hosts:
            if self._stop_event.is_set():
                break
            t = threading.Thread(target=worker, args=(ip,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        stopped = self._stop_event.is_set()
        HUB.publish({"channel": "scan", "type": "scan_done", "kind": "network",
                     "found": found, "scanned": scanned, "total": total, "stopped": stopped})
        HUB.publish({"channel": "scan", "type": "scan_status",
                     "state": "idle" if not stopped else "idle",
                     "kind": "network",
                     "message": (f"Stopped — {found} gateway(s) found"
                                 if stopped else f"Done — {found} gateway(s) found")})

    # --- Device scan: probe slave IDs on one gateway --------------------
    def _scan_devices(self, cfg):
        ip, port = cfg["dev_ip"], int(cfg["dev_port"])
        s_start, s_end = int(cfg["dev_slave_start"]), int(cfg["dev_slave_end"])
        reg, timeout = int(cfg["dev_register"]), float(cfg["dev_timeout"])
        slaves = list(range(max(1, s_start), min(247, s_end) + 1))
        total = len(slaves)
        found = scanned = 0
        tid = 0

        HUB.publish({"channel": "scan", "type": "scan_status", "state": "running", "kind": "device",
                     "message": f"Probing slaves {s_start}–{s_end} on {ip}:{port} ..."})
        HUB.publish({"channel": "scan", "type": "scan_begin", "kind": "device", "total": total})

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
        except Exception as e:
            HUB.publish({"channel": "scan", "type": "log", "level": "error",
                         "message": f"Could not connect to gateway {ip}:{port}: {e}"})
            HUB.publish({"channel": "scan", "type": "scan_done", "kind": "device",
                         "found": 0, "scanned": 0, "total": total, "stopped": False})
            HUB.publish({"channel": "scan", "type": "scan_status", "state": "error", "kind": "device",
                         "message": f"Connect failed: {e}"})
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            return

        try:
            for slave in slaves:
                if self._stop_event.is_set():
                    break
                tid = (tid % 0xFFFF) + 1
                status, detail = "timeout", "no response"
                try:
                    sock.send(build_read_packet(tid, slave, reg, 1))
                    resp = sock.recv(1024)
                    if not resp:
                        raise ConnectionError("connection closed")
                    if len(resp) >= 9 and resp[7] == 0x03:
                        status, detail = "present", "responded with data"
                    elif len(resp) >= 9 and resp[7] == 0x83:
                        code = resp[8]
                        # An exception still proves a device is answering at this unit id.
                        status = "present"
                        detail = f"exception {code}: {EXCEPTION_MEANINGS.get(code, 'Unknown')}"
                    else:
                        status, detail = "other", resp.hex().upper()
                except socket.timeout:
                    status, detail = "timeout", "no response"
                except (ConnectionError, OSError) as e:
                    HUB.publish({"channel": "scan", "type": "log", "level": "error",
                                 "message": f"Link error during device scan: {e}"})
                    break

                scanned += 1
                make = model = ""
                if status == "present":
                    found += 1
                    devid = read_device_identification(ip, port, slave, max(timeout, 0.8))
                    make, model = derive_make_model(devid)
                    if make or model:
                        HUB.publish({"channel": "scan", "type": "log", "level": "ok",
                                     "message": f"Slave {slave}: {make or '?'} "
                                                f"{model or ''}".rstrip()})
                HUB.publish({"channel": "scan", "type": "scan_device", "slave": slave,
                             "status": status, "detail": detail,
                             "make": make, "model": model})
                HUB.publish({"channel": "scan", "type": "scan_progress", "kind": "device",
                             "scanned": scanned, "total": total, "found": found})
        finally:
            try:
                sock.close()
            except Exception:
                pass

        stopped = self._stop_event.is_set()
        HUB.publish({"channel": "scan", "type": "scan_done", "kind": "device",
                     "found": found, "scanned": scanned, "total": total, "stopped": stopped})
        HUB.publish({"channel": "scan", "type": "scan_status", "state": "idle", "kind": "device",
                     "message": (f"Stopped — {found} slave(s) found"
                                 if stopped else f"Done — {found} slave(s) responding")})


SCAN = ScanManager()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE)
        elif self.path == "/config":
            self._json(200, {
                "monitor": dict(MONITOR_DEFAULTS, running=MANAGER.is_running()),
                "control": dict(CONTROL_DEFAULTS, connected=CONTROL.is_connected(),
                                watching=CONTROL.is_watching()),
                "scan": dict(SCAN_DEFAULTS, running=SCAN.is_running()),
                "server": {"ips": SERVER_INFO["ips"], "port": PORT},
            })
        elif self.path.startswith("/control/history"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            now = time.time()
            if "coil" in q:
                coil = int(q["coil"][0])
                entries = [{"ts": ts, "state": st} for ts, st in COILS_HISTORY.get(coil, now)]
                self._json(200, {"coil": coil, "now": now,
                                 "window": COIL_HISTORY_WINDOW, "entries": entries})
            else:
                self._json(200, {"now": now, "window": COIL_HISTORY_WINDOW,
                                 "summary": COILS_HISTORY.summary(now)})
        elif self.path == "/stream":
            self._stream()
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            if self.path == "/monitor/start":
                cfg = dict(MONITOR_DEFAULTS, **self._body())
                cfg["target_port"] = int(cfg["target_port"])
                cfg["slave_id"] = int(cfg["slave_id"])
                cfg["register_addr"] = int(cfg["register_addr"])
                cfg["num_registers"] = max(1, min(125, int(cfg["num_registers"])))
                cfg["interval"] = max(0.05, float(cfg["interval"]))
                cfg["timeout"] = max(0.2, float(cfg["timeout"]))
                MANAGER.start(cfg)
                self._json(200, {"ok": True})
            elif self.path == "/monitor/stop":
                MANAGER.stop()
                self._json(200, {"ok": True})
            elif self.path == "/control/connect":
                cfg = dict(CONTROL_DEFAULTS, **self._body())
                cfg["target_port"] = int(cfg["target_port"])
                cfg["slave_id"] = int(cfg["slave_id"])
                cfg["timeout"] = max(0.2, float(cfg["timeout"]))
                ok = CONTROL.connect(cfg)
                self._json(200, {"ok": ok})
            elif self.path == "/control/disconnect":
                CONTROL.disconnect()
                self._json(200, {"ok": True})
            elif self.path == "/control/watch":
                cfg = dict(CONTROL_DEFAULTS, **self._body())
                ok = CONTROL.start_watch(cfg)
                self._json(200, {"ok": ok})
            elif self.path == "/control/unwatch":
                CONTROL.stop_watch()
                HUB.publish({"channel": "control", "type": "log", "level": "info",
                             "message": "Stopped watching coil status."})
                self._json(200, {"ok": True})
            elif self.path == "/control/coil":
                body = self._body()
                coil = int(body["coil"])
                action = str(body.get("action", "on")).lower()
                if action == "fire":
                    duration = max(0.1, float(body.get("pulse", CONTROL_DEFAULTS["pulse"])))
                    CONTROL.pulse(coil, duration)
                else:
                    CONTROL.write_coil(coil, action == "on")
                self._json(200, {"ok": True})
            elif self.path == "/scan/network":
                cfg = dict(SCAN_DEFAULTS, **self._body())
                SCAN.start_network(cfg)
                self._json(200, {"ok": True})
            elif self.path == "/scan/devices":
                cfg = dict(SCAN_DEFAULTS, **self._body())
                SCAN.start_devices(cfg)
                self._json(200, {"ok": True})
            elif self.path == "/scan/stop":
                SCAN.stop()
                self._json(200, {"ok": True})
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _stream(self):
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            import queue
            while True:
                try:
                    event = q.get(timeout=15.0)
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)


# ---------------------------------------------------------------------------
# Embedded single-file front end (multi-page SPA)
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ModFire — Modbus TCP Console</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2a3140;
    --text:#e6edf3; --muted:#8b949e; --accent:#1f6feb; --accent2:#58a6ff;
    --ok:#3fb950; --warn:#d29922; --err:#f85149;
    --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  header{display:flex;align-items:center;gap:14px;padding:12px 20px;
    border-bottom:1px solid var(--line);background:linear-gradient(180deg,#161b22,#0d1117);
    position:sticky;top:0;z-index:10}
  header .flame{font-size:22px}
  header h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.3px}
  nav{display:flex;gap:6px;margin-left:8px}
  nav button{padding:7px 14px;border-radius:8px;border:1px solid var(--line);
    background:var(--panel);color:var(--muted);cursor:pointer;font-size:14px;font-weight:600}
  nav button.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .srv{margin-left:auto;font-size:12px;color:var(--muted);font-family:var(--mono);
    padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
  .srv b{color:var(--accent2);font-weight:600}
  .badge{margin-left:14px;display:flex;align-items:center;gap:8px;font-size:13px;
    padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel)}
  .badge #stateTime{color:var(--muted);font-family:var(--mono);font-size:11px;
    border-left:1px solid var(--line);padding-left:8px;margin-left:2px}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
  .dot.idle{background:var(--muted)} .dot.connecting{background:var(--warn);animation:pulse 1s infinite}
  .dot.running{background:var(--ok);animation:pulse 1.4s infinite} .dot.error{background:var(--err)}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 8px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  main{display:grid;grid-template-columns:330px 1fr;gap:16px;padding:16px;align-items:start}
  @media(max-width:900px){main{grid-template-columns:1fr}}
  .page{display:none}
  .page.active{display:contents}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  .panel + .panel{margin-top:16px}
  .panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);
    margin:0 0 12px;font-weight:600}
  .field{margin-bottom:12px}
  .field label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
  .field input{width:100%;padding:8px 10px;border-radius:7px;border:1px solid var(--line);
    background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;outline:none}
  .field input:focus{border-color:var(--accent2)}
  .row{display:flex;gap:10px}
  .row .field{flex:1}
  .btns{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap}
  button.act{flex:1;min-width:80px;padding:10px;border-radius:7px;border:1px solid var(--line);
    cursor:pointer;font-size:14px;font-weight:600;color:var(--text);background:var(--panel2)}
  button.act:hover{border-color:var(--accent2)}
  button.act.primary{background:var(--accent);border-color:var(--accent)}
  button.act.on{background:#1a7f37;border-color:#1a7f37}
  button.act.off{background:#3d2222;border-color:#5c2b2b}
  button.act.fire{background:#9e6a00;border-color:#9e6a00}
  button.act:disabled{opacity:.45;cursor:not-allowed}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
  .stat{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px;text-align:center}
  .stat .num{font-size:24px;font-weight:700;font-family:var(--mono)}
  .stat .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
  .stat.ok .num{color:var(--ok)} .stat.err .num{color:var(--err)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px}
  .reg{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 12px;
    transition:background .35s,border-color .35s}
  .reg.flash{background:#15324a;border-color:var(--accent2)}
  .reg .addr{font-size:11px;color:var(--muted);font-family:var(--mono)}
  .reg .val{font-size:21px;font-weight:700;font-family:var(--mono);margin-top:3px}
  .reg .hex{font-size:11px;color:var(--accent2);font-family:var(--mono);margin-top:2px}
  .coil{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px;text-align:center}
  .coil .cidx{font-size:12px;color:var(--muted);font-family:var(--mono);margin-bottom:6px}
  .coil .light{width:100%;height:8px;border-radius:4px;background:#30363d;margin-bottom:8px;transition:.25s}
  .coil.on .light{background:var(--ok);box-shadow:0 0 10px rgba(63,185,80,.7)}
  .coil .mini{display:flex;gap:5px}
  .coil .mini button{flex:1;padding:5px 0;font-size:12px;border-radius:6px;border:1px solid var(--line);
    background:var(--bg);color:var(--text);cursor:pointer;font-weight:600}
  .coil .mini button:hover{border-color:var(--accent2)}
  .coil{position:relative}
  .coil .cd{position:absolute;top:6px;right:8px;font-size:11px;font-weight:700;font-family:var(--mono);
    color:var(--warn);display:none}
  .coil.counting .cd{display:block}
  .coil.counting{border-color:var(--warn)}
  .coil.stale{border-color:var(--err);background:#2a1216;animation:stalepulse 2s infinite}
  .coil.stale .light{background:#402022}
  @keyframes stalepulse{0%{box-shadow:0 0 0 0 rgba(248,81,73,.45)}70%{box-shadow:0 0 0 6px rgba(248,81,73,0)}100%{box-shadow:0 0 0 0 rgba(248,81,73,0)}}
  .stale-badge{display:none;font-size:10px;font-weight:700;color:var(--err);margin-top:6px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .coil.stale .stale-badge{display:block}
  .state-time{font-size:11px;font-family:var(--mono);color:var(--muted);margin-top:6px}
  .state-time.on{color:var(--ok)}
  .watchflag{font-size:11px;color:var(--muted);margin:4px 0 2px}
  .watchflag b{color:var(--ok)}
  .timer{display:flex;align-items:center;gap:12px;background:var(--panel2);border:1px solid var(--warn);
    border-radius:9px;padding:10px 14px;margin-bottom:12px}
  .timer .big{font-size:26px;font-weight:700;font-family:var(--mono);color:var(--warn);min-width:74px}
  .timer .txt{flex:1}
  .timer .txt .t1{font-weight:600}
  .timer .txt .t2{font-size:12px;color:var(--muted)}
  .timer .ring{height:8px;border-radius:5px;background:#30363d;overflow:hidden;margin-top:6px}
  .timer .ring > div{height:100%;background:var(--warn);transition:width .2s;width:100%}
  .meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-bottom:12px;font-family:var(--mono)}
  .log{height:240px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:9px;
    padding:10px;font-family:var(--mono);font-size:12px;line-height:1.55}
  .log div{white-space:pre-wrap;word-break:break-word}
  .log .t{color:var(--muted)}
  .log .info{color:var(--accent2)} .log .ok{color:var(--ok)} .log .warn{color:var(--warn)} .log .error{color:var(--err)}
  .empty{color:var(--muted);font-size:13px;padding:20px;text-align:center}
  .hint{font-size:11px;color:var(--muted);margin-top:10px;line-height:1.5}
  .bar{height:8px;border-radius:5px;background:var(--panel2);overflow:hidden;margin:8px 0}
  .bar > div{height:100%;width:0;background:var(--accent);transition:width .2s}
  .found{display:flex;flex-direction:column;gap:8px;margin-top:10px}
  .found-item{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:9px 12px;font-family:var(--mono);font-size:13px}
  .found-item{flex-wrap:wrap}
  .found-item .ip{font-weight:700;color:var(--ok)}
  .found-item .mm{color:var(--text);font-weight:400}
  .found-item .mm .muted{color:var(--muted)}
  .found-item .mac{flex-basis:100%;color:var(--muted);font-size:11px;margin-top:2px}
  .found-item button{margin-left:auto;padding:5px 10px;border-radius:6px;border:1px solid var(--line);
    background:var(--bg);color:var(--text);cursor:pointer;font-size:12px;font-weight:600}
  .found-item button:hover{border-color:var(--accent2)}
  .slavegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:10px}
  .slave{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 6px;text-align:center;
    font-family:var(--mono);font-size:13px;color:var(--muted);min-height:34px}
  .slave.present{background:#11301d;border-color:var(--ok);color:var(--ok);font-weight:700}
  .slave.timeout{opacity:.5}
  .slave.other{border-color:var(--warn);color:var(--warn)}
  .slave .mm{display:block;font-size:10px;font-weight:600;color:var(--accent2);margin-top:3px;line-height:1.25;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;
    justify-content:center;z-index:100;padding:20px}
  .modal-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;width:min(560px,100%);
    max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
  .modal-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;
    border-bottom:1px solid var(--line);font-weight:600}
  .modal-head button{background:var(--panel2);border:1px solid var(--line);color:var(--text);
    border-radius:7px;width:30px;height:30px;cursor:pointer;font-size:15px}
  .modal-body{padding:14px 18px;overflow:auto}
  .hsum{color:var(--muted);font-size:12px;margin-bottom:12px}
  table.htab{width:100%;border-collapse:collapse;font-size:13px}
  table.htab th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
    letter-spacing:.5px;padding:6px 8px;border-bottom:1px solid var(--line)}
  table.htab td{padding:7px 8px;border-bottom:1px solid var(--line);font-family:var(--mono)}
  .pill{padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700}
  .pill.on{background:#11301d;color:var(--ok)} .pill.off{background:#30363d;color:var(--muted)}
</style>
</head>
<body>
<header>
  <span class="flame">🔥</span>
  <h1>ModFire — Modbus TCP Console</h1>
  <nav>
    <button data-page="monitor" class="active" onclick="showPage('monitor')">📊 Monitor</button>
    <button data-page="control" onclick="showPage('control')">🎚 Control</button>
    <button data-page="scan" onclick="showPage('scan')">🛰 Scan</button>
  </nav>
  <span class="srv" id="serverInfo" title="This PC's address — browse here from other devices">🖥 —</span>
  <div class="badge"><span id="dot" class="dot idle"></span><span id="stateLbl">Idle</span><span id="stateTime"></span></div>
</header>

<!-- ================= MONITOR ================= -->
<main id="page-monitor" class="page active">
  <section class="panel">
    <h2>Monitor — Read Registers (FC03)</h2>
    <div class="field"><label>Gateway IP</label><input id="m_target_ip"></div>
    <div class="row">
      <div class="field"><label>Port</label><input id="m_target_port"></div>
      <div class="field"><label>Slave ID</label><input id="m_slave_id"></div>
    </div>
    <div class="row">
      <div class="field"><label>Start Register</label><input id="m_register_addr"></div>
      <div class="field"><label># Registers</label><input id="m_num_registers"></div>
    </div>
    <div class="row">
      <div class="field"><label>Interval (s)</label><input id="m_interval"></div>
      <div class="field"><label>Timeout (s)</label><input id="m_timeout"></div>
    </div>
    <div class="btns">
      <button class="act primary" id="m_start" onclick="monitorStart()">▶ Start</button>
      <button class="act" id="m_stop" onclick="monitorStop()" disabled>■ Stop</button>
    </div>
    <div class="hint">Continuously reads Holding Registers and streams results live.
      Config changes apply on the next Start.</div>
  </section>
  <section>
    <div class="panel">
      <h2>Registers</h2>
      <div class="meta"><span id="m_range">—</span><span id="m_time">Last update: —</span></div>
      <div class="stats">
        <div class="stat"><div class="num" id="m_polls">0</div><div class="lbl">Polls</div></div>
        <div class="stat ok"><div class="num" id="m_ok">0</div><div class="lbl">Success</div></div>
        <div class="stat err"><div class="num" id="m_err">0</div><div class="lbl">Errors</div></div>
      </div>
      <div id="m_grid" class="grid"><div class="empty">No data yet — press Start to begin polling.</div></div>
    </div>
    <div class="panel"><h2>Event Log</h2><div id="m_log" class="log"></div></div>
  </section>
</main>

<!-- ================= CONTROL ================= -->
<main id="page-control" class="page">
  <section class="panel">
    <h2>Control — Write Coils (FC05)</h2>
    <div class="field"><label>Gateway IP</label><input id="c_target_ip"></div>
    <div class="row">
      <div class="field"><label>Port</label><input id="c_target_port"></div>
      <div class="field"><label>Slave ID</label><input id="c_slave_id"></div>
    </div>
    <div class="row">
      <div class="field"><label>Timeout (s)</label><input id="c_timeout"></div>
      <div class="field"><label>Pulse (s)</label><input id="c_pulse"></div>
    </div>
    <div class="btns">
      <button class="act primary" id="c_connect" onclick="controlConnect()">🔌 Connect</button>
      <button class="act" id="c_disconnect" onclick="controlDisconnect()" disabled>✕ Disconnect</button>
    </div>
    <div class="hint">Connect once, then fire coils. <b>Pulse</b> turns a coil ON,
      waits the pulse time, then OFF — like the original script.</div>

    <h2 style="margin-top:18px">Coil Bank &amp; Status Watch (FC01)</h2>
    <div class="row">
      <div class="field"><label>Start coil</label><input id="c_watch_start"></div>
      <div class="field"><label># Coils</label><input id="c_watch_count"></div>
    </div>
    <div class="field"><label>Poll interval (s)</label><input id="c_watch_interval"></div>
    <div class="btns">
      <button class="act" id="c_watch" onclick="controlWatch()" disabled data-cdis>👁 Watch</button>
      <button class="act" id="c_unwatch" onclick="controlUnwatch()" disabled>■ Stop watch</button>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field"><label>Flag if last ON was shorter than (s)</label>
        <input id="c_stale_on_threshold"></div>
      <div class="field"><label>Flag if last OFF was shorter than (s)</label>
        <input id="c_stale_off_threshold"></div>
    </div>
    <div class="hint">If a coil's most recent completed ON period was shorter than
      the ON threshold (while it's currently OFF), or its most recent completed
      OFF period was shorter than the OFF threshold (while it's currently ON),
      it's highlighted <span style="color:var(--err);font-weight:600">red</span>
      with the duration — e.g. a coil meant to hold ON for 30s that only stayed
      on 10s, or one that's flapping back ON too soon after being OFF. The flag
      clears once the relevant period lasts long enough. Each coil also shows a
      live <b>ON/OFF</b> timer for its current state. <b># Coils</b> sets how
      many coils appear in the bank
      (starting at <b>Start coil</b>) and, once watching, reads their live status
      via FC01 so the lights reflect the device's real state. Change either field
      and the bank resizes immediately — no need to reconnect. Click a coil's
      name to see its last 30 min of state changes. Up to 2000 coils can be
      watched (Modbus's own limit); the bank display is capped at 512 to stay
      responsive.</div>
  </section>
  <section>
    <div class="panel">
      <h2>Fire a Coil</h2>
      <div id="c_timer" class="timer" style="display:none">
        <div class="big" id="c_timer_num">—</div>
        <div class="txt"><div class="t1" id="c_timer_t1">—</div><div class="t2" id="c_timer_t2"></div>
          <div class="ring"><div id="c_timer_ring"></div></div></div>
      </div>
      <div class="row" style="align-items:flex-end">
        <div class="field" style="flex:0 0 130px"><label>Coil index</label>
          <input id="c_coil" value="0"></div>
        <div class="btns" style="flex:1;margin:0 0 12px">
          <button class="act on" onclick="coil('on')" disabled data-cdis>ON</button>
          <button class="act off" onclick="coil('off')" disabled data-cdis>OFF</button>
          <button class="act fire" onclick="coil('fire')" disabled data-cdis>⚡ Pulse</button>
        </div>
      </div>
      <div class="watchflag" id="c_watchflag">Coil watch: <b>off</b></div>
      <div class="meta"><span id="c_bank_range">Coils 0–15</span><span id="c_bank_note"></span></div>
      <div id="c_bank" class="grid"></div>
    </div>
    <div class="panel"><h2>Command Log</h2><div id="c_log" class="log"></div></div>
  </section>
</main>

<!-- ================= SCAN ================= -->
<main id="page-scan" class="page">
  <section class="panel">
    <h2>Scan — Find Gateways</h2>
    <div class="field"><label>Subnet base (first 3 octets)</label><input id="s_net_base"></div>
    <div class="row">
      <div class="field"><label>From</label><input id="s_net_start"></div>
      <div class="field"><label>To</label><input id="s_net_end"></div>
    </div>
    <div class="row">
      <div class="field"><label>Port</label><input id="s_net_port"></div>
      <div class="field"><label>Timeout (s)</label><input id="s_net_timeout"></div>
    </div>
    <div class="btns">
      <button class="act primary" id="s_net_btn" onclick="scanNetwork()">🛰 Scan network</button>
      <button class="act" onclick="scanStop()">■ Stop</button>
    </div>
    <hr style="border:0;border-top:1px solid var(--line);margin:18px 0">
    <h2>Scan — Find RS485 Slaves on a Gateway</h2>
    <div class="field"><label>Gateway IP</label><input id="s_dev_ip"></div>
    <div class="row">
      <div class="field"><label>Port</label><input id="s_dev_port"></div>
      <div class="field"><label>Probe register</label><input id="s_dev_register"></div>
    </div>
    <div class="row">
      <div class="field"><label>Slave from</label><input id="s_dev_slave_start"></div>
      <div class="field"><label>Slave to</label><input id="s_dev_slave_end"></div>
    </div>
    <div class="field"><label>Timeout (s)</label><input id="s_dev_timeout"></div>
    <div class="btns">
      <button class="act primary" id="s_dev_btn" onclick="scanDevices()">🔎 Probe slaves</button>
      <button class="act" onclick="scanStop()">■ Stop</button>
    </div>
    <div class="hint">Network scan finds hosts with the Modbus port open. Slave scan probes
      each unit ID with a read — a reply (data <i>or</i> exception) proves a module is present.
      ModFire identifies the <b>make / model</b> of found <i>gateways</i> from Modbus FC43,
      the MAC address vendor (OUI), and any web-UI banner. Downstream RS485 slaves have no
      IP/MAC, so they can only be identified if they support FC43.</div>
  </section>
  <section>
    <div class="panel">
      <h2>Network Results</h2>
      <div class="bar"><div id="s_net_bar"></div></div>
      <div class="meta"><span id="s_net_prog">Idle</span><span id="s_net_count">0 found</span></div>
      <div id="s_net_found" class="found"><div class="empty">No gateways found yet.</div></div>
    </div>
    <div class="panel">
      <h2>Slave Results</h2>
      <div class="bar"><div id="s_dev_bar"></div></div>
      <div class="meta"><span id="s_dev_prog">Idle</span><span id="s_dev_count">0 found</span></div>
      <div id="s_dev_grid" class="slavegrid"><div class="empty">No slaves probed yet.</div></div>
    </div>
    <div class="panel"><h2>Scan Log</h2><div id="s_log" class="log"></div></div>
  </section>
</main>

<div id="histModal" class="modal" style="display:none" onclick="closeHistory(event)">
  <div class="modal-box">
    <div class="modal-head"><span id="hist_title">Coil history</span>
      <button onclick="closeHistory(true)" title="Close">✕</button></div>
    <div id="hist_body" class="modal-body"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let es = null;

// ---- field maps (server key -> input id) ----
const M = ["target_ip","target_port","slave_id","register_addr","num_registers","interval","timeout"];
const C = ["target_ip","target_port","slave_id","timeout","pulse",
           "watch_start","watch_count","watch_interval",
           "stale_on_threshold","stale_off_threshold"];
const S = ["net_base","net_start","net_end","net_port","net_timeout",
           "dev_ip","dev_port","dev_register","dev_slave_start","dev_slave_end","dev_timeout"];

function gather(prefix, keys){ const o={}; keys.forEach(k=>o[k]=$(prefix+k).value.trim()); return o; }
function fill(prefix, keys, cfg){ keys.forEach(k=>{ if(cfg[k]!==undefined && $(prefix+k)) $(prefix+k).value=cfg[k]; }); }

function showPage(p){
  document.querySelectorAll(".page").forEach(el=>el.classList.remove("active"));
  $("page-"+p).classList.add("active");
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active", b.dataset.page===p));
  setBadge(p);
}
function nowSec(){ return Date.now()/1000; }
let badges = {
  monitor:{state:"idle",msg:"Idle",since:null},
  control:{state:"idle",msg:"Disconnected",since:null},
  scan:{state:"idle",msg:"Idle",since:null}
};
let current = "monitor";
function setBadge(p){
  current=p; const b=badges[p]||{state:"idle",msg:"Idle",since:null};
  $("dot").className="dot "+b.state; $("stateLbl").textContent=b.msg;
  renderStateTime();
}
function updateBadge(ch, state, msg){
  const prev=badges[ch];
  const changed=!prev || prev.state!==state;
  badges[ch]={state:state, msg:msg||state, since: changed?nowSec():(prev?prev.since:nowSec())};
  if(current===ch) setBadge(ch);
}
function renderStateTime(){
  const b=badges[current];
  $("stateTime").textContent=(b && b.since)?("· "+dur(nowSec()-b.since)):"";
}

function pad(n){return String(n).padStart(2,"0");}
function tstr(ts){const d=ts?new Date(ts*1000):new Date();return pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function addLog(elId, level, message, ts){
  const log=$(elId); const div=document.createElement("div");
  div.innerHTML='<span class="t">['+tstr(ts)+']</span> <span class="'+level+'">'+esc(message)+'</span>';
  log.appendChild(div);
  while(log.childNodes.length>400) log.removeChild(log.firstChild);
  log.scrollTop=log.scrollHeight;
}

// ---------------- MONITOR ----------------
function monitorBtns(running){ $("m_start").disabled=running; $("m_stop").disabled=!running; }
function renderRegs(start, regs, ts){
  const grid=$("m_grid");
  if(grid.dataset.count!=regs.length || grid.dataset.start!=start){
    grid.innerHTML="";
    for(let i=0;i<regs.length;i++){
      const c=document.createElement("div"); c.className="reg"; c.id="reg"+i;
      c.innerHTML='<div class="addr">#'+(start+i)+'</div><div class="val">—</div><div class="hex">0x0000</div>';
      grid.appendChild(c);
    }
    grid.dataset.count=regs.length; grid.dataset.start=start;
  }
  for(let i=0;i<regs.length;i++){
    const c=$("reg"+i); if(!c) continue;
    const v=c.querySelector(".val"), h=c.querySelector(".hex"), nv=String(regs[i]);
    if(v.textContent!==nv){ v.textContent=nv; h.textContent="0x"+regs[i].toString(16).toUpperCase().padStart(4,"0");
      c.classList.add("flash"); setTimeout(()=>c.classList.remove("flash"),350); }
  }
  $("m_range").textContent="Registers "+start+"–"+(start+regs.length-1);
  $("m_time").textContent="Last update: "+tstr(ts);
}
async function monitorStart(){
  monitorBtns(true);
  try{ const r=await fetch("/monitor/start",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(gather("m_",M))}); const j=await r.json();
    if(!j.ok){ addLog("m_log","error",j.error||"Failed to start"); monitorBtns(false);} }
  catch(e){ addLog("m_log","error","Start request failed: "+e); monitorBtns(false); }
}
async function monitorStop(){ try{ await fetch("/monitor/stop",{method:"POST"}); }catch(e){} }

// ---------------- CONTROL ----------------
function controlBtns(connected){
  $("c_connect").disabled=connected; $("c_disconnect").disabled=!connected;
  document.querySelectorAll("[data-cdis]").forEach(b=>b.disabled=!connected);
  if(!connected) watchBtns(false);
}
function watchBtns(watching){
  $("c_watch").disabled=watching || $("c_connect").disabled===false;
  $("c_unwatch").disabled=!watching;
  $("c_watchflag").innerHTML="Coil watch: <b>"+(watching?"on":"off")+"</b>";
}
const BANK_RENDER_CAP = 512;
let bankStart=0, bankCount=16;
function buildBank(){
  let start=parseInt($("c_watch_start").value,10); if(isNaN(start)) start=0;
  let count=parseInt($("c_watch_count").value,10); if(isNaN(count)||count<1) count=1;
  const capped=Math.min(count, BANK_RENDER_CAP);
  bankStart=start; bankCount=capped;
  const bank=$("c_bank"); bank.innerHTML="";
  for(let n=0;n<capped;n++){
    const i=start+n;
    const c=document.createElement("div"); c.className="coil"; c.id="coil"+i;
    c.innerHTML='<div class="cidx" style="cursor:pointer" title="Click for 30-min history" '+
      'onclick="showHistory('+i+')">Coil '+i+' &#128203;</div>'+
      '<div class="cd" id="cd'+i+'"></div><div class="light"></div>'+
      '<div class="state-time" id="st'+i+'">—</div>'+
      '<div class="stale-badge" id="stale'+i+'"></div>'+
      '<div class="mini"><button onclick="bankCoil('+i+',\'on\')">ON</button>'+
      '<button onclick="bankCoil('+i+',\'off\')">OFF</button>'+
      '<button onclick="bankCoil('+i+',\'fire\')">⚡</button></div>';
    bank.appendChild(c);
  }
  $("c_bank_range").textContent="Coils "+start+"–"+(start+capped-1);
  $("c_bank_note").textContent=(count>capped)?("showing first "+capped+" of "+count+" watched"):"";
}
// ---- short-cycle flagging: was the coil's LAST completed ON (or OFF) ----
// ---- period shorter than the configured threshold? ----
let coilKnown={};        // coil -> {state, since}  (current known state + when it started)
let lastOnDuration={};   // coil -> seconds the most recent completed ON period lasted
let lastOffDuration={};  // coil -> seconds the most recent completed OFF period lasted
function setCoilLight(coil, on, ts){
  const c=$("coil"+coil); if(c) c.classList.toggle("on", !!on);
  ts = ts || nowSec();
  on = !!on;
  const prev = coilKnown[coil];
  if(prev === undefined){ coilKnown[coil] = {state:on, since:ts}; return; }
  if(prev.state === on){ return; }  // no transition, leave "since" alone
  if(prev.state === true && on === false){
    lastOnDuration[coil] = Math.max(0, ts - prev.since);
  } else if(prev.state === false && on === true){
    lastOffDuration[coil] = Math.max(0, ts - prev.since);
  }
  coilKnown[coil] = {state:on, since:ts};
}
function parsePositive(id){
  const v=parseFloat($(id).value);
  return (isNaN(v)||v<=0)?null:v;
}
function updateCoilVisuals(){
  const onThr=parsePositive("c_stale_on_threshold");
  const offThr=parsePositive("c_stale_off_threshold");
  for(let n=0;n<bankCount;n++){
    const coil=bankStart+n, el=$("coil"+coil), badge=$("stale"+coil), timeEl=$("st"+coil);
    if(!el) continue;
    const known=coilKnown[coil];
    const isOn=el.classList.contains("on");

    // Live "time in current state" readout.
    if(timeEl){
      if(known===undefined){ timeEl.textContent="—"; timeEl.className="state-time"; }
      else {
        timeEl.textContent=(known.state?"ON ":"OFF ")+dur(nowSec()-known.since);
        timeEl.className="state-time"+(known.state?" on":"");
      }
    }

    // Short-cycle red flag: while OFF, judge the last ON period; while ON,
    // judge the last OFF period (i.e. did it come back on too soon?).
    let flagged=false, text="";
    if(isOn){
      const d=lastOffDuration[coil];
      if(offThr!==null && d!==undefined && d<offThr){
        flagged=true; text="⚠ last OFF only "+dur(d)+" (< "+dur(offThr)+")";
      }
    } else {
      const d=lastOnDuration[coil];
      if(onThr!==null && d!==undefined && d<onThr){
        flagged=true; text="⚠ last ON only "+dur(d)+" (< "+dur(onThr)+")";
      }
    }
    el.classList.toggle("stale", flagged);
    if(badge) badge.textContent=flagged?text:"";
  }
}
function setCoilCountdown(coil, remaining, done){
  const c=$("coil"+coil), cd=$("cd"+coil); if(!c||!cd) return;
  if(done){ c.classList.remove("counting"); cd.textContent=""; }
  else { c.classList.add("counting"); cd.textContent=remaining.toFixed(1)+"s"; }
}
let timerCoils={};
function updateTimer(coil, remaining, total, done){
  if(done){ delete timerCoils[coil]; }
  else { timerCoils[coil]={remaining:remaining, total:total}; }
  const keys=Object.keys(timerCoils);
  const box=$("c_timer");
  if(!keys.length){ box.style.display="none"; return; }
  // Show the coil with the least time remaining.
  let show=keys[0];
  keys.forEach(k=>{ if(timerCoils[k].remaining < timerCoils[show].remaining) show=k; });
  const t=timerCoils[show];
  box.style.display="flex";
  $("c_timer_num").textContent=t.remaining.toFixed(1)+"s";
  $("c_timer_t1").textContent="⚡ Coil "+show+" is ON";
  $("c_timer_t2").textContent="turns OFF in "+t.remaining.toFixed(1)+" s"+
    (keys.length>1?("  (+"+(keys.length-1)+" more pulsing)"):"");
  $("c_timer_ring").style.width=(t.total>0?Math.max(0,Math.min(100,t.remaining/t.total*100)):0)+"%";
}
async function sendCoil(coil, action){
  try{ await fetch("/control/coil",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({coil:coil, action:action, pulse:$("c_pulse").value})}); }
  catch(e){ addLog("c_log","error","Command failed: "+e); }
}
function coil(action){ const v=parseInt($("c_coil").value,10); if(isNaN(v)){addLog("c_log","error","Enter a valid coil index");return;} sendCoil(v,action); }
function bankCoil(i, action){ sendCoil(i, action); }
async function controlConnect(){
  try{ const r=await fetch("/control/connect",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(gather("c_",C))}); const j=await r.json();
    if(!j.ok) addLog("c_log","error",j.error||"Connect failed"); }
  catch(e){ addLog("c_log","error","Connect request failed: "+e); }
}
async function controlDisconnect(){ try{ await fetch("/control/disconnect",{method:"POST"}); }catch(e){} }
async function controlWatch(){
  try{ await fetch("/control/watch",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(gather("c_",C))}); }catch(e){ addLog("c_log","error","Watch failed: "+e); }
}
async function controlUnwatch(){ try{ await fetch("/control/unwatch",{method:"POST"}); }catch(e){} }

// ---- per-coil 30-minute history ----
function fullTime(ts){ const d=new Date(ts*1000);
  return pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds()); }
function dur(sec){ sec=Math.max(0,sec);
  if(sec<60) return sec.toFixed(1)+"s";
  const m=Math.floor(sec/60), s=Math.round(sec%60); return m+"m "+(s<10?"0":"")+s+"s"; }
async function showHistory(coil){
  $("hist_title").textContent="Coil "+coil+" — last 30 min";
  $("hist_body").innerHTML='<div class="empty">Loading…</div>';
  $("histModal").style.display="flex";
  try{
    const j=await (await fetch("/control/history?coil="+coil)).json();
    renderHistory(coil, j);
  }catch(e){ $("hist_body").innerHTML='<div class="empty">Failed to load history.</div>'; }
}
function closeHistory(ev){
  if(ev===true || (ev && ev.target && ev.target.id==="histModal")) $("histModal").style.display="none";
}
function renderHistory(coil, j){
  const now=j.now, entries=j.entries||[];
  if(!entries.length){
    $("hist_body").innerHTML='<div class="empty">No recorded changes in the last 30 min.<br>'+
      'Connect and <b>Watch</b> (or fire this coil) to build history.</div>'; return;
  }
  let onTime=0, onCount=0;
  for(let i=0;i<entries.length;i++){
    const endTs=(i<entries.length-1)?entries[i+1].ts:now;
    if(entries[i].state){ onCount++; onTime+=endTs-entries[i].ts; }
  }
  let html='<div class="hsum">'+entries.length+' change(s) · '+onCount+' ON period(s) · total ON '+
    dur(onTime)+'</div>';
  html+='<table class="htab"><thead><tr><th>Time</th><th>State</th><th>Held for</th></tr></thead><tbody>';
  for(let i=entries.length-1;i>=0;i--){
    const e=entries[i], endTs=(i<entries.length-1)?entries[i+1].ts:now, held=endTs-e.ts;
    html+='<tr><td>'+fullTime(e.ts)+'</td><td><span class="pill '+(e.state?'on':'off')+'">'+
      (e.state?'ON':'OFF')+'</span></td><td>'+dur(held)+(i===entries.length-1?' <span style="color:var(--muted)">(current)</span>':'')+'</td></tr>';
  }
  html+='</tbody></table>';
  $("hist_body").innerHTML=html;
}
window.addEventListener("keydown", e=>{ if(e.key==="Escape") closeHistory(true); });

// ---------------- SCAN ----------------
function scanRunning(running){ $("s_net_btn").disabled=running; $("s_dev_btn").disabled=running; }
async function scanNetwork(){
  $("s_net_found").innerHTML='<div class="empty">Scanning…</div>'; $("s_net_found").dataset.has="0";
  $("s_net_bar").style.width="0%";
  try{ await fetch("/scan/network",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(gather("s_",S))}); }catch(e){ addLog("s_log","error","Scan failed: "+e); }
}
async function scanDevices(){
  $("s_dev_grid").innerHTML='<div class="empty">Probing…</div>'; $("s_dev_grid").dataset.has="0";
  $("s_dev_bar").style.width="0%";
  try{ await fetch("/scan/devices",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(gather("s_",S))}); }catch(e){ addLog("s_log","error","Scan failed: "+e); }
}
async function scanStop(){ try{ await fetch("/scan/stop",{method:"POST"}); }catch(e){} }
function useGateway(ip){
  $("c_target_ip").value=ip; $("m_target_ip").value=ip; $("s_dev_ip").value=ip;
  saveAll(); addLog("s_log","ok","Gateway "+ip+" copied to Monitor & Control.");
}
function mmText(make, model){
  const m=[make,model].map(x=>(x||"").trim()).filter(Boolean).join(" · ");
  return m ? esc(m) : '<span class="muted">make/model not reported</span>';
}
function addFoundHost(ip, port, make, model, mac){
  const box=$("s_net_found");
  if(box.dataset.has!=="1"){ box.innerHTML=""; box.dataset.has="1"; }
  const d=document.createElement("div"); d.className="found-item";
  d.innerHTML='<span class="ip">'+esc(ip)+'</span><span style="color:var(--muted)">:'+port+'</span>'+
    '<span class="mm">'+mmText(make,model)+'</span>'+
    '<button onclick="useGateway(\''+esc(ip)+'\')">Use</button>'+
    (mac?'<span class="mac">MAC '+esc(mac)+'</span>':"");
  box.appendChild(d);
}
function addSlave(slave, status, detail, make, model){
  const grid=$("s_dev_grid");
  if(grid.dataset.has!=="1"){ grid.innerHTML=""; grid.dataset.has="1"; }
  let cls="timeout"; if(status==="present") cls="present"; else if(status==="other") cls="other";
  const d=document.createElement("div"); d.className="slave "+cls;
  const mm=[make,model].map(x=>(x||"").trim()).filter(Boolean).join(" ");
  d.title=(detail||"")+(mm?("  —  "+mm):"");
  d.innerHTML="#"+slave+(mm?'<span class="mm">'+esc(mm)+'</span>':"");
  grid.appendChild(d);
}

// ---------------- SSE routing ----------------
function handle(ev){
  const ch=ev.channel||"monitor";
  if(ch==="monitor"){
    if(ev.type==="status"){ updateBadge("monitor",ev.state,ev.message); monitorBtns(ev.state==="running"||ev.state==="connecting");
      if(ev.state!=="idle") addLog("m_log",ev.state==="error"?"error":"info",ev.message,ev.ts); }
    else if(ev.type==="log") addLog("m_log",ev.level||"info",ev.message,ev.ts);
    else if(ev.type==="data"){ renderRegs(ev.start,ev.registers,ev.ts);
      $("m_polls").textContent=ev.poll_count; $("m_ok").textContent=ev.success_count; $("m_err").textContent=ev.error_count; }
    else if(ev.type==="stats"){ $("m_polls").textContent=ev.poll_count; $("m_ok").textContent=ev.success_count; $("m_err").textContent=ev.error_count; }
  } else if(ch==="control"){
    if(ev.type==="status"){ updateBadge("control",ev.state,ev.message); controlBtns(ev.state==="running");
      if(ev.state!=="idle") addLog("c_log",ev.state==="error"?"error":"info",ev.message,ev.ts); }
    else if(ev.type==="log") addLog("c_log",ev.level||"info",ev.message,ev.ts);
    else if(ev.type==="coil") setCoilLight(ev.coil, ev.state, ev.ts);
    else if(ev.type==="coil_status"){
      for(let i=0;i<ev.states.length;i++) setCoilLight(ev.start+i, ev.states[i], ev.ts);
    }
    else if(ev.type==="countdown"){
      setCoilCountdown(ev.coil, ev.remaining, ev.done);
      updateTimer(ev.coil, ev.remaining, ev.total, ev.done);
    }
    else if(ev.type==="watch"){
      watchBtns(!!ev.watching);
      if(ev.watching && ev.start!==undefined && ev.count!==undefined){
        $("c_watch_start").value=ev.start; $("c_watch_count").value=ev.count;
        buildBank(); saveAll();
      }
    }
  } else if(ch==="scan"){
    if(ev.type==="scan_status"){ updateBadge("scan",ev.state,ev.message); scanRunning(ev.state==="running");
      const p=ev.kind==="device"?"s_dev_prog":"s_net_prog"; $(p).textContent=ev.message; }
    else if(ev.type==="log") addLog("s_log",ev.level||"info",ev.message,ev.ts);
    else if(ev.type==="scan_found") addFoundHost(ev.ip, ev.port, ev.make, ev.model, ev.mac);
    else if(ev.type==="scan_device") addSlave(ev.slave, ev.status, ev.detail, ev.make, ev.model);
    else if(ev.type==="scan_progress"){
      const pct=ev.total?Math.round(ev.scanned/ev.total*100):0;
      if(ev.kind==="device"){ $("s_dev_bar").style.width=pct+"%"; $("s_dev_prog").textContent=ev.scanned+"/"+ev.total; $("s_dev_count").textContent=ev.found+" found"; }
      else { $("s_net_bar").style.width=pct+"%"; $("s_net_prog").textContent=ev.scanned+"/"+ev.total; $("s_net_count").textContent=ev.found+" found"; }
    }
    else if(ev.type==="scan_done"){
      addLog("s_log", ev.found?"ok":"info",
        (ev.kind==="network"?"Network":"Slave")+" scan "+(ev.stopped?"stopped":"complete")+
        " — "+ev.found+" found ("+ev.scanned+"/"+ev.total+" probed).");
      if(ev.found===0){
        if(ev.kind==="network" && $("s_net_found").dataset.has!=="1") $("s_net_found").innerHTML='<div class="empty">No gateways found.</div>';
        if(ev.kind==="device" && $("s_dev_grid").dataset.has!=="1") $("s_dev_grid").innerHTML='<div class="empty">No responding slaves found.</div>';
      }
    }
  }
}
function connectStream(){
  if(es) es.close();
  es=new EventSource("/stream");
  es.onmessage=e=>{ try{ handle(JSON.parse(e.data)); }catch(err){} };
}

// ---------------- persistence + boot ----------------
function saveAll(){
  localStorage.setItem("modfire", JSON.stringify({
    m:gather("m_",M), c:gather("c_",C), s:gather("s_",S)
  }));
}
window.addEventListener("load", async ()=>{
  // 1) server defaults
  let cfg=null; try{ cfg=await (await fetch("/config")).json(); }catch(e){}
  if(cfg){ fill("m_",M,cfg.monitor); fill("c_",C,cfg.control); fill("s_",S,cfg.scan);
    if(cfg.server){
      const ips=cfg.server.ips||[], port=cfg.server.port;
      if(ips.length) $("serverInfo").innerHTML="🖥 This PC: <b>"+esc(ips[0])+":"+port+"</b>";
      else $("serverInfo").innerHTML="🖥 This PC: <b>localhost:"+port+"</b>";
      if(ips.length>1) $("serverInfo").title="Also reachable at: "+ips.slice(1).map(i=>i+":"+port).join(", ");
    }
  }
  // 2) saved overrides
  try{ const saved=JSON.parse(localStorage.getItem("modfire")||"{}");
    if(saved.m) fill("m_",M,saved.m); if(saved.c) fill("c_",C,saved.c); if(saved.s) fill("s_",S,saved.s); }catch(e){}
  buildBank();
  // 3) live state
  if(cfg){
    if(cfg.monitor.running){ updateBadge("monitor","running","Polling"); monitorBtns(true); }
    if(cfg.control.connected){ updateBadge("control","running","Connected"); controlBtns(true);
      watchBtns(!!cfg.control.watching); }
  }
  document.querySelectorAll("input").forEach(i=>i.addEventListener("change",saveAll));
  $("c_watch_start").addEventListener("input", buildBank);
  $("c_watch_count").addEventListener("input", buildBank);
  connectStream();
  setInterval(()=>{ renderStateTime(); updateCoilVisuals(); }, 1000);
});
</script>
</body>
</html>
"""


def get_local_ips():
    """Return this PC's LAN IPv4 address(es), best guess first."""
    ips = []
    # The address used to reach the outside world is usually the primary LAN IP.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # Add any other non-loopback addresses bound to this host.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def ensure_windows_firewall(port):
    """On Windows, offer to open the port so other devices can reach the page.

    Adding the rule needs admin rights, so this launches an elevated command,
    which raises the standard Windows UAC 'allow changes?' prompt. No-op on
    other platforms (their firewalls, if any, are handled differently)."""
    if platform.system() != "Windows":
        return
    rule = "ModFire Modbus Console"
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            capture_output=True, text=True)
        if rule in out.stdout and "No rules match" not in out.stdout:
            print(f"  Windows Firewall: rule '{rule}' is already in place.")
            return
    except Exception:
        pass

    print("\n  Windows Firewall")
    print(f"  To reach this page from other devices, Windows must allow inbound")
    print(f"  TCP port {port}.")
    try:
        ans = input("  Add the firewall rule now? A Windows prompt will appear. [Y/n]: ")
    except EOFError:
        ans = "n"
    manual = (f'netsh advfirewall firewall add rule name="{rule}" '
              f'dir=in action=allow protocol=TCP localport={port}')
    if ans.strip().lower() in ("", "y", "yes"):
        ps = (f'Start-Process netsh -Verb RunAs -ArgumentList '
              f"'advfirewall firewall add rule name=\"{rule}\" dir=in action=allow "
              f"protocol=TCP localport={port}'")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
            print("  → Approve the Windows 'allow changes?' prompt to finish.")
            print(f"    (If you declined, run this later as Administrator:\n     {manual})")
        except Exception as e:
            print(f"  Could not open the prompt automatically: {e}")
            print(f"  Run this as Administrator instead:\n     {manual}")
    else:
        print("  Skipped — other devices may be blocked until you allow the port.")
        print(f"  To allow it later, run as Administrator:\n     {manual}")


def main():
    SERVER_INFO["ips"] = get_local_ips()
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print("=" * 60)
    print("  ModFire — Modbus TCP Console (Monitor · Control · Scan)")
    print("=" * 60)
    print("  Open one of these in your browser:")
    print(f"    • On this PC:        http://127.0.0.1:{PORT}")
    for ip in SERVER_INFO["ips"]:
        print(f"    • On the network:    http://{ip}:{PORT}")
    if not SERVER_INFO["ips"]:
        print("    (Could not detect a LAN IP — check your network connection.)")
    print("=" * 60)

    ensure_windows_firewall(PORT)

    print("\n  Server running. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        MANAGER.stop()
        CONTROL.disconnect()
        SCAN.stop()
        server.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
