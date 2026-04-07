"""HTTP control API for KVM commands that can't be sent over VNC.

Runs alongside the VNC server on a separate port (default: 6900).
Provides a simple REST API for video settings, power control, KVM port
switching, exclusive access, and other KVM-specific features.

Usage examples with curl:

    # Get current status and video settings
    curl http://localhost:6900/status

    # Video settings
    curl -X POST http://localhost:6900/video/brightness/128
    curl -X POST http://localhost:6900/video/contrast/200
    curl -X POST http://localhost:6900/video/auto-adjust
    curl -X POST http://localhost:6900/video/save
    curl -X POST http://localhost:6900/video/undo
    curl -X POST http://localhost:6900/video/reset-mode
    curl -X POST http://localhost:6900/video/reset-all

    # KVM port switching (multi-server KVM)
    curl -X POST http://localhost:6900/kvm/port/2

    # Exclusive access
    curl -X POST http://localhost:6900/exclusive/on
    curl -X POST http://localhost:6900/exclusive/off

    # Keyboard
    curl -X POST http://localhost:6900/keyboard/release-all

    # Type a string (sends key press+release for each character)
    curl -X POST http://localhost:6900/keyboard/type -d 'Hello World'
"""

import asyncio
import json
import logging
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# Video setting name -> (setting_id, min, max)
VIDEO_SETTINGS = {
    "brightness":      (0, 0, 127),   # KVM firmware wraps at 128, cap to 127
    "contrast":        (1, 0, 255),
    "contrast-red":    (1, 0, 255),
    "contrast-green":  (2, 0, 255),
    "contrast-blue":   (3, 0, 255),
    "clock":           (4, 0, 4320),
    "phase":           (5, 0, 31),
    "h-offset":        (6, 0, 512),
    "v-offset":        (7, 0, 128),
}

VIDEO_ACTIONS = {
    "reset-all":   (8, 0),
    "reset-mode":  (9, 0),
    "save":        (10, 0),
    "undo":        (11, 0),
    "auto-adjust": (12, 0),
}


class ControlAPI:
    """Lightweight HTTP API server for KVM control commands."""

    def __init__(self, bridge, listen_host: str = "127.0.0.1", listen_port: int = 6900):
        self.bridge = bridge
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server: asyncio.Server | None = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection, self.listen_host, self.listen_port)
        addr = self._server.sockets[0].getsockname()
        logger.info("Control API listening on http://%s:%d/", addr[0], addr[1])

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
        try:
            # Read HTTP request (simple parsing - no chunked, no keep-alive)
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                await self._send_response(writer, 400, {"error": "bad request"})
                return

            method = parts[0].upper()
            path = unquote(parts[1])

            # Read headers
            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                header = line.decode("utf-8", errors="replace").strip().lower()
                if header.startswith("content-length:"):
                    content_length = int(header.split(":", 1)[1].strip())

            # Read body if present
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            # Route request
            result = await self._route(method, path, body)
            if len(result) == 3:
                await self._send_response(writer, result[0], result[1], result[2])
            else:
                await self._send_response(writer, result[0], result[1])

        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("Control API error")
            try:
                await self._send_response(writer, 500, {"error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer: asyncio.StreamWriter,
                              status: int, body: dict | str,
                              content_type: str | None = None):
        if isinstance(body, dict):
            body_bytes = json.dumps(body, indent=2).encode("utf-8")
            ct = content_type or "application/json"
        else:
            body_bytes = body.encode("utf-8") if isinstance(body, str) else body
            ct = content_type or "text/plain"

        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "")
        header = (
            f"HTTP/1.0 {status} {reason}\r\n"
            f"Content-Type: {ct}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode("utf-8") + body_bytes)
        await writer.drain()

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        """Route an HTTP request to the appropriate handler."""
        kvm = self.bridge.kvm

        # Strip trailing slash
        path = path.rstrip("/")
        segments = [s for s in path.split("/") if s]

        if not segments:
            return (200, _WEB_UI_HTML, "text/html; charset=utf-8")

        # GET /status
        if segments == ["status"]:
            return self._get_status()

        # GET /help
        if segments == ["help"]:
            return self._help()

        if not kvm.connected:
            return (503, {"error": "not connected to KVM"})

        # POST /video/<setting>/<value>  or  POST /video/<action>
        if segments[0] == "video":
            return await self._handle_video(segments[1:], kvm)

        # POST /kvm/port/<n>
        if segments[0] == "kvm" and len(segments) >= 3 and segments[1] == "port":
            try:
                port = int(segments[2])
            except ValueError:
                return (400, {"error": "port must be an integer"})
            await kvm.send_kvm_port_switch(port)
            return (200, {"ok": True, "action": "kvm_port_switch", "port": port})

        # POST /exclusive/on or /exclusive/off
        if segments[0] == "exclusive" and len(segments) >= 2:
            if segments[1] in ("on", "off"):
                await kvm.send_exclusive_access(segments[1] == "on")
                return (200, {"ok": True, "action": "exclusive", "state": segments[1]})
            return (400, {"error": "use /exclusive/on or /exclusive/off"})

        # POST /keyboard/release-all
        if segments[0] == "keyboard" and len(segments) >= 2:
            if segments[1] == "release-all":
                await kvm.send_release_all_modifiers()
                return (200, {"ok": True, "action": "release_all_modifiers"})
            if segments[1] == "type":
                return await self._handle_type_string(body, kvm)
            return (400, {"error": "use /keyboard/release-all or /keyboard/type"})

        # POST /rdp/on or /rdp/off
        if segments[0] == "rdp" and len(segments) >= 2:
            if segments[1] == "on":
                await kvm.send_mode_command(0)
                return (200, {"ok": True, "action": "rdp", "state": "on"})
            elif segments[1] == "off":
                await kvm.send_mode_command(3)
                return (200, {"ok": True, "action": "rdp", "state": "off"})
            return (400, {"error": "use /rdp/on or /rdp/off"})

        # POST /host-direct/on or /host-direct/off
        if segments[0] == "host-direct" and len(segments) >= 2:
            if segments[1] == "on":
                await kvm.send_mode_command(2)
                return (200, {"ok": True, "action": "host_direct", "state": "on"})
            elif segments[1] == "off":
                await kvm.send_mode_command(3)
                return (200, {"ok": True, "action": "host_direct", "state": "off"})
            return (400, {"error": "use /host-direct/on or /host-direct/off"})

        return (404, {"error": f"unknown endpoint: {path}",
                      "hint": "try GET /help"})

    def _get_status(self) -> tuple[int, dict]:
        kvm = self.bridge.kvm
        vs = kvm.video_settings
        return (200, {
            "connected": kvm.connected,
            "server_name": kvm.server_name,
            "protocol_version": f"{kvm.server_version_major}.{kvm.server_version_minor:02d}",
            "framebuffer": {
                "width": kvm.width,
                "height": kvm.height,
            },
            "video_settings": {
                "brightness": vs.brightness,
                "contrast": vs.contrast,
                "contrast_green": vs.contrast_green,
                "contrast_blue": vs.contrast_blue,
                "clock": vs.clock,
                "phase": vs.phase,
                "h_offset": vs.h_offset,
                "v_offset": vs.v_offset,
                "resolution": f"{vs.h_resolution}x{vs.v_resolution}",
                "refresh_rate": vs.refresh_rate,
            },
            "keyboard_layout": self.bridge.keyboard_layout,
            "vnc_clients": len(self.bridge.vnc._clients),
            "kvm_port": kvm.current_port,
            "exclusive_mode": kvm.exclusive_mode,
            "rdp_mode": kvm.rdp_mode,
            "rdp_available": kvm.rdp_available,
            "host_direct_mode": kvm.host_direct_mode,
            "connected_users": kvm.connected_users,
        })

    def _help(self) -> tuple[int, dict]:
        return (200, {
            "endpoints": {
                "GET /status": "Current KVM status and video settings",
                "GET /help": "This help message",
                "POST /video/<setting>/<value>": f"Adjust video: {', '.join(VIDEO_SETTINGS.keys())}",
                "POST /video/auto-adjust": "Auto-adjust video settings",
                "POST /video/save": "Save current video settings",
                "POST /video/undo": "Undo video setting changes",
                "POST /video/reset-mode": "Reset current video mode to factory",
                "POST /video/reset-all": "Reset ALL video modes to factory",
                "POST /kvm/port/<n>": "Switch KVM to port number n",
                "POST /exclusive/on|off": "Enable/disable exclusive access",
                "POST /keyboard/release-all": "Release all held modifier keys",
                "POST /keyboard/type": "Type a string (send in request body)",
                "POST /rdp/on|off": "Enter/exit Remote Desktop mode",
                "POST /host-direct/on|off": "Enter/exit Host Acceleration mode",
            }
        })

    async def _handle_video(self, segments: list[str], kvm) -> tuple[int, dict]:
        if not segments:
            return (400, {"error": "specify a setting name or action",
                          "settings": list(VIDEO_SETTINGS.keys()),
                          "actions": list(VIDEO_ACTIONS.keys())})

        name = segments[0]

        # Check for action (no value needed)
        if name in VIDEO_ACTIONS:
            setting_id, default_val = VIDEO_ACTIONS[name]
            # Open the video settings dialog on the KVM side first
            await kvm.send_video_settings_request(1)
            await asyncio.sleep(0.1)
            await kvm.send_video_setting(setting_id, default_val)
            return (200, {"ok": True, "action": name})

        # Check for setting with value
        if name not in VIDEO_SETTINGS:
            return (400, {"error": f"unknown video setting: {name}",
                          "settings": list(VIDEO_SETTINGS.keys()),
                          "actions": list(VIDEO_ACTIONS.keys())})

        if len(segments) < 2:
            setting_id, vmin, vmax = VIDEO_SETTINGS[name]
            return (400, {"error": f"specify a value: /video/{name}/<{vmin}-{vmax}>"})

        try:
            value = int(segments[1])
        except ValueError:
            return (400, {"error": "value must be an integer"})

        setting_id, vmin, vmax = VIDEO_SETTINGS[name]
        if value < vmin or value > vmax:
            return (400, {"error": f"{name} must be between {vmin} and {vmax}"})

        # Request current settings first (opens the dialog on the KVM side)
        await kvm.send_video_settings_request(1)
        await asyncio.sleep(0.1)  # brief pause for KVM to respond
        await kvm.send_video_setting(setting_id, value)
        return (200, {"ok": True, "setting": name, "value": value})

    async def _handle_type_string(self, body: bytes, kvm) -> tuple[int, dict]:
        """Type a string by sending press+release for each character."""
        from vnc2ipkvm.keyboard import keysym_to_scancode, MODIFIER_SCANCODES
        text = body.decode("utf-8", errors="replace")
        if not text:
            return (400, {"error": "provide text in request body"})

        typed = 0
        for ch in text:
            keysym = ord(ch)
            sc = keysym_to_scancode(keysym)
            if sc is not None:
                # Check if this is an uppercase letter or shifted symbol
                needs_shift = ch.isupper() or ch in '!@#$%^&*()_+{}|:"<>?~'
                if needs_shift:
                    await kvm.send_key_event(41, True)  # Shift press
                await kvm.send_key_event(sc, True)
                await kvm.send_key_event(sc, False)
                if needs_shift:
                    await kvm.send_key_event(41, False)  # Shift release
                typed += 1
                await asyncio.sleep(0.02)  # 20ms between keys

        return (200, {"ok": True, "action": "type", "chars_typed": typed,
                      "chars_total": len(text)})


# ---------------------------------------------------------------------------
# Embedded single-page web UI
# ---------------------------------------------------------------------------

_WEB_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IP-KVM Control Panel</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; margin: 0;
         background: #1a1a2e; color: #e0e0e0; }
  .container { max-width: 860px; margin: 0 auto; padding: 16px; }
  h1 { margin: 0 0 4px; font-size: 1.4em; color: #fff; }
  h1 small { font-weight: normal; color: #888; font-size: 0.7em; }
  .status-bar { display: flex; gap: 16px; flex-wrap: wrap;
                padding: 8px 12px; background: #16213e; border-radius: 6px;
                margin-bottom: 16px; font-size: 0.85em; }
  .status-bar .dot { width: 10px; height: 10px; border-radius: 50%;
                     display: inline-block; margin-right: 4px; }
  .dot.on  { background: #4ade80; }
  .dot.off { background: #f87171; }

  .card { background: #16213e; border-radius: 8px; padding: 16px;
          margin-bottom: 12px; }
  .card h2 { margin: 0 0 12px; font-size: 1.1em; color: #93c5fd;
             border-bottom: 1px solid #2a3a5e; padding-bottom: 6px; }

  .slider-row { display: flex; align-items: center; gap: 10px;
                margin-bottom: 8px; }
  .slider-row label { width: 130px; text-align: right; font-size: 0.85em;
                      flex-shrink: 0; }
  .slider-row input[type=range] { flex: 1; accent-color: #60a5fa; }
  .slider-row .spin { width: 64px; text-align: right; font-size: 0.85em;
                      font-variant-numeric: tabular-nums; color: #93c5fd;
                      background: #0f172a; border: 1px solid #334155;
                      border-radius: 4px; padding: 2px 4px; }

  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .btn { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer;
         font-size: 0.85em; color: #fff; background: #334155; }
  .btn:hover { background: #475569; }
  .btn:active { background: #1e293b; }
  .btn.primary { background: #2563eb; }
  .btn.primary:hover { background: #3b82f6; }
  .btn.danger { background: #991b1b; }
  .btn.danger:hover { background: #b91c1c; }

  .form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .form-row label { width: 130px; text-align: right; font-size: 0.85em; flex-shrink: 0; }
  .form-row input[type=number], .form-row input[type=text] {
    flex: 1; max-width: 200px; padding: 4px 8px; background: #0f172a;
    border: 1px solid #334155; border-radius: 4px; color: #e0e0e0;
    font-size: 0.85em; }

  .toast { position: fixed; bottom: 20px; right: 20px; padding: 10px 18px;
           border-radius: 6px; font-size: 0.85em; z-index: 999;
           transition: opacity 0.3s; }
  .toast.ok { background: #166534; color: #bbf7d0; }
  .toast.err { background: #7f1d1d; color: #fecaca; }
  .toast.hidden { opacity: 0; pointer-events: none; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 600px) { .grid-2 { grid-template-columns: 1fr; } }

  #type-text { max-width: 100%; width: 100%; }
</style>
</head>
<body>
<div class="container">

<h1>IP-KVM Control Panel <small>vnc2ipkvm</small></h1>

<div class="status-bar" id="status-bar">
  <span><span class="dot off" id="dot"></span> <span id="conn-status">Checking...</span></span>
  <span id="srv-name"></span>
  <span id="resolution"></span>
  <span id="vnc-clients"></span>
  <span>Port: <span id="kvm-port-display">—</span></span>
</div>

<div class="grid-2">

<!-- Video Settings -->
<div class="card">
  <h2>Video Settings <button class="btn" id="mode-toggle" onclick="toggleMode()" style="float:right;font-size:0.75em">Advanced</button></h2>
  <div id="video-sliders"></div>
  <div class="btn-row">
    <button class="btn primary" onclick="postAction('/video/auto-adjust')">Auto Adjust</button>
    <button class="btn" onclick="postAction('/video/save')">Save</button>
    <button class="btn" onclick="postAction('/video/undo')">Undo</button>
    <button class="btn danger" onclick="if(confirm('Reset this video mode to factory?')) postAction('/video/reset-mode')">Reset Mode</button>
    <button class="btn danger" onclick="if(confirm('Reset ALL video modes to factory?')) postAction('/video/reset-all')">Reset All</button>
  </div>
</div>

<!-- KVM & Access -->
<div class="card">
  <h2>KVM &amp; Access</h2>
  <div class="form-row">
    <label>KVM Port:</label>
    <input type="number" id="kvm-port" value="1" min="1" max="16" style="max-width:80px">
    <button class="btn primary" onclick="postAction('/kvm/port/'+document.getElementById('kvm-port').value)">Switch</button>
  </div>
  <div class="form-row" style="margin-top:16px">
    <label>Exclusive Access:</label>
    <button class="btn" onclick="postAction('/exclusive/on')">Enable</button>
    <button class="btn" onclick="postAction('/exclusive/off')">Disable</button>
  </div>
</div>

</div><!-- grid-2 -->

<!-- Keyboard -->
<div class="card">
  <h2>Keyboard</h2>
  <div class="form-row">
    <label>Type text:</label>
    <input type="text" id="type-text" placeholder="Text to type on KVM...">
    <button class="btn primary" onclick="typeText()">Send</button>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button class="btn" onclick="postAction('/keyboard/release-all')">Release All Keys</button>
  </div>
</div>

<!-- Mode & Status -->
<div class="card">
  <h2>Mode &amp; Status</h2>
  <div class="form-row">
    <label>Remote Desktop:</label>
    <span id="rdp-state" style="margin-right:8px">—</span>
    <button class="btn" onclick="postAction('/rdp/on')">Enter</button>
    <button class="btn" onclick="postAction('/rdp/off')">Exit</button>
  </div>
  <div class="form-row">
    <label>Host Acceleration:</label>
    <span id="hd-state" style="margin-right:8px">—</span>
    <button class="btn" onclick="postAction('/host-direct/on')">Enter</button>
    <button class="btn" onclick="postAction('/host-direct/off')">Exit</button>
  </div>
  <div class="form-row" style="margin-top:12px">
    <label>Exclusive Access:</label>
    <span id="excl-state" style="margin-right:8px">—</span>
  </div>
  <div class="form-row">
    <label>Connected Users:</label>
    <span id="user-count">—</span>
  </div>
</div>

</div><!-- container -->

<div class="toast hidden" id="toast"></div>

<script>
const API = '';  // same origin

const SLIDERS_STD = [
  { name: 'brightness',      label: 'Brightness',      key: 'brightness',      min: 0, max: 127 },
  { name: 'contrast',        label: 'Contrast',         key: 'contrast',        min: 0, max: 255 },
  { name: 'h-offset',        label: 'H Offset',         key: 'h_offset',        min: 0, max: 512 },
  { name: 'v-offset',        label: 'V Offset',         key: 'v_offset',        min: 0, max: 128 },
];
const SLIDERS_ADV = [
  { name: 'brightness',      label: 'Brightness',      key: 'brightness',      min: 0, max: 127 },
  { name: 'contrast',        label: 'Contrast Red',     key: 'contrast',        min: 0, max: 255 },
  { name: 'contrast-green',  label: 'Contrast Green',   key: 'contrast_green',  min: 0, max: 255 },
  { name: 'contrast-blue',   label: 'Contrast Blue',    key: 'contrast_blue',   min: 0, max: 255 },
  { name: 'clock',           label: 'Clock',            key: 'clock',           min: 0, max: 4320 },
  { name: 'phase',           label: 'Phase',            key: 'phase',           min: 0, max: 31 },
  { name: 'h-offset',        label: 'H Offset',         key: 'h_offset',        min: 0, max: 512 },
  { name: 'v-offset',        label: 'V Offset',         key: 'v_offset',        min: 0, max: 128 },
];
let SLIDERS = SLIDERS_STD;
let advancedMode = false;

function buildSliders() {
  const c = document.getElementById('video-sliders');
  c.innerHTML = '';
  SLIDERS.forEach(s => {
    const row = document.createElement('div');
    row.className = 'slider-row';
    row.innerHTML =
      '<label>' + s.label + '</label>' +
      '<input type="range" min="' + s.min + '" max="' + s.max + '" value="0" ' +
        'id="sl-' + s.name + '" oninput="sliderInput(this, \\'' + s.name + '\\')">' +
      '<input type="number" min="' + s.min + '" max="' + s.max + '" value="0" ' +
        'id="sv-' + s.name + '" class="spin" ' +
        'onchange="spinChanged(this, \\'' + s.name + '\\')">';
    c.appendChild(row);
  });
}

function toggleMode() {
  advancedMode = !advancedMode;
  SLIDERS = advancedMode ? SLIDERS_ADV : SLIDERS_STD;
  document.getElementById('mode-toggle').textContent = advancedMode ? 'Standard' : 'Advanced';  // shows what you'd switch TO
  buildSliders();
  refreshStatus();
}

let sliderTimer = {};
function sliderInput(el, name) {
  document.getElementById('sv-' + name).value = el.value;
  clearTimeout(sliderTimer[name]);
  sliderTimer[name] = setTimeout(() => {
    postAction('/video/' + name + '/' + el.value);
  }, 150);
}
function spinChanged(el, name) {
  const slider = document.getElementById('sl-' + name);
  if (slider) slider.value = el.value;
  clearTimeout(sliderTimer[name]);
  sliderTimer[name] = setTimeout(() => {
    postAction('/video/' + name + '/' + el.value);
  }, 150);
}

let pauseRefreshUntil = 0;
async function postAction(path) {
  try {
    pauseRefreshUntil = Date.now() + 2000;  // pause auto-refresh for 2s after action
    const r = await fetch(API + path, { method: 'POST' });
    const j = await r.json();
    if (j.ok) toast(JSON.stringify(j), 'ok');
    else toast(j.error || 'Error', 'err');
  } catch (e) { toast('Request failed: ' + e.message, 'err'); }
}

async function typeText() {
  const text = document.getElementById('type-text').value;
  if (!text) return;
  try {
    const r = await fetch(API + '/keyboard/type', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: text
    });
    const j = await r.json();
    if (j.ok) {
      toast('Typed ' + j.chars_typed + '/' + j.chars_total + ' chars', 'ok');
      document.getElementById('type-text').value = '';
    } else toast(j.error || 'Error', 'err');
  } catch (e) { toast('Request failed: ' + e.message, 'err'); }
}

function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add('hidden'), 3000);
}

async function refreshStatus() {
  if (Date.now() < pauseRefreshUntil) return;  // skip during slider interaction
  try {
    const r = await fetch(API + '/status');
    const s = await r.json();
    const dot = document.getElementById('dot');
    const conn = document.getElementById('conn-status');
    if (s.connected) {
      dot.className = 'dot on';
      conn.textContent = 'Connected';
    } else {
      dot.className = 'dot off';
      conn.textContent = 'Disconnected';
    }
    document.getElementById('srv-name').textContent = s.server_name || '';
    const fb = s.framebuffer || {};
    const vs = s.video_settings || {};
    document.getElementById('resolution').textContent =
      (vs.resolution || (fb.width + 'x' + fb.height)) +
      (vs.refresh_rate ? ' @' + vs.refresh_rate + 'Hz' : '');
    document.getElementById('vnc-clients').textContent =
      (s.vnc_clients || 0) + ' VNC client' + (s.vnc_clients !== 1 ? 's' : '');

    const portEl = document.getElementById('kvm-port-display');
    if (portEl) portEl.textContent = s.kvm_port != null ? s.kvm_port : '—';

    // Mode & status
    const rdpEl = document.getElementById('rdp-state');
    if (rdpEl) rdpEl.textContent = s.rdp_mode ? 'Active' :
      (s.rdp_available === false ? 'Unavailable' : 'Off');
    const hdEl = document.getElementById('hd-state');
    if (hdEl) hdEl.textContent = s.host_direct_mode ? 'Active' : 'Off';
    const exclEl = document.getElementById('excl-state');
    if (exclEl) exclEl.textContent = s.exclusive_mode === true ? 'On' :
      (s.exclusive_mode === false ? 'Off' : '—');
    const userEl = document.getElementById('user-count');
    if (userEl) userEl.textContent = s.connected_users != null ? s.connected_users : '—';

    // Update sliders and spin inputs to match server values
    SLIDERS.forEach(sl => {
      const val = vs[sl.key];
      if (val !== undefined) {
        const slider = document.getElementById('sl-' + sl.name);
        const spin = document.getElementById('sv-' + sl.name);
        if (slider && document.activeElement !== slider) slider.value = val;
        if (spin && document.activeElement !== spin) spin.value = val;
      }
    });
  } catch (e) {
    document.getElementById('dot').className = 'dot off';
    document.getElementById('conn-status').textContent = 'API unreachable';
  }
}

buildSliders();
refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""
