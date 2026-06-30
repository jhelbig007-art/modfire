# ModFire — Modbus TCP Console

A single-file local web app for talking to Modbus TCP devices from your browser,
with everything updating in **real time**. It wraps the original command-line
scripts into one multi-page UI:

- **📊 Monitor** — continuously read Holding Registers (FC03) and watch values
  update live (the `modpoll` script).
- **🎚 Control** — fire coils (FC05): turn ON/OFF, or **Pulse** (ON → wait → OFF),
  plus a 16-coil quick bank (the `modfire` coil script).
- **🛰 Scan** — discover Modbus modules: find gateways on your network (open
  Modbus port) and probe a gateway for responding RS485 slave IDs.

## Why a Python file and not a plain `.html`?

Browsers can't open raw TCP sockets to a Modbus gateway (security sandbox), so a
standalone HTML file can't talk to the device on its own. `modfire_web.py` is a
self-contained server **and** web page in one file: it does the Modbus polling
and serves the UI, pushing updates to the page over Server-Sent Events. No
third-party packages — Python standard library only.

## Run it

```bash
python3 modfire_web.py
```

Then open the URL it prints (default <http://127.0.0.1:8512>) in your browser.

## Use it

Switch between pages with the tabs in the header.

### Monitor
1. Fill in gateway IP, port, slave ID, start register, register count, poll
   interval, and timeout (defaults: `192.168.23.201:502`, slave 1, 16 registers
   from address 0, every 2s).
2. Click **Start** — values update live in the grid (changed cells flash), with
   running poll/success/error counts and an event log. **Stop** ends it.

### Control
1. Fill in gateway IP/port/slave (default `192.168.23.200:502`, slave 1) and a
   **Pulse** duration, then **Connect**.
2. Fire a coil by index with **ON** / **OFF** / **⚡ Pulse**, or use the 16-coil
   quick bank. Pulse turns the coil ON, waits, then OFF — like the original
   script's 5-second cycle. Coil indicators light up green when ON.

### Scan
- **Find gateways** — enter a subnet (first three octets) and host range; it
  reports every host with the Modbus port open. Click **Use** to copy a found
  gateway into the Monitor and Control pages.
- **Find slaves** — point at one gateway and probe a range of unit IDs; any
  reply (data *or* a Modbus exception) proves a module is present at that ID.

For every device it finds (gateway or slave), ModFire also requests the device
identification (Modbus **FC43 / MEI-14**) and shows the **make / model** when
the device reports it. This is best-effort: many simple RS485 devices and some
gateways don't implement FC43, in which case it shows "make/model not reported".

## Notes

- The server binds to `127.0.0.1` only (local machine). Change `HOST`/`PORT` at
  the top of `modfire_web.py` if needed.
- The Python server must run on a machine that can reach the Modbus gateway
  (the browser can't open raw TCP sockets itself).
- Multiple browser tabs can watch the same live stream simultaneously; field
  values are remembered in your browser.
