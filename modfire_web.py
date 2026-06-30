#!/usr/bin/env python3
"""
modfire_web.py — Single-file local web app for live Modbus TCP polling.

Run it:
    python3 modfire_web.py
Then open the URL it prints (default http://127.0.0.1:8512) in your browser.

It serves a self-contained web page (HTML/CSS/JS embedded below) that lets you
configure a Modbus TCP target, start/stop continuous polling of holding
registers (Function Code 03), and watch the results update in real time via
Server-Sent Events. No third-party packages required — standard library only.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8512

# --- Default configuration (matches the original modpoll script) ---
DEFAULTS = {
    "target_ip": "192.168.23.201",
    "target_port": 502,
    "slave_id": 1,
    "register_addr": 0,
    "num_registers": 16,
    "interval": 2.0,
    "timeout": 4.0,
}


# ---------------------------------------------------------------------------
# Modbus TCP helpers
# ---------------------------------------------------------------------------
def build_read_packet(transaction_id, slave_id, start_register, count):
    """Assemble a raw Modbus TCP frame (MBAP header + PDU) for FC03."""
    tid = transaction_id.to_bytes(2, "big")
    protocol_id = b"\x00\x00"
    length = b"\x00\x06"
    unit_id = bytes([slave_id & 0xFF])
    function_code = b"\x03"
    reg_address = start_register.to_bytes(2, "big")
    reg_count = count.to_bytes(2, "big")
    return tid + protocol_id + length + unit_id + function_code + reg_address + reg_count


def parse_response(response, num_registers):
    """Return (registers, info_dict). Raises ValueError on a bad frame."""
    if len(response) < 9:
        raise ValueError(f"Short frame ({len(response)} bytes): {response.hex().upper()}")

    fc = response[7]
    if fc == 0x83:  # Exception response to FC03
        code = response[8] if len(response) > 8 else 0
        meanings = {
            1: "Illegal Function",
            2: "Illegal Data Address",
            3: "Illegal Data Value",
            4: "Slave Device Failure",
            5: "Acknowledge",
            6: "Slave Device Busy",
        }
        raise ValueError(f"Modbus exception {code}: {meanings.get(code, 'Unknown')}")

    if fc != 0x03:
        raise ValueError(f"Unexpected response (hex): {response.hex().upper()}")

    byte_count = response[8]
    expected_total = 9 + byte_count
    if len(response) < expected_total:
        raise ValueError(f"Incomplete packet: expected {expected_total} bytes, got {len(response)}")

    registers = []
    for i in range(0, byte_count, 2):
        start_idx = 9 + i
        registers.append(int.from_bytes(response[start_idx:start_idx + 2], "big"))
    return registers, {"byte_count": byte_count}


# ---------------------------------------------------------------------------
# Event hub: broadcasts poller events to all connected SSE clients
# ---------------------------------------------------------------------------
class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()
        self._last_status = {"type": "status", "state": "idle", "message": "Idle"}

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
        # Immediately send the latest known status so a fresh page is in sync.
        try:
            q.put_nowait(self._last_status)
        except Exception:
            pass
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event):
        if event.get("type") == "status":
            self._last_status = event
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                # Drop the event for slow/full clients rather than block.
                pass


HUB = EventHub()


# ---------------------------------------------------------------------------
# Poller: connects to the gateway and reads registers on an interval
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
        ip = cfg["target_ip"]
        port = int(cfg["target_port"])
        slave = int(cfg["slave_id"])
        start = int(cfg["register_addr"])
        count = int(cfg["num_registers"])
        interval = float(cfg["interval"])
        timeout = float(cfg["timeout"])

        poll_count = 0
        success_count = 0
        error_count = 0
        sock = None

        HUB.publish({"type": "log", "level": "info",
                     "message": f"Connecting to gateway at {ip}:{port} ..."})
        HUB.publish({"type": "status", "state": "connecting",
                     "message": f"Connecting to {ip}:{port}"})

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            HUB.publish({"type": "log", "level": "ok",
                         "message": "TCP link active. (Waveshare blue light should be SOLID.)"})
            HUB.publish({"type": "status", "state": "running",
                         "message": f"Polling {count} register(s) from addr {start} on slave {slave}"})
        except Exception as e:
            HUB.publish({"type": "log", "level": "error", "message": f"Connection failed: {e}"})
            HUB.publish({"type": "status", "state": "error", "message": f"Connection failed: {e}"})
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
                    packet = build_read_packet(self._next_tid(), slave, start, count)
                    sock.send(packet)
                    response = sock.recv(1024)
                    if not response:
                        raise ConnectionError("Connection closed by remote host")

                    registers, _ = parse_response(response, count)
                    success_count += 1
                    HUB.publish({
                        "type": "data",
                        "ts": ts,
                        "start": start,
                        "registers": registers,
                        "poll_count": poll_count,
                        "success_count": success_count,
                        "error_count": error_count,
                        "raw": response.hex().upper(),
                    })
                except socket.timeout:
                    error_count += 1
                    HUB.publish({"type": "log", "level": "warn", "ts": ts,
                                 "message": "No reply from downstream RS485 device "
                                            "(check baud rate / wiring / slave ID)"})
                    HUB.publish({"type": "stats", "poll_count": poll_count,
                                 "success_count": success_count, "error_count": error_count})
                except (ConnectionError, OSError) as e:
                    error_count += 1
                    HUB.publish({"type": "log", "level": "error", "ts": ts,
                                 "message": f"Link error: {e}. Stopping."})
                    HUB.publish({"type": "status", "state": "error",
                                 "message": f"Link error: {e}"})
                    break
                except ValueError as e:
                    error_count += 1
                    HUB.publish({"type": "log", "level": "error", "ts": ts, "message": str(e)})
                    HUB.publish({"type": "stats", "poll_count": poll_count,
                                 "success_count": success_count, "error_count": error_count})

                # Sleep in small slices so Stop is responsive.
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
            HUB.publish({"type": "log", "level": "info", "message": "Socket closed safely."})
            if self._stop_event.is_set():
                HUB.publish({"type": "status", "state": "idle", "message": "Stopped"})


# ---------------------------------------------------------------------------
# Poller manager (one active poller at a time)
# ---------------------------------------------------------------------------
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
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Quiet; we have our own logging.

    def _send(self, code, body, content_type="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE)
        elif self.path == "/config":
            cfg = dict(DEFAULTS)
            cfg["running"] = MANAGER.is_running()
            self._send(200, json.dumps(cfg), "application/json")
        elif self.path == "/stream":
            self._stream()
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path == "/start":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                cfg = dict(DEFAULTS)
                cfg.update(json.loads(raw or b"{}"))
                # Coerce/validate numerics.
                cfg["target_port"] = int(cfg["target_port"])
                cfg["slave_id"] = int(cfg["slave_id"])
                cfg["register_addr"] = int(cfg["register_addr"])
                cfg["num_registers"] = max(1, min(125, int(cfg["num_registers"])))
                cfg["interval"] = max(0.05, float(cfg["interval"]))
                cfg["timeout"] = max(0.2, float(cfg["timeout"]))
                MANAGER.start(cfg)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}), "application/json")
        elif self.path == "/stop":
            MANAGER.stop()
            self._send(200, json.dumps({"ok": True}), "application/json")
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def _stream(self):
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            # Initial comment to open the stream.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            import queue
            while True:
                try:
                    event = q.get(timeout=15.0)
                    payload = "data: " + json.dumps(event) + "\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Heartbeat keeps the connection (and proxies) alive.
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)


# ---------------------------------------------------------------------------
# Embedded single-page front end
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ModFire — Live Modbus Monitor</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2a3140;
    --text:#e6edf3; --muted:#8b949e; --accent:#1f6feb; --accent2:#58a6ff;
    --ok:#3fb950; --warn:#d29922; --err:#f85149; --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;
    border-bottom:1px solid var(--line);background:linear-gradient(180deg,#161b22,#0d1117);}
  header h1{font-size:18px;margin:0;font-weight:600;letter-spacing:.3px}
  header .flame{font-size:22px}
  .badge{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px;
    padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel);}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--muted);box-shadow:0 0 0 0 rgba(0,0,0,0)}
  .dot.idle{background:var(--muted)}
  .dot.connecting{background:var(--warn);animation:pulse 1s infinite}
  .dot.running{background:var(--ok);animation:pulse 1.4s infinite}
  .dot.error{background:var(--err)}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 8px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  main{display:grid;grid-template-columns:320px 1fr;gap:16px;padding:16px;align-items:start}
  @media(max-width:880px){main{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  .panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);
    margin:0 0 12px;font-weight:600}
  .field{margin-bottom:12px}
  .field label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
  .field input{width:100%;padding:8px 10px;border-radius:7px;border:1px solid var(--line);
    background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;outline:none}
  .field input:focus{border-color:var(--accent2)}
  .row{display:flex;gap:10px}
  .row .field{flex:1}
  .btns{display:flex;gap:10px;margin-top:6px}
  button{flex:1;padding:10px;border-radius:7px;border:1px solid var(--line);cursor:pointer;
    font-size:14px;font-weight:600;color:var(--text);background:var(--panel2);transition:.15s}
  button:hover{border-color:var(--accent2)}
  button.start{background:var(--accent);border-color:var(--accent)}
  button.start:hover{background:#2a7bf0}
  button.stop{background:#30363d}
  button:disabled{opacity:.45;cursor:not-allowed}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
  .stat{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px;text-align:center}
  .stat .num{font-size:24px;font-weight:700;font-family:var(--mono)}
  .stat .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
  .stat.ok .num{color:var(--ok)} .stat.err .num{color:var(--err)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px}
  .reg{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 12px;
    transition:background .35s, border-color .35s}
  .reg.flash{background:#15324a;border-color:var(--accent2)}
  .reg .addr{font-size:11px;color:var(--muted);font-family:var(--mono)}
  .reg .val{font-size:21px;font-weight:700;font-family:var(--mono);margin-top:3px}
  .reg .hex{font-size:11px;color:var(--accent2);font-family:var(--mono);margin-top:2px}
  .meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-bottom:12px;font-family:var(--mono)}
  .log{height:260px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:9px;
    padding:10px;font-family:var(--mono);font-size:12px;line-height:1.55}
  .log div{white-space:pre-wrap;word-break:break-word}
  .log .t{color:var(--muted)}
  .log .info{color:var(--accent2)} .log .ok{color:var(--ok)}
  .log .warn{color:var(--warn)} .log .error{color:var(--err)}
  .empty{color:var(--muted);font-size:13px;padding:20px;text-align:center}
  .hint{font-size:11px;color:var(--muted);margin-top:10px;line-height:1.5}
</style>
</head>
<body>
<header>
  <span class="flame">🔥</span>
  <h1>ModFire — Live Modbus TCP Monitor</h1>
  <div class="badge"><span id="dot" class="dot idle"></span><span id="state">Idle</span></div>
</header>

<main>
  <section class="panel" id="cfgPanel">
    <h2>Target Configuration</h2>
    <div class="field">
      <label>Gateway IP</label>
      <input id="target_ip" value="192.168.23.201">
    </div>
    <div class="row">
      <div class="field"><label>Port</label><input id="target_port" value="502"></div>
      <div class="field"><label>Slave ID</label><input id="slave_id" value="1"></div>
    </div>
    <div class="row">
      <div class="field"><label>Start Register</label><input id="register_addr" value="0"></div>
      <div class="field"><label># Registers</label><input id="num_registers" value="16"></div>
    </div>
    <div class="row">
      <div class="field"><label>Interval (s)</label><input id="interval" value="2"></div>
      <div class="field"><label>Timeout (s)</label><input id="timeout" value="4"></div>
    </div>
    <div class="btns">
      <button id="startBtn" class="start" onclick="startPoll()">▶ Start</button>
      <button id="stopBtn" class="stop" onclick="stopPoll()" disabled>■ Stop</button>
    </div>
    <div class="hint">Reads Holding Registers (FC03) continuously and streams
      results here in real time. Config changes apply on the next Start.</div>
  </section>

  <section>
    <div class="panel" style="margin-bottom:16px">
      <h2>Registers</h2>
      <div class="meta">
        <span id="metaRange">—</span>
        <span id="metaTime">Last update: —</span>
      </div>
      <div class="stats">
        <div class="stat"><div class="num" id="sPolls">0</div><div class="lbl">Polls</div></div>
        <div class="stat ok"><div class="num" id="sOk">0</div><div class="lbl">Success</div></div>
        <div class="stat err"><div class="num" id="sErr">0</div><div class="lbl">Errors</div></div>
      </div>
      <div id="grid" class="grid"><div class="empty">No data yet — press Start to begin polling.</div></div>
    </div>

    <div class="panel">
      <h2>Event Log</h2>
      <div id="log" class="log"></div>
    </div>
  </section>
</main>

<script>
let es = null;
const $ = id => document.getElementById(id);
const fields = ["target_ip","target_port","slave_id","register_addr","num_registers","interval","timeout"];

function setState(state, msg){
  $("dot").className = "dot " + state;
  $("state").textContent = msg || state;
  const running = (state === "running" || state === "connecting");
  $("startBtn").disabled = running;
  $("stopBtn").disabled = !running;
}

function pad(n){return String(n).padStart(2,"0");}
function tstr(ts){
  const d = ts ? new Date(ts*1000) : new Date();
  return pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());
}

function addLog(level, message, ts){
  const log = $("log");
  const div = document.createElement("div");
  div.innerHTML = '<span class="t">['+tstr(ts)+']</span> <span class="'+level+'">'+
                  escapeHtml(message)+'</span>';
  log.appendChild(div);
  // keep last ~400 lines
  while(log.childNodes.length > 400) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function renderRegisters(start, regs, ts){
  const grid = $("grid");
  const want = regs.length;
  // (Re)build cells if structure changed.
  if(grid.dataset.count != want || grid.dataset.start != start){
    grid.innerHTML = "";
    for(let i=0;i<want;i++){
      const cell = document.createElement("div");
      cell.className = "reg";
      cell.id = "reg"+i;
      cell.innerHTML = '<div class="addr">#'+(start+i)+'</div>'+
                       '<div class="val">—</div><div class="hex">0x0000</div>';
      grid.appendChild(cell);
    }
    grid.dataset.count = want;
    grid.dataset.start = start;
  }
  for(let i=0;i<want;i++){
    const cell = $("reg"+i);
    if(!cell) continue;
    const valEl = cell.querySelector(".val");
    const hexEl = cell.querySelector(".hex");
    const newVal = String(regs[i]);
    if(valEl.textContent !== newVal){
      valEl.textContent = newVal;
      hexEl.textContent = "0x"+regs[i].toString(16).toUpperCase().padStart(4,"0");
      cell.classList.add("flash");
      setTimeout(()=>cell.classList.remove("flash"), 350);
    }
  }
  $("metaRange").textContent = "Registers "+start+"–"+(start+want-1);
  $("metaTime").textContent = "Last update: "+tstr(ts);
}

function handle(ev){
  if(ev.type === "status"){
    setState(ev.state, ev.message);
    if(ev.state !== "idle") addLog(ev.state==="error"?"error":"info", ev.message, ev.ts);
  } else if(ev.type === "log"){
    addLog(ev.level || "info", ev.message, ev.ts);
  } else if(ev.type === "data"){
    renderRegisters(ev.start, ev.registers, ev.ts);
    $("sPolls").textContent = ev.poll_count;
    $("sOk").textContent = ev.success_count;
    $("sErr").textContent = ev.error_count;
  } else if(ev.type === "stats"){
    $("sPolls").textContent = ev.poll_count;
    $("sOk").textContent = ev.success_count;
    $("sErr").textContent = ev.error_count;
  }
}

function connectStream(){
  if(es) es.close();
  es = new EventSource("/stream");
  es.onmessage = e => { try{ handle(JSON.parse(e.data)); }catch(err){} };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

function gather(){
  const cfg = {};
  fields.forEach(f => cfg[f] = $(f).value.trim());
  return cfg;
}

async function startPoll(){
  setState("connecting", "Starting…");
  try{
    const r = await fetch("/start", {method:"POST", headers:{"Content-Type":"application/json"},
                                     body: JSON.stringify(gather())});
    const j = await r.json();
    if(!j.ok){ setState("error", j.error||"Failed"); addLog("error", j.error||"Failed to start"); }
  }catch(e){ setState("error", "Request failed"); addLog("error", "Start request failed: "+e); }
}

async function stopPoll(){
  try{ await fetch("/stop", {method:"POST"}); }catch(e){}
}

// Load saved config from localStorage, then current server defaults.
window.addEventListener("load", async () => {
  try{
    const saved = JSON.parse(localStorage.getItem("modfire_cfg")||"{}");
    fields.forEach(f => { if(saved[f] !== undefined) $(f).value = saved[f]; });
  }catch(e){}
  fields.forEach(f => $(f).addEventListener("change", () => {
    const cfg = gather(); localStorage.setItem("modfire_cfg", JSON.stringify(cfg));
  }));
  try{
    const r = await fetch("/config"); const j = await r.json();
    if(j.running) setState("running", "Polling");
  }catch(e){}
  connectStream();
});
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("  ModFire — Live Modbus TCP Monitor")
    print("=" * 60)
    print(f"  Open this in your browser:  {url}")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        MANAGER.stop()
        server.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
