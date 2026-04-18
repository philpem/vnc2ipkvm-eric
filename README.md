# vnc2ipkvm - VNC-to-IPKVM Protocol Translator

Phil Pemberton, 2026.

`vnc2ipkvm` is a shim which translates from the VNC-based protocol used
by Raritan e-RIC RFB IP-KVMs to the standard VNC protocol. Alongside VNC,
a web interface is provided, which gives access to a web-based VNC client.

There's no password on the VNC port or web interface, so don't use this on
an insecure network...

## Requirements

- Python 3.10+
- `websockets` library (for the browser-based noVNC viewer)

```bash
pip install websockets
```

The core VNC bridge works without `websockets` — only the noVNC browser
viewer requires it.

## Quick Start

```bash
# Auto-login with username/password (recommended)
python -m vnc2ipkvm --host 192.168.1.100 --user admin --password pass -v

# Or provide a session ID manually
python -m vnc2ipkvm --host 192.168.1.100 --applet-id ABC123 -v
```

Then connect any VNC client (TigerVNC, RealVNC, etc.) to `localhost:5900`,
or open **http://localhost:6900/** in a browser for the built-in noVNC
viewer and control panel.

When using `--user`/`--password`, the session token is fetched
automatically from the KVM's web interface and refreshed on reconnect.
When using `--applet-id`, you must extract the session token manually
from the KVM's web UI (look for `APPLET_ID` in the applet page source),
and auto-reconnect will not be able to refresh expired sessions.

## Pixel Format and Encodings

### Pixel depth (`--bpp`)

| Mode | Flag | Description |
|------|------|-------------|
| **16-bit** (default) | `--bpp 16` | Native RGB565. Best colour fidelity. The KVM captures in 16-bit internally, so this avoids a quantisation step. |
| **8-bit** | `--bpp 8` | RGB332 with colour map. Lower bandwidth but visibly reduced colour depth (256 colours). Matches the original Java applet's mode. |

### Encoding presets (`--encodings`)

The encoding preset controls how the KVM compresses framebuffer updates.
Use a named preset or a comma-separated list of encoding IDs.

| Preset | Encodings | Description |
|--------|-----------|-------------|
| **`default`** | `255, 7, -250` | Hextile + tight. Best overall: hextile for incremental updates (cursor tracking, small changes), tight for larger regions. Recommended. |
| `compressed` | `7, -252` | Tight only with higher compression. Slower but lower bandwidth. |
| `corre` | `5` | Hextile only. Simple and reliable; no zlib state to manage. Good fallback if tight causes issues. |
| `tight` | `7, -250, 9` | Tight + extended encoding. Adds the tile-predictor cache (encoding 9) for better compression of repeated patterns. Extended encoding has known issues in 16-bit mode. |

```bash
# Use the default preset (recommended)
python -m vnc2ipkvm --host kvm.local --user admin --password pass

# Hextile only (simplest, most reliable)
python -m vnc2ipkvm --host kvm.local --user admin --password pass --encodings corre

# Custom encoding list
python -m vnc2ipkvm --host kvm.local --user admin --password pass --encodings 7,-250
```

### Choosing the right settings

For most use cases, the defaults (`--bpp 16`, `--encodings default`) are
best. The KVM will use hextile for small updates and tight for large
regions, with native 16-bit colour.

If you experience issues:
- **Visual corruption in a horizontal band** near the top of the initial
  screen load: this is the tight gradient filter (filter type 2), which
  the KVM sends only in 16-bit mode. It should render correctly; if it
  doesn't, try `--encodings corre` to avoid tight encoding entirely.
- **Stream desync or hangs**: try `--encodings corre` (hextile only) or
  `--bpp 8` to fall back to the mode the Java applet used.
- **Extended encoding (9) issues**: the `tight` preset includes encoding
  9 which has known wire-level desync in 16-bit mode. Avoid `--encodings
  tight` with `--bpp 16` until this is resolved.

## Command-Line Options

```
Usage: python -m vnc2ipkvm [options]

Connection:
  --host HOST             KVM hostname or IP address (required)
  --port PORT             KVM TCP port (default: 443)
  --ssl-port PORT         KVM SSL port (default: same as --port)
  --user USER             KVM web login username
  --password PASS         KVM web login password
  --http-port PORT        KVM web interface HTTP port (default: 80)
  --applet-id ID          Session/auth ID (auto-fetched with --user/--password)
  --protocol-version VER  Protocol version string (default: 01.00)
  --port-id N             KVM port number (default: 0)
  --no-share              Request exclusive access (default: shared)
  --ssl / --no-ssl        Enable/disable SSL (default: SSL on)
  --norbox {no,ipv4,ipv6} NORBOX routing mode (default: no)
  --norbox-target ADDR    NORBOX target IP address

Display:
  --bpp {8,16}            Pixel depth (default: 16 = RGB565, 8 = RGB332)
  --encodings PRESET      Encoding preset or comma-separated list
                          Presets: default, compressed, corre, tight

VNC Server:
  --vnc-host ADDR         VNC listen address (default: 0.0.0.0)
  --vnc-port PORT         VNC listen port (default: 5900)

Control API:
  --api-host ADDR         API listen address (default: 127.0.0.1)
  --api-port PORT         API listen port (default: 6900, 0 to disable)

Input:
  --layout LAYOUT         Keyboard layout (default: en_US)

Other:
  --no-reconnect          Disable auto-reconnect to KVM
  -v, -vv                 Increase verbosity (info / debug)
```

### Keyboard Layouts

| Layout   | Description     |
|----------|-----------------|
| `en_US`  | US English      |
| `en_GB`  | UK English      |
| `de_DE`  | German          |
| `de_CH`  | Swiss German    |
| `fr_FR`  | French (AZERTY) |
| `fr_CH`  | Swiss French    |
| `sv_SE`  | Swedish         |
| `no_NO`  | Norwegian       |
| `ja_JP`  | Japanese        |

## Control API

A lightweight HTTP API runs on port 6900 for KVM features that can't be
sent over VNC (video settings, KVM port switching, hotkeys, etc.).

Open **http://localhost:6900/** in a browser for the web control panel,
which includes:

- **Embedded noVNC viewer** — browser-based VNC client (no install needed)
- Live connection status with Server-Sent Events (SSE)
- Video setting sliders (brightness, contrast, clock, phase, offsets)
- KVM port switching for multi-server KVMs
- Exclusive access toggle
- Hotkey buttons (from KVM configuration, e.g. Ctrl+Alt+Delete)
- Text typing and key expression input
- RDP and Host Acceleration mode controls

### API Endpoints

```bash
# Status
curl http://localhost:6900/status        # JSON status and video settings
curl http://localhost:6900/help          # List all endpoints
GET  /events                            # SSE stream of status updates

# Video settings
curl -X POST http://localhost:6900/video/brightness/80    # 0-127
curl -X POST http://localhost:6900/video/contrast/200     # 0-255
curl -X POST http://localhost:6900/video/auto-adjust
curl -X POST http://localhost:6900/video/refresh          # Force full screen redraw
curl -X POST http://localhost:6900/video/save
curl -X POST http://localhost:6900/video/undo
curl -X POST http://localhost:6900/video/reset-mode
curl -X POST http://localhost:6900/video/reset-all

# KVM port switching
curl -X POST http://localhost:6900/kvm/port/1

# Exclusive access
curl -X POST http://localhost:6900/exclusive/on
curl -X POST http://localhost:6900/exclusive/off

# Keyboard
curl -X POST http://localhost:6900/keyboard/release-all
curl -X POST http://localhost:6900/keyboard/type -d 'Hello World'
curl -X POST http://localhost:6900/keyboard/send -d 'Ctrl+Alt+Delete'
curl -X POST http://localhost:6900/keyboard/send -d '36 37 4e f1'  # raw hex scan codes

# Hotkeys (from KVM configuration)
curl -X POST http://localhost:6900/hotkey/0

# Modes
curl -X POST http://localhost:6900/rdp/on            # Enter Remote Desktop mode
curl -X POST http://localhost:6900/host-direct/on    # Enter Host Acceleration mode
curl -X POST http://localhost:6900/mode/exit         # Exit current mode
```

### noVNC Viewer

The built-in noVNC viewer is available at **http://localhost:6900/vnc**
(or embedded in the main control panel). It connects via a WebSocket
proxy running on port 6901 (control API port + 1).

### API Security

By default the API listens on `127.0.0.1` (localhost only). To expose it
on all interfaces, use `--api-host 0.0.0.0`. There is no authentication
on the API — use firewall rules if exposing it on a network.

## Architecture

```
vnc2ipkvm/
  __init__.py          Package marker
  __main__.py          python -m entry point
  main.py              CLI, Bridge class (wires KVM client <-> VNC server)
  eric_protocol.py     e-RIC RFB client (connect, auth, framebuffer, input)
  vnc_server.py        Standard VNC/RFB 3.8 server (per-client dirty tracking)
  control_api.py       HTTP control API + embedded web UI
  websocket_proxy.py   WebSocket-to-TCP proxy for noVNC
  framebuffer.py       Shared framebuffer with dirty-region broadcasting
  keyboard.py          Keysym-to-scancode translation (9 layouts)
  color.py             RGB332/RGB565 <-> RGB888 color conversion tables
  web_login.py         Auto-login to KVM web interface for session tokens
  novnc/               Bundled noVNC v1.6.0 (MPL 2.0)
```

### Protocol Documentation

See [PROTOCOL.md](PROTOCOL.md) for a specification of the e-RIC RFB
protocol reverse-engineered from the decompiled Java client.

## Examples

```bash
# Auto-login with UK keyboard (most common usage)
python -m vnc2ipkvm --host 192.168.1.100 --user admin --password pass \
  --layout en_GB -v

# Plain TCP (no SSL), hextile-only encoding
python -m vnc2ipkvm --host 10.0.0.50 --no-ssl --user admin --password pass \
  --encodings corre --vnc-port 5901 -v

# 8-bit mode (matches original Java applet)
python -m vnc2ipkvm --host kvm.local --user admin --password pass --bpp 8

# Manual session ID, no control API
python -m vnc2ipkvm --host kvm.local --applet-id ABC123 --api-port 0

# With NORBOX routing
python -m vnc2ipkvm --host proxy.local --user admin --password pass \
  --norbox ipv4 --norbox-target 192.168.1.100
```
