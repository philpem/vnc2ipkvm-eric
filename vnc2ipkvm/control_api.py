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
import os
from urllib.parse import unquote

from vnc2ipkvm.websocket_proxy import WebSocketProxy

# Directory containing bundled noVNC files
_NOVNC_DIR = os.path.join(os.path.dirname(__file__), "novnc")

# MIME types for static file serving
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain",
    ".png": "image/png",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
}

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

def _is_hex_byte(s: str) -> bool:
    """Return True if s is a valid 1-2 digit hex value 00-FF."""
    try:
        v = int(s, 16)
        return 0 <= v <= 255 and len(s) <= 2
    except ValueError:
        return False


VIDEO_ACTIONS = {
    "reset-all":   (8, 0),
    "reset-mode":  (9, 0),
    "save":        (10, 0),
    "undo":        (11, 0),
    "auto-adjust": (12, 0),
}


class ControlAPI:
    """Lightweight HTTP API server for KVM control commands."""

    def __init__(self, bridge, listen_host: str = "127.0.0.1", listen_port: int = 6900,
                 vnc_host: str = "127.0.0.1", vnc_port: int = 5900):
        self.bridge = bridge
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.ws_port = listen_port + 1 if listen_port > 0 else 0
        self._server: asyncio.Server | None = None
        self._sse_clients: set[asyncio.StreamWriter] = set()
        self._ws_proxy = WebSocketProxy(vnc_host, vnc_port)

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection, self.listen_host, self.listen_port)
        addr = self._server.sockets[0].getsockname()
        logger.info("Control API listening on http://%s:%d/", addr[0], addr[1])
        await self._ws_proxy.start(self.listen_host, self.ws_port)

    async def stop(self):
        await self._ws_proxy.stop()
        # Close all SSE clients
        for writer in list(self._sse_clients):
            try:
                writer.close()
            except Exception:
                pass
        self._sse_clients.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def notify_clients(self):
        """Push current status to all SSE clients. Call from bridge on state changes."""
        if not self._sse_clients:
            return
        status = self._get_status()
        data = json.dumps(status[1], separators=(",", ":"))
        sse_msg = f"data: {data}\n\n"
        sse_bytes = sse_msg.encode("utf-8")
        dead = []
        for writer in self._sse_clients:
            try:
                writer.write(sse_bytes)
                # Schedule drain so buffered data actually gets sent
                asyncio.ensure_future(self._drain_writer(writer))
            except Exception:
                dead.append(writer)
        for w in dead:
            self._sse_clients.discard(w)

    @staticmethod
    async def _drain_writer(writer: asyncio.StreamWriter):
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _handle_sse(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter):
        """Handle an SSE connection: send headers, initial status, then keep alive."""
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        )
        writer.write(header.encode("utf-8"))
        # Send initial status immediately
        status = self._get_status()
        data = json.dumps(status[1], separators=(",", ":"))
        writer.write(f"data: {data}\n\n".encode("utf-8"))
        await writer.drain()
        self._sse_clients.add(writer)
        try:
            # Keep connection open until client disconnects
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._sse_clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

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

            # Read all headers
            headers = {}
            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                header = line.decode("utf-8", errors="replace").strip()
                if ":" in header:
                    key, val = header.split(":", 1)
                    headers[key.strip().lower()] = val.strip()
                    if key.strip().lower() == "content-length":
                        content_length = int(val.strip())

            # SSE endpoint
            if path.rstrip("/") == "/events" and method == "GET":
                await self._handle_sse(reader, writer)
                return

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

            # Push updated status to SSE clients after any successful POST
            if method == "POST" and result[0] == 200:
                self.notify_clients()

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

        # GET /vnc — redirect to noVNC viewer
        if segments == ["vnc"]:
            return self._serve_novnc_viewer()

        # GET /novnc/... — serve noVNC static files
        if segments[0] == "novnc":
            return self._serve_static(segments[1:])

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
            if segments[1] == "send":
                return await self._handle_send_scancodes(body, kvm)
            return (400, {"error": "use /keyboard/release-all, /keyboard/type, or /keyboard/send"})

        # POST /hotkey/<index>
        if segments[0] == "hotkey" and len(segments) >= 2:
            return await self._handle_hotkey(segments[1], kvm)

        # POST /rdp/on — enter Remote Desktop mode
        if segments[0] == "rdp" and len(segments) >= 2 and segments[1] == "on":
            await kvm.send_mode_command(0)
            return (200, {"ok": True, "action": "rdp", "state": "on"})

        # POST /host-direct/on — enter Host Acceleration mode
        if segments[0] == "host-direct" and len(segments) >= 2 and segments[1] == "on":
            await kvm.send_mode_command(2)
            return (200, {"ok": True, "action": "host_direct", "state": "on"})

        # POST /mode/exit — exit current mode (RDP or Host Direct)
        if segments[0] == "mode" and len(segments) >= 2 and segments[1] == "exit":
            await kvm.send_mode_command(3)
            return (200, {"ok": True, "action": "mode_exit"})

        return (404, {"error": f"unknown endpoint: {path}",
                      "hint": "try GET /help"})

    def _serve_novnc_viewer(self) -> tuple[int, str, str]:
        """Redirect to the noVNC viewer page (served under /novnc/ so
        relative ES module imports like ./core/rfb.js resolve correctly)."""
        html = ('<!DOCTYPE html><html><head>'
                '<meta http-equiv="refresh" content="0;url=/novnc/vnc_lite.html">'
                '</head></html>')
        return (200, html, "text/html; charset=utf-8")

    def _serve_static(self, path_segments: list[str]) -> tuple:
        """Serve a static file from the noVNC directory."""
        if not path_segments:
            return (404, {"error": "not found"})
        # Sanitise: reject path traversal attempts
        rel_path = "/".join(path_segments)
        if ".." in rel_path:
            return (400, {"error": "invalid path"})
        filepath = os.path.join(_NOVNC_DIR, rel_path)
        if not os.path.isfile(filepath):
            return (404, {"error": f"not found: {rel_path}"})
        ext = os.path.splitext(filepath)[1].lower()
        content_type = _MIME_TYPES.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            return (200, data, content_type)
        except OSError:
            return (500, {"error": "read error"})

    def _get_status(self) -> tuple[int, dict]:
        kvm = self.bridge.kvm
        vs = kvm.video_settings
        return (200, {
            "connected": kvm.connected,
            "kvm_host": self.bridge.kvm_config.host,
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
            "server_message": kvm.server_message,
            "hotkeys": [{"label": h["label"], "confirm": h["confirm"]}
                        for h in self.bridge.hotkeys],
        })

    def _help(self) -> tuple[int, dict]:
        return (200, {
            "endpoints": {
                "GET /status": "Current KVM status and video settings",
                "GET /events": "Server-Sent Events stream of status updates",
                "GET /vnc": "Embedded noVNC viewer (browser-based VNC client)",
                "ws://<host>:<port+1>": "WebSocket-to-VNC proxy (used by noVNC, separate port)",
                "GET /help": "This help message",
                "POST /video/<setting>/<value>": f"Adjust video: {', '.join(VIDEO_SETTINGS.keys())}",
                "POST /video/auto-adjust": "Auto-adjust video settings",
                "POST /video/refresh": "Force full screen refresh",
                "POST /video/save": "Save current video settings",
                "POST /video/undo": "Undo video setting changes",
                "POST /video/reset-mode": "Reset current video mode to factory",
                "POST /video/reset-all": "Reset ALL video modes to factory",
                "POST /kvm/port/<n>": "Switch KVM to port number n",
                "POST /exclusive/on|off": "Enable/disable exclusive access",
                "POST /keyboard/release-all": "Release all held modifier keys",
                "POST /keyboard/type": "Type a string (send in request body)",
                "POST /keyboard/send": "Send key expression (e.g. 'Ctrl+Alt+Delete') or hex scan codes",
                "POST /hotkey/<n>": "Send hotkey n (from KVM configuration)",
                "POST /rdp/on": "Enter Remote Desktop mode",
                "POST /host-direct/on": "Enter Host Acceleration mode",
                "POST /mode/exit": "Exit current mode (RDP or Host Direct)",
            }
        })

    async def _handle_video(self, segments: list[str], kvm) -> tuple[int, dict]:
        if not segments:
            return (400, {"error": "specify a setting name or action",
                          "settings": list(VIDEO_SETTINGS.keys()),
                          "actions": list(VIDEO_ACTIONS.keys())})

        name = segments[0]

        # Refresh video — dedicated command, not a video setting
        if name == "refresh":
            await kvm.send_refresh_video()
            return (200, {"ok": True, "action": "refresh"})

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

    async def _send_scancode_sequence(self, hex_codes: str, kvm) -> int:
        """Execute a scan code sequence. Returns number of keys sent.

        Codes are space-separated hex bytes. Special control codes:
          F0 = separator (ignored)
          F1 = release all held keys
          F2 = pause (100ms)
          F3 = release last key pressed
        All other values are e-RIC scan codes (pressed in order,
        accumulated on a stack, released in reverse at the end).
        """
        codes = hex_codes.split()
        pressed = []
        sent = 0

        for hex_code in codes:
            code = int(hex_code, 16)
            if code == 0xF0:
                continue
            elif code == 0xF1:
                for sc in reversed(pressed):
                    await kvm.send_key_event(sc, False)
                pressed.clear()
            elif code == 0xF2:
                await asyncio.sleep(0.1)
            elif code == 0xF3:
                if pressed:
                    await kvm.send_key_event(pressed.pop(), False)
            else:
                pressed.append(code)
                await kvm.send_key_event(code, True)
                sent += 1
                await asyncio.sleep(0.02)

        for sc in reversed(pressed):
            await kvm.send_key_event(sc, False)

        return sent

    async def _handle_hotkey(self, index_str: str, kvm) -> tuple[int, dict]:
        """Execute a hotkey by index."""
        try:
            index = int(index_str)
        except ValueError:
            return (400, {"error": "hotkey index must be an integer"})

        hotkeys = self.bridge.hotkeys
        if index < 0 or index >= len(hotkeys):
            return (400, {"error": f"hotkey index {index} out of range (0-{len(hotkeys)-1})"})

        hotkey = hotkeys[index]
        sent = await self._send_scancode_sequence(hotkey["codes"], kvm)
        return (200, {"ok": True, "action": "hotkey",
                      "index": index, "label": hotkey["label"], "keys_sent": sent})

    async def _handle_send_scancodes(self, body: bytes, kvm) -> tuple[int, dict]:
        """Send key sequence from the request body.

        Accepts either:
          - KVM-style hotkey expression: "Ctrl+Alt+Delete", "A-B-C"
          - Raw hex scan codes: "36 37 4e f1"
        Auto-detects format: if all tokens are valid hex bytes, treat as hex.
        """
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return (400, {"error": "provide key expression or hex scan codes"})

        # Auto-detect: if all space-separated tokens are valid hex 00-FF, use hex mode
        tokens = text.split()
        is_hex = all(_is_hex_byte(t) for t in tokens)

        if is_hex:
            sent = await self._send_scancode_sequence(text, kvm)
            return (200, {"ok": True, "action": "send", "format": "hex", "keys_sent": sent})

        # Otherwise parse as hotkey expression
        return await self._execute_hotkey_expression(text, kvm)

    async def _execute_hotkey_expression(self, expr: str, kvm) -> tuple[int, dict]:
        """Execute a KVM-style hotkey expression like 'Ctrl+Alt+Delete'."""
        from vnc2ipkvm.keyboard import parse_hotkey_expression
        try:
            actions = parse_hotkey_expression(expr)
        except ValueError as e:
            return (400, {"error": str(e)})

        sent = 0
        for action, sc in actions:
            if action == "press":
                await kvm.send_key_event(sc, True)
                sent += 1
                await asyncio.sleep(0.02)
            elif action == "release":
                await kvm.send_key_event(sc, False)
                await asyncio.sleep(0.02)
            elif action == "pause":
                await asyncio.sleep(0.1)

        return (200, {"ok": True, "action": "send", "format": "expression",
                      "expression": expr, "keys_sent": sent})


# ---------------------------------------------------------------------------
# Embedded single-page web UI
# ---------------------------------------------------------------------------

_WEB_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title id="page-title">vnc2ipkvm</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; margin: 0;
         background: #1a1a2e; color: #e0e0e0; }
  .container { max-width: 1100px; margin: 0 auto; padding: 16px; }
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

  .kvm-banner { background: #1e40af; color: #fff; text-align: center;
                padding: 8px 12px; border-radius: 6px; margin-top: 10px;
                font-size: 0.9em; font-family: monospace; white-space: pre;
                display: none; }
  .kvm-banner.visible { display: block; }

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

<h1>vnc2ipkvm <small id="kvm-host"></small>
  <a href="/vnc" target="_blank" style="float:right;font-size:0.55em;color:#93c5fd;text-decoration:none" title="Open in new tab">&#x29c9; Pop-out</a>
</h1>

<div class="status-bar" id="status-bar">
  <span><span class="dot off" id="dot"></span> <span id="conn-status">Checking...</span></span>
  <span id="srv-name"></span>
  <span id="resolution"></span>
  <span id="vnc-clients"></span>
  <span>Port: <span id="kvm-port-display">—</span></span>
</div>

<div class="card" id="vnc-card">
  <h2>Remote Console
    <button class="btn" id="vnc-toggle" onclick="toggleVnc()" style="float:right;font-size:0.75em">Hide</button>
  </h2>
  <iframe id="vnc-frame" src="/novnc/vnc_lite.html" style="width:100%;height:480px;border:1px solid #2a3a5e;border-radius:4px;background:#000"></iframe>
  <div class="kvm-banner" id="kvm-banner"></div>
</div>

<div class="grid-2">

<!-- Video Settings -->
<div class="card">
  <h2>Video Settings <button class="btn" id="mode-toggle" onclick="toggleMode()" style="float:right;font-size:0.75em">Advanced</button></h2>
  <div id="video-sliders"></div>
  <div class="btn-row">
    <button class="btn primary" onclick="postAction('/video/refresh')">Refresh</button>
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
  <div class="form-row" style="margin-top:12px">
    <label>Exclusive Access:</label>
    <button class="btn" onclick="postAction('/exclusive/on')">Enable</button>
    <button class="btn" onclick="postAction('/exclusive/off')">Disable</button>
  </div>
  <div class="form-row">
    <label></label>
    <span id="excl-state" style="font-size:0.85em;color:#93c5fd">—</span>
    <span style="font-size:0.85em;color:#888;margin-left:12px">Users: <span id="user-count">—</span></span>
  </div>
  <div class="form-row" style="margin-top:12px">
    <label>Remote Desktop:</label>
    <span id="rdp-state" style="margin-right:8px;font-size:0.85em;color:#93c5fd">—</span>
    <button class="btn" onclick="postAction('/rdp/on')">Enter</button>
  </div>
  <div class="form-row">
    <label>Host Acceleration:</label>
    <span id="hd-state" style="margin-right:8px;font-size:0.85em;color:#93c5fd">—</span>
    <button class="btn" onclick="postAction('/host-direct/on')">Enter</button>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button class="btn" onclick="postAction('/mode/exit')">Exit Current Mode</button>
  </div>
</div>

</div><!-- grid-2 -->

<!-- Keyboard -->
<div class="card">
  <h2>Keyboard</h2>
  <div id="hotkey-buttons" class="btn-row" style="margin-bottom:8px"></div>
  <div class="form-row">
    <label>Type text:</label>
    <input type="text" id="type-text" placeholder="Text to type on KVM...">
    <button class="btn primary" onclick="typeText()">Send</button>
  </div>
  <div class="form-row">
    <label>Send keys:</label>
    <input type="text" id="send-codes" placeholder="e.g. Ctrl+Alt+Delete">
    <button class="btn primary" onclick="sendCodes()">Send</button>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button class="btn" onclick="postAction('/keyboard/release-all')">Release All Keys</button>
  </div>
  <details style="margin-top:10px;font-size:0.8em;color:#aaa">
    <summary style="cursor:pointer;color:#93c5fd">Key expression syntax</summary>
    <div style="margin-top:6px;line-height:1.6">
      <b>Syntax:</b> <code>key [+ key]* [- key [+ key]*]*</code><br>
      <b>+</b> builds combinations (all held, released in reverse at <b>-</b> or end)<br>
      <b>-</b> separates independent keypress groups<br>
      <b>*</b> inserts a pause<br>
      <b>Examples:</b> <code>Ctrl+Alt+Delete</code> &nbsp; <code>Alt+F4</code> &nbsp; <code>A-B-C</code> (types A, B, C separately)<br>
      <b>Letters/digits:</b> A-Z, 0-9<br>
      <b>Modifiers:</b> Ctrl, Shift, Alt, AltGr, RCtrl, RShift<br>
      <b>F-keys:</b> F1-F12<br>
      <b>Navigation:</b> Insert, Delete, Home, End, Page_Up, Page_Down<br>
      <b>Arrows:</b> Up, Down, Left, Right<br>
      <b>Control:</b> Enter, Escape (Esc), Tab, Back_Space, Space, Caps_Lock<br>
      <b>Other:</b> PrintScreen, Scroll_Lock, Pause, Num_Lock, Windows, Menu<br>
      <b>Numpad:</b> Numpad0-Numpad9, NumpadPlus, NumpadMinus, NumpadMul, Numpad/, NumpadEnter<br>
      <b>Symbols:</b> ~ - = ; ' &lt; , . / [ ] \\<br>
      <b>Raw hex:</b> also accepts hex scan codes: <code>36 37 4e f1</code>
    </div>
  </details>
</div>

</div><!-- container -->

<div class="toast hidden" id="toast"></div>

<script>
const API = '';  // same origin

function toggleVnc() {
  const frame = document.getElementById('vnc-frame');
  const btn = document.getElementById('vnc-toggle');
  if (frame.style.display === 'none') {
    frame.style.display = '';
    btn.textContent = 'Hide';
  } else {
    frame.style.display = 'none';
    btn.textContent = 'Show';
  }
}

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

function applyStatus(s) {
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
  if (s.kvm_host) {
    document.getElementById('kvm-host').textContent = s.kvm_host;
    document.title = 'vnc2ipkvm — ' + s.kvm_host;
  }
  const fb = s.framebuffer || {};
  const vs = s.video_settings || {};
  document.getElementById('resolution').textContent =
    (vs.resolution || (fb.width + 'x' + fb.height)) +
    (vs.refresh_rate ? ' @' + vs.refresh_rate + 'Hz' : '');
  document.getElementById('vnc-clients').textContent =
    (s.vnc_clients || 0) + ' VNC client' + (s.vnc_clients !== 1 ? 's' : '');

  if (s.hotkeys) buildHotkeys(s.hotkeys);

  const banner = document.getElementById('kvm-banner');
  if (banner) {
    if (s.server_message) {
      banner.textContent = s.server_message;
      banner.className = 'kvm-banner visible';
    } else {
      banner.className = 'kvm-banner';
    }
  }

  const portEl = document.getElementById('kvm-port-display');
  if (portEl) portEl.textContent = s.kvm_port != null ? s.kvm_port : '—';

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

  SLIDERS.forEach(sl => {
    const val = vs[sl.key];
    if (val !== undefined) {
      const slider = document.getElementById('sl-' + sl.name);
      const spin = document.getElementById('sv-' + sl.name);
      if (slider && document.activeElement !== slider) slider.value = val;
      if (spin && document.activeElement !== spin) spin.value = val;
    }
  });
}

async function refreshStatus() {
  if (Date.now() < pauseRefreshUntil) return;
  try {
    const r = await fetch(API + '/status');
    const s = await r.json();
    applyStatus(s);
  } catch (e) {
    document.getElementById('dot').className = 'dot off';
    document.getElementById('conn-status').textContent = 'API unreachable';
  }
}

async function sendCodes() {
  const codes = document.getElementById('send-codes').value.trim();
  if (!codes) return;
  try {
    const r = await fetch(API + '/keyboard/send', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: codes
    });
    const j = await r.json();
    if (j.ok) {
      toast('Sent ' + j.keys_sent + ' keys', 'ok');
      document.getElementById('send-codes').value = '';
    } else toast(j.error || 'Error', 'err');
  } catch (e) { toast('Request failed: ' + e.message, 'err'); }
}

function sendHotkey(idx, label, needsConfirm) {
  if (needsConfirm && !confirm("Send '" + label + "'?")) return;
  postAction('/hotkey/' + idx);
}

let hotkeysBuilt = false;
function buildHotkeys(hotkeys) {
  if (hotkeysBuilt || !hotkeys || !hotkeys.length) return;
  hotkeysBuilt = true;
  const c = document.getElementById('hotkey-buttons');
  hotkeys.forEach((h, i) => {
    const btn = document.createElement('button');
    btn.className = 'btn primary';
    btn.textContent = h.label;
    btn.title = 'Send ' + h.label;
    btn.onclick = () => sendHotkey(i, h.label, h.confirm);
    c.appendChild(btn);
  });
}

buildSliders();
refreshStatus();

// SSE for real-time updates, falling back to polling
let sseActive = false;
let pollTimer = setInterval(refreshStatus, 1000);  // start with 1s polling

function connectSSE() {
  const es = new EventSource(API + '/events');
  es.onopen = () => {
    sseActive = true;
    // Slow down polling when SSE is working
    clearInterval(pollTimer);
    pollTimer = setInterval(refreshStatus, 10000);
  };
  es.onmessage = (e) => {
    if (Date.now() < pauseRefreshUntil) return;
    try { applyStatus(JSON.parse(e.data)); } catch(err) {}
  };
  es.onerror = () => {
    if (sseActive) {
      // Was working, now lost — speed up polling
      sseActive = false;
      clearInterval(pollTimer);
      pollTimer = setInterval(refreshStatus, 1000);
    }
    // Let EventSource auto-reconnect (don't close it)
  };
}
connectSSE();
</script>
</body>
</html>
"""
