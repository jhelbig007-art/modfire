# ModFire — Modbus TCP Console

A single-file local web app for talking to Modbus TCP devices from your browser,
with everything updating in **real time**. It wraps the original command-line
scripts into one multi-page UI:

- **📊 Monitor** — continuously read Holding Registers (FC03) and watch values
  update live (the `modpoll` script).
- **🎚 Control** — fire coils (FC05): turn ON/OFF, or **Pulse** (ON → wait → OFF),
  plus a configurable-size quick bank (the `modfire` coil script).
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
python3 modfire_web.py     # on Windows:  python modfire_web.py
```

On startup it prints **this PC's IP address** and the URLs to open — both on
this machine and from other devices on the network, for example:

```
  Open one of these in your browser:
    • On this PC:        http://127.0.0.1:8080
    • On the network:    http://192.168.23.50:8080
```

The same address is shown in the top-right of the web page (🖥 This PC: …).

### Network access & Windows Firewall

The server listens on all network interfaces so a tablet or another PC on the
same network can open it. The first time you run it on **Windows**, it offers to
add a firewall rule for the port and launches an elevated command — approve the
Windows **"allow changes?"** prompt to let other devices connect. If you skip it
(or aren't on Windows reaching it remotely), you can add it later as
Administrator:

```
netsh advfirewall firewall add rule name="ModFire Modbus Console (8080)" dir=in action=allow protocol=TCP localport=8080
```

The rule name includes the port number, so if you change `PORT` later a fresh
rule gets created automatically rather than being silently skipped because an
old rule (for a previous port) already existed under the same name.

To keep the page private to this machine only, set `HOST = "127.0.0.1"` near the
top of `modfire_web.py` (no firewall rule needed then).

#### Still can't reach it from another device?

1. **Confirm the rule is actually for this port.** `netsh advfirewall firewall
   show rule name=all | findstr /I modfire` (or check Windows Defender
   Firewall → Advanced Settings → Inbound Rules) — you should see "ModFire
   Modbus Console (8080)", enabled, TCP, with `LocalPort 8080`. Delete any
   older rule for a different port; it's just clutter, not a blocker.
2. **Test from the server PC itself first**, using its LAN IP (not
   `127.0.0.1`) — e.g. `http://192.168.x.x:8080` in a browser on the same
   machine. If that fails too, it's not the *other* device or the network,
   it's this machine's firewall/binding — re-run the firewall prompt or add
   the rule manually.
3. **Check for a second firewall.** Third-party antivirus/security suites
   (Norton, McAfee, Kaspersky, etc.) often run their own firewall on top of
   Windows Firewall and need the port allowed separately.
4. **Check the network profile.** If Windows treats the connection as
   "Public" rather than "Private", some setups restrict inbound traffic more
   aggressively — Settings → Network & Internet → confirm the active network
   type, and consider switching it to Private.
5. **Router/AP client isolation.** Some home and most guest Wi-Fi networks
   block devices from talking to each other even on the same SSID. Try both
   devices on Ethernet, or check your router's Wi-Fi settings for "AP/client
   isolation" and disable it.
6. **Make sure both devices are actually on the same subnet** (e.g. both
   `192.168.1.x`, not one on `192.168.1.x` and the other on a guest network
   like `192.168.2.x`).

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
2. Fire a coil by index with **ON** / **OFF** / **⚡ Pulse**, or use the quick
   bank. Pulse turns the coil ON, waits, then OFF — like the original script's
   5-second cycle, with a **live countdown** shown on the coil and in a timer
   banner.
3. **Coil bank size** — set **Start coil** and **# Coils** to size the bank to
   your device (e.g. start `0`, count `32`). It resizes immediately as you type,
   no reconnect needed. Modbus allows up to 2000 coils per read; the bank
   display itself is capped at 512 cells to stay responsive — the status watch
   still covers your full requested count either way.
4. **Coil status watch** — on Connect, ModFire polls live coil states with
   **FC01 (Read Coils)** over the same Start coil/# Coils range, so the bank
   lights reflect the device's *actual* status, not just what was commanded.
   Change the range/interval and click **Watch** to re-apply, or **Stop watch**
   to pause it.
5. **30-minute history** — every coil's ON/OFF changes are logged for the last
   30 minutes. **Click a coil's name** in the bank to open its timeline (each
   state, when it changed, and how long it was held, plus total ON time).
6. **Short-cycle flags (ON and OFF)** — two thresholds, both default 10s:
   - **Flag if last ON was shorter than (s)** — while a coil is currently OFF,
     if its most recent completed ON period lasted less than this, it's
     highlighted **red** (e.g. meant to hold 30s, only stayed on 10s).
   - **Flag if last OFF was shorter than (s)** — while a coil is currently ON,
     if its most recent completed OFF period was shorter than this, it's
     flagged **red** too (e.g. it flapped back ON too soon after being OFF).

   Only one applies at a time (whichever matches the coil's *current* state),
   with a badge showing the actual duration. The flag clears as soon as the
   relevant period lasts long enough. Works for both manual ON/OFF and Pulse.
7. **Live state timer** — every coil in the bank shows how long it's been in
   its current state (e.g. `ON 12s` or `OFF 3m45s`), ticking live.

The header badge next to each page's status also shows how long it's been in
that state (e.g. "Connected · 4m12s"), updating live.

### Scan
- **Find gateways** — enter a subnet (first three octets) and host range; it
  reports every host with the Modbus port open. Click **Use** to copy a found
  gateway into the Monitor and Control pages.
- **Find slaves** — point at one gateway and probe a range of unit IDs; any
  reply (data *or* a Modbus exception) proves a module is present at that ID.

ModFire reports the **make / model** of what it finds, using several methods in
order (best effort):

- **Modbus FC43 / MEI-14** (Read Device Identification) — works for any device,
  gateway or slave, that implements it. Many simple devices don't.
- For **gateways** (devices with their own IP), two extra fallbacks that don't
  need Modbus support:
  - **MAC address vendor (OUI)** — the manufacturer encoded in the MAC, read
    from the local ARP table. The raw MAC is always shown.
  - **HTTP banner** — the web-UI page title / `Server:` header, if the gateway
    has a web interface.

A downstream **RS485 slave** has no IP, MAC, or web UI, so it can only be
identified if it supports FC43; otherwise it shows "make/model not reported".
The MAC-vendor lookup uses a small built-in table and, if the machine has
internet, an online OUI API — failing gracefully to just the raw MAC.

## Notes

- The server binds to all interfaces (`0.0.0.0`) so the LAN can reach it; change
  `HOST`/`PORT` at the top of `modfire_web.py` if needed.
- The Python server must run on a machine that can reach the Modbus gateway
  (the browser can't open raw TCP sockets itself).
- Multiple browser tabs can watch the same live stream simultaneously; field
  values are remembered in your browser.
