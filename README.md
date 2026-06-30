# ModFire — Live Modbus TCP Monitor

A single-file local web app that continuously polls a Modbus TCP device
(Holding Registers, Function Code 03) and displays the results in your browser
in **real time**.

It's the original `modpoll` polling script wrapped in a small web UI: configure
the target, hit **Start**, and watch register values stream in live.

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

1. Fill in the gateway IP, port, slave ID, starting register, register count,
   poll interval, and timeout. Defaults match the original script
   (`192.168.23.201:502`, slave 1, 16 registers from address 0, every 2s).
2. Click **Start**. The status badge turns green while polling.
3. Register values update live in the grid (changed cells flash), with running
   counts of polls / successes / errors and a scrolling event log.
4. Click **Stop** to end polling. Config changes apply on the next Start and are
   remembered in your browser.

## Notes

- The server binds to `127.0.0.1` only (local machine). Change `HOST`/`PORT` at
  the top of `modfire_web.py` if needed.
- Multiple browser tabs can watch the same live stream simultaneously.
