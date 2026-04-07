# vnc2ipkvm - VNC-to-IPKVM Protocol Translator

A standalone bridge that connects to a Belkin IP-KVM device using its
proprietary e-RIC RFB protocol, and exposes it as a standard VNC server.
Any modern VNC client can then control the KVM without needing Java or a
legacy browser.

```
[VNC Client] <--standard RFB 3.8--> [vnc2ipkvm] <--e-RIC RFB--> [Belkin IP-KVM]
                                         |
                                    HTTP Control API
                                    (video, power, KVM port, ...)
```

## Requirements

- Python 3.10+
- No external dependencies (uses only the standard library)

## Quick Start

```bash
python -m vnc2ipkvm --host 192.168.1.100 --applet-id ABC123 -v
```

Then connect any VNC client (TigerVNC, RealVNC, TightVNC, etc.) to
`localhost:5900`.

The **Applet ID** is the session token from the KVM's web interface. Log
into the KVM's web UI, open the remote console page, and find the
`APPLET_ID` or `SRV_ID` parameter in the applet tag or URL.

## Command-Line Options

```
Usage: python -m vnc2ipkvm [options]

Connection:
  --host HOST             KVM hostname or IP address (required)
  --port PORT             KVM TCP port (default: 443)
  --ssl-port PORT         KVM SSL port (default: same as --port)
  --applet-id ID          Session/auth ID from KVM web interface (required)
  --protocol-version VER  Protocol version string (default: 01.00)
  --port-id N             KVM port number (default: 0)
  --no-share              Request exclusive access (default: shared)
  --ssl / --no-ssl        Enable/disable SSL (default: SSL on)
  --norbox {no,ipv4,ipv6} NORBOX routing mode (default: no)
  --norbox-target ADDR    NORBOX target IP address

VNC Server:
  --vnc-host ADDR         VNC listen address (default: 0.0.0.0)
  --vnc-port PORT         VNC listen port (default: 5900)

Control API:
  --api-host ADDR         API listen address (default: 127.0.0.1)
  --api-port PORT         API listen port (default: 6900, 0 to disable)

Input:
  --layout LAYOUT         Keyboard layout (default: en_US)
  --encodings LIST        Comma-separated encoding list (default: 255,7,6)

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
sent over VNC (video settings, power control, KVM port switching, etc.).

Open **http://localhost:6900/** in a browser for a web-based control panel
with sliders and buttons. Or use `curl` from the command line:

### Endpoints

#### Status

```bash
# Current KVM status and video settings (JSON)
curl http://localhost:6900/status

# List all endpoints
curl http://localhost:6900/help
```

Example `/status` response:

```json
{
  "connected": true,
  "server_name": "Belkin KVM",
  "protocol_version": "3.08",
  "framebuffer": {
    "width": 1024,
    "height": 768
  },
  "video_settings": {
    "brightness": 128,
    "contrast": 200,
    "contrast_green": 200,
    "contrast_blue": 200,
    "clock": 2160,
    "phase": 16,
    "h_offset": 256,
    "v_offset": 64,
    "resolution": "1024x768",
    "refresh_rate": 60
  },
  "keyboard_layout": "en_GB",
  "vnc_clients": 1
}
```

#### Video Settings

Adjust the KVM's video capture parameters. These control the analog-to-digital
conversion of the VGA signal from the managed server.

```bash
# Individual settings (each has a valid range)
curl -X POST http://localhost:6900/video/brightness/128     # 0-255
curl -X POST http://localhost:6900/video/contrast/200       # 0-255 (or contrast-red)
curl -X POST http://localhost:6900/video/contrast-green/200 # 0-255
curl -X POST http://localhost:6900/video/contrast-blue/200  # 0-255
curl -X POST http://localhost:6900/video/clock/2160         # 0-4320
curl -X POST http://localhost:6900/video/phase/16           # 0-31
curl -X POST http://localhost:6900/video/h-offset/256       # 0-512
curl -X POST http://localhost:6900/video/v-offset/64        # 0-128

# Actions
curl -X POST http://localhost:6900/video/auto-adjust  # auto-detect optimal settings
curl -X POST http://localhost:6900/video/save          # save current settings to KVM
curl -X POST http://localhost:6900/video/undo          # revert to saved settings
curl -X POST http://localhost:6900/video/reset-mode    # reset current video mode
curl -X POST http://localhost:6900/video/reset-all     # factory reset all video modes
```

#### KVM Port Switching

For multi-server KVMs, switch the active port:

```bash
curl -X POST http://localhost:6900/kvm/port/1   # switch to port 1
curl -X POST http://localhost:6900/kvm/port/2   # switch to port 2
```

#### Exclusive Access

Lock out other remote console users:

```bash
curl -X POST http://localhost:6900/exclusive/on
curl -X POST http://localhost:6900/exclusive/off
```

#### Keyboard

```bash
# Release all stuck modifier keys (useful if Ctrl/Alt/Shift get stuck)
curl -X POST http://localhost:6900/keyboard/release-all

# Type a string on the KVM (sends individual key press/release events)
curl -X POST http://localhost:6900/keyboard/type -d 'Hello World'
```

#### Power Control

```bash
curl -X POST http://localhost:6900/power/0
```

### Web Control Panel

Open **http://localhost:6900/** in any browser for a graphical control panel:

- Live connection status, resolution, and refresh rate
- Video setting sliders with real-time preview
- Auto-adjust, save, undo, and factory reset buttons
- KVM port switching
- Exclusive access toggle
- Text typing input
- Key release button

The panel auto-refreshes status every 3 seconds and sends slider changes
with 150ms debouncing to avoid flooding the KVM.

### API Security

By default the API listens on `127.0.0.1` (localhost only). To expose it
on all interfaces:

```bash
python -m vnc2ipkvm --host ... --api-host 0.0.0.0
```

There is no authentication on the API. Use firewall rules if exposing
it on a network.

## Architecture

```
vnc2ipkvm/
  __init__.py          Package marker
  __main__.py          python -m entry point
  main.py              CLI, Bridge class (wires KVM client <-> VNC server)
  eric_protocol.py     e-RIC RFB client (connect, auth, framebuffer, input)
  vnc_server.py        Standard VNC/RFB 3.8 server
  control_api.py       HTTP control API + embedded web UI
  framebuffer.py       Shared framebuffer with dirty tracking
  keyboard.py          Keysym-to-scancode translation (9 layouts)
  color.py             RGB332 <-> RGB888 color conversion
```

### Protocol Documentation

See [PROTOCOL.md](../PROTOCOL.md) for a complete specification of the e-RIC
RFB protocol reverse-engineered from the decompiled Java client.

## Examples

```bash
# Basic usage with UK keyboard
python -m vnc2ipkvm --host 192.168.1.100 --applet-id ABC123 --layout en_GB -v

# Plain TCP (no SSL), custom VNC port
python -m vnc2ipkvm --host 10.0.0.50 --no-ssl --port 80 --applet-id XYZ \
  --vnc-port 5901 -v

# Disable control API
python -m vnc2ipkvm --host kvm.local --applet-id ABC123 --api-port 0

# With NORBOX routing
python -m vnc2ipkvm --host proxy.local --applet-id ABC123 \
  --norbox ipv4 --norbox-target 192.168.1.100
```
