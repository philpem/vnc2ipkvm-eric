"""Tests for the HTTP Control API and web UI serving."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from vnc2ipkvm.control_api import ControlAPI, VIDEO_SETTINGS, VIDEO_ACTIONS


def make_bridge(connected=True):
    """Create a mock Bridge object with a mock KVM client."""
    bridge = MagicMock()
    bridge.keyboard_layout = "en_US"

    kvm = MagicMock()
    kvm.connected = connected
    kvm.server_name = "Test KVM"
    kvm.server_version_major = 1
    kvm.server_version_minor = 0
    kvm.width = 1024
    kvm.height = 768
    kvm.video_settings = MagicMock(
        brightness=128, contrast=200, contrast_green=200,
        contrast_blue=200, clock=1000, phase=15,
        h_offset=100, v_offset=50,
        h_resolution=1024, v_resolution=768,
        refresh_rate=60,
    )

    # Make all send methods async
    kvm.send_video_setting = AsyncMock()
    kvm.send_video_settings_request = AsyncMock()
    kvm.send_kvm_port_switch = AsyncMock()
    kvm.send_exclusive_access = AsyncMock()
    kvm.send_release_all_modifiers = AsyncMock()
    kvm.send_key_event = AsyncMock()
    kvm.send_mode_command = AsyncMock()

    # State tracking
    kvm.current_port = 0
    kvm.exclusive_mode = None
    kvm.rdp_mode = False
    kvm.rdp_available = None
    kvm.host_direct_mode = False
    kvm.connected_users = None
    kvm.server_message = ""

    bridge.kvm = kvm

    kvm_config = MagicMock()
    kvm_config.host = "test-kvm.local"
    bridge.kvm_config = kvm_config

    vnc = MagicMock()
    vnc._clients = []
    bridge.vnc = vnc

    return bridge


class TestControlAPIRouting(unittest.TestCase):
    """Test the _route method directly to avoid network complexity."""

    def setUp(self):
        self.bridge = make_bridge(connected=True)
        self.api = ControlAPI(self.bridge)

    def _route(self, method, path, body=b""):
        return asyncio.get_event_loop().run_until_complete(
            self.api._route(method, path, body))

    # --- GET / (Web UI) ---

    def test_root_serves_html(self):
        status, body, ct = self._route("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ct)
        self.assertIn("vnc2ipkvm", body)
        self.assertIn("<html", body)
        self.assertIn("</html>", body)

    def test_root_no_trailing_slash(self):
        # With no segments, should also serve the web UI
        status, body, ct = self._route("GET", "/")
        self.assertEqual(status, 200)

    # --- GET /status ---

    def test_status_returns_json(self):
        result = self._route("GET", "/status")
        status, body = result[0], result[1]
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertTrue(body["connected"])
        self.assertEqual(body["server_name"], "Test KVM")
        self.assertEqual(body["framebuffer"]["width"], 1024)
        self.assertEqual(body["framebuffer"]["height"], 768)
        self.assertEqual(body["video_settings"]["brightness"], 128)
        self.assertEqual(body["keyboard_layout"], "en_US")
        self.assertEqual(body["vnc_clients"], 0)

    def test_status_protocol_version(self):
        result = self._route("GET", "/status")
        self.assertEqual(result[1]["protocol_version"], "1.00")

    # --- GET /help ---

    def test_help_returns_endpoints(self):
        result = self._route("GET", "/help")
        status, body = result[0], result[1]
        self.assertEqual(status, 200)
        self.assertIn("endpoints", body)
        endpoints = body["endpoints"]
        self.assertIn("GET /status", endpoints)
        self.assertIn("GET /help", endpoints)
        self.assertIn("POST /video/<setting>/<value>", endpoints)

    # --- POST /video/<setting>/<value> ---

    def test_video_brightness(self):
        result = self._route("POST", "/video/brightness/64")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.assertEqual(result[1]["setting"], "brightness")
        self.assertEqual(result[1]["value"], 64)
        self.bridge.kvm.send_video_settings_request.assert_awaited_once_with(1)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(0, 64)

    def test_video_contrast(self):
        result = self._route("POST", "/video/contrast/200")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(1, 200)

    def test_video_clock(self):
        result = self._route("POST", "/video/clock/2000")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(4, 2000)

    def test_video_phase(self):
        result = self._route("POST", "/video/phase/15")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(5, 15)

    def test_video_out_of_range_low(self):
        result = self._route("POST", "/video/brightness/-1")
        self.assertEqual(result[0], 400)
        self.assertIn("error", result[1])

    def test_video_out_of_range_high(self):
        result = self._route("POST", "/video/brightness/128")
        self.assertEqual(result[0], 400)

    def test_video_clock_max_boundary(self):
        result = self._route("POST", "/video/clock/4320")
        self.assertEqual(result[0], 200)

    def test_video_clock_over_max(self):
        result = self._route("POST", "/video/clock/4321")
        self.assertEqual(result[0], 400)

    def test_video_non_integer_value(self):
        result = self._route("POST", "/video/brightness/abc")
        self.assertEqual(result[0], 400)
        self.assertIn("integer", result[1]["error"])

    def test_video_unknown_setting(self):
        result = self._route("POST", "/video/unknown/42")
        self.assertEqual(result[0], 400)
        self.assertIn("unknown", result[1]["error"])

    def test_video_missing_value(self):
        result = self._route("POST", "/video/brightness")
        self.assertEqual(result[0], 400)

    def test_video_empty_path(self):
        result = self._route("POST", "/video")
        self.assertEqual(result[0], 400)
        self.assertIn("settings", result[1])

    # --- POST /video/<action> ---

    def test_video_auto_adjust(self):
        result = self._route("POST", "/video/auto-adjust")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.assertEqual(result[1]["action"], "auto-adjust")
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(12, 0)

    def test_video_save(self):
        result = self._route("POST", "/video/save")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(10, 0)

    def test_video_undo(self):
        result = self._route("POST", "/video/undo")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(11, 0)

    def test_video_reset_mode(self):
        result = self._route("POST", "/video/reset-mode")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(9, 0)

    def test_video_reset_all(self):
        result = self._route("POST", "/video/reset-all")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_video_setting.assert_awaited_once_with(8, 0)

    # --- POST /kvm/port/<n> ---

    def test_kvm_port_switch(self):
        result = self._route("POST", "/kvm/port/2")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.assertEqual(result[1]["port"], 2)
        self.bridge.kvm.send_kvm_port_switch.assert_awaited_once_with(2)

    def test_kvm_port_invalid(self):
        result = self._route("POST", "/kvm/port/abc")
        self.assertEqual(result[0], 400)

    # --- POST /exclusive/on|off ---

    def test_exclusive_on(self):
        result = self._route("POST", "/exclusive/on")
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["state"], "on")
        self.bridge.kvm.send_exclusive_access.assert_awaited_once_with(True)

    def test_exclusive_off(self):
        result = self._route("POST", "/exclusive/off")
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["state"], "off")
        self.bridge.kvm.send_exclusive_access.assert_awaited_once_with(False)

    def test_exclusive_invalid(self):
        result = self._route("POST", "/exclusive/maybe")
        self.assertEqual(result[0], 400)

    # --- POST /keyboard/release-all ---

    def test_keyboard_release_all(self):
        result = self._route("POST", "/keyboard/release-all")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.bridge.kvm.send_release_all_modifiers.assert_awaited_once()

    def test_keyboard_invalid(self):
        result = self._route("POST", "/keyboard/something")
        self.assertEqual(result[0], 400)

    # --- POST /keyboard/type ---

    def test_keyboard_type(self):
        result = self._route("POST", "/keyboard/type", b"hello")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.assertEqual(result[1]["action"], "type")
        self.assertGreater(result[1]["chars_typed"], 0)
        self.assertEqual(result[1]["chars_total"], 5)

    def test_keyboard_type_empty(self):
        result = self._route("POST", "/keyboard/type", b"")
        self.assertEqual(result[0], 400)

    def test_keyboard_type_uppercase_uses_shift(self):
        result = self._route("POST", "/keyboard/type", b"A")
        self.assertEqual(result[0], 200)
        # Should have sent shift press, key press, key release, shift release
        calls = self.bridge.kvm.send_key_event.await_args_list
        self.assertEqual(len(calls), 4)
        # Shift press
        self.assertEqual(calls[0].args, (41, True))
        # Key release
        self.assertEqual(calls[3].args, (41, False))

    # --- POST /power/<cmd> ---

    def test_rdp_on(self):
        result = self._route("POST", "/rdp/on")
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["ok"])
        self.assertEqual(result[1]["action"], "rdp")
        self.bridge.kvm.send_mode_command.assert_awaited_once_with(0)

    def test_host_direct_on(self):
        result = self._route("POST", "/host-direct/on")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_mode_command.assert_awaited_once_with(2)

    def test_mode_exit(self):
        result = self._route("POST", "/mode/exit")
        self.assertEqual(result[0], 200)
        self.bridge.kvm.send_mode_command.assert_awaited_once_with(3)

    # --- 404 ---

    def test_unknown_endpoint(self):
        result = self._route("GET", "/nonexistent")
        self.assertEqual(result[0], 404)
        self.assertIn("error", result[1])
        self.assertIn("hint", result[1])

    # --- 503 when disconnected ---

    def test_disconnected_video(self):
        self.bridge.kvm.connected = False
        result = self._route("POST", "/video/brightness/128")
        self.assertEqual(result[0], 503)
        self.assertIn("not connected", result[1]["error"])

    def test_disconnected_kvm_port(self):
        self.bridge.kvm.connected = False
        result = self._route("POST", "/kvm/port/1")
        self.assertEqual(result[0], 503)

    def test_disconnected_exclusive(self):
        self.bridge.kvm.connected = False
        result = self._route("POST", "/exclusive/on")
        self.assertEqual(result[0], 503)

    def test_disconnected_keyboard(self):
        self.bridge.kvm.connected = False
        result = self._route("POST", "/keyboard/release-all")
        self.assertEqual(result[0], 503)

    def test_disconnected_rdp(self):
        self.bridge.kvm.connected = False
        result = self._route("POST", "/rdp/on")
        self.assertEqual(result[0], 503)

    def test_status_works_when_disconnected(self):
        self.bridge.kvm.connected = False
        result = self._route("GET", "/status")
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["connected"])

    def test_help_works_when_disconnected(self):
        self.bridge.kvm.connected = False
        result = self._route("GET", "/help")
        self.assertEqual(result[0], 200)

    def test_web_ui_works_when_disconnected(self):
        self.bridge.kvm.connected = False
        result = self._route("GET", "/")
        self.assertEqual(result[0], 200)

    # --- Trailing slash handling ---

    def test_trailing_slash_stripped(self):
        result = self._route("GET", "/status/")
        self.assertEqual(result[0], 200)


class TestVideoSettingsConfig(unittest.TestCase):
    """Test VIDEO_SETTINGS and VIDEO_ACTIONS dictionaries."""

    def test_all_settings_have_valid_ranges(self):
        for name, (sid, vmin, vmax) in VIDEO_SETTINGS.items():
            self.assertIsInstance(sid, int)
            self.assertGreaterEqual(vmin, 0)
            self.assertGreater(vmax, vmin, f"{name} has max <= min")

    def test_all_actions_have_ids(self):
        for name, (sid, default) in VIDEO_ACTIONS.items():
            self.assertIsInstance(sid, int)
            self.assertIsInstance(default, int)

    def test_no_id_overlap(self):
        setting_ids = {sid for sid, _, _ in VIDEO_SETTINGS.values()}
        action_ids = {sid for sid, _ in VIDEO_ACTIONS.values()}
        # Actions use IDs 8-12, settings use 0-7
        self.assertEqual(len(setting_ids & action_ids), 0)

    def test_expected_settings_present(self):
        expected = ["brightness", "contrast", "contrast-red", "contrast-green",
                    "contrast-blue", "clock", "phase", "h-offset", "v-offset"]
        for name in expected:
            self.assertIn(name, VIDEO_SETTINGS)

    def test_expected_actions_present(self):
        expected = ["reset-all", "reset-mode", "save", "undo", "auto-adjust"]
        for name in expected:
            self.assertIn(name, VIDEO_ACTIONS)


class TestHTTPResponseFormatting(unittest.TestCase):
    """Test the _send_response method."""

    def setUp(self):
        self.bridge = make_bridge()
        self.api = ControlAPI(self.bridge)

    def test_json_response(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            self.api._send_response(writer, 200, {"key": "value"}))
        written = writer.write.call_args[0][0]
        header, body = written.split(b"\r\n\r\n", 1)
        header_str = header.decode()
        self.assertIn("HTTP/1.0 200 OK", header_str)
        self.assertIn("application/json", header_str)
        self.assertIn("Access-Control-Allow-Origin: *", header_str)
        self.assertIn("Connection: close", header_str)
        parsed = json.loads(body)
        self.assertEqual(parsed["key"], "value")

    def test_html_response(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            self.api._send_response(writer, 200, "<html>test</html>", "text/html"))
        written = writer.write.call_args[0][0]
        header, body = written.split(b"\r\n\r\n", 1)
        self.assertIn(b"text/html", header)
        self.assertEqual(body, b"<html>test</html>")

    def test_content_length_correct(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            self.api._send_response(writer, 200, {"test": True}))
        written = writer.write.call_args[0][0]
        header_str, body = written.split(b"\r\n\r\n", 1)
        header_str = header_str.decode()
        for line in header_str.split("\r\n"):
            if line.startswith("Content-Length:"):
                cl = int(line.split(":")[1].strip())
                self.assertEqual(cl, len(body))

    def test_status_codes(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        for code, reason in [(200, "OK"), (400, "Bad Request"),
                              (404, "Not Found"), (500, "Internal Server Error"),
                              (503, "Service Unavailable")]:
            asyncio.get_event_loop().run_until_complete(
                self.api._send_response(writer, code, {}))
            written = writer.write.call_args[0][0]
            first_line = written.split(b"\r\n")[0].decode()
            self.assertIn(str(code), first_line)
            self.assertIn(reason, first_line)

    def test_cors_header(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            self.api._send_response(writer, 200, {}))
        written = writer.write.call_args[0][0]
        self.assertIn(b"Access-Control-Allow-Origin: *", written)


class TestWebUIContent(unittest.TestCase):
    """Test the embedded web UI HTML content."""

    def setUp(self):
        self.bridge = make_bridge()
        self.api = ControlAPI(self.bridge)

    def _get_html(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.api._route("GET", "/", b""))
        return result[1]  # HTML string

    def test_has_doctype(self):
        html = self._get_html()
        self.assertTrue(html.startswith("<!DOCTYPE html>"))

    def test_has_title(self):
        html = self._get_html()
        self.assertIn("<title", html)
        self.assertIn("vnc2ipkvm", html)

    def test_has_status_bar(self):
        html = self._get_html()
        self.assertIn('id="status-bar"', html)
        self.assertIn('id="conn-status"', html)

    def test_has_video_sliders(self):
        html = self._get_html()
        self.assertIn('id="video-sliders"', html)
        self.assertIn("Brightness", html)
        self.assertIn("Contrast", html)
        self.assertIn("Clock", html)
        self.assertIn("Phase", html)
        self.assertIn("SLIDERS_STD", html)
        self.assertIn("SLIDERS_ADV", html)
        self.assertIn("toggleMode", html)

    def test_has_video_buttons(self):
        html = self._get_html()
        self.assertIn("Auto Adjust", html)
        self.assertIn("Save", html)
        self.assertIn("Undo", html)
        self.assertIn("Reset Mode", html)
        self.assertIn("Reset All", html)

    def test_has_kvm_port_control(self):
        html = self._get_html()
        self.assertIn('id="kvm-port"', html)
        self.assertIn("Switch", html)

    def test_has_exclusive_access(self):
        html = self._get_html()
        self.assertIn("/exclusive/on", html)
        self.assertIn("/exclusive/off", html)

    def test_has_keyboard_section(self):
        html = self._get_html()
        self.assertIn('id="type-text"', html)
        self.assertIn("Release All Keys", html)

    def test_has_mode_section(self):
        html = self._get_html()
        self.assertIn("Mode", html)
        self.assertIn("/rdp/on", html)
        self.assertIn("/host-direct/on", html)
        self.assertIn("Exclusive Access", html)

    def test_has_sse_and_fallback_refresh(self):
        html = self._get_html()
        self.assertIn("EventSource", html)
        self.assertIn("connectSSE", html)
        self.assertIn("refreshStatus", html)

    def test_has_slider_debounce(self):
        html = self._get_html()
        self.assertIn("150", html)  # 150ms debounce

    def test_has_toast_notification(self):
        html = self._get_html()
        self.assertIn('id="toast"', html)
        self.assertIn("function toast", html)

    def test_javascript_fetch_uses_post(self):
        html = self._get_html()
        self.assertIn("method: 'POST'", html)

    def test_responsive_design(self):
        html = self._get_html()
        self.assertIn("@media", html)
        self.assertIn("max-width: 600px", html)


class TestControlAPIStartStop(unittest.TestCase):
    """Test server start and stop lifecycle."""

    def test_start_and_stop(self):
        bridge = make_bridge()
        api = ControlAPI(bridge, "127.0.0.1", 0)  # port 0 = OS picks

        async def _test():
            await api.start()
            self.assertIsNotNone(api._server)
            addr = api._server.sockets[0].getsockname()
            self.assertGreater(addr[1], 0)
            await api.stop()

        asyncio.get_event_loop().run_until_complete(_test())

    def test_stop_without_start(self):
        bridge = make_bridge()
        api = ControlAPI(bridge)
        asyncio.get_event_loop().run_until_complete(api.stop())


class TestControlAPIHTTPIntegration(unittest.TestCase):
    """Integration tests that exercise the full HTTP path."""

    def setUp(self):
        self.bridge = make_bridge()
        self.api = ControlAPI(self.bridge, "127.0.0.1", 0)
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self.api.start())
        self.port = self.api._server.sockets[0].getsockname()[1]

    def tearDown(self):
        self.loop.run_until_complete(self.api.stop())
        self.loop.close()

    def _request(self, method, path, body=b""):
        async def _do():
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            request = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n"
            writer.write(request.encode() + body)
            await writer.drain()
            response = await reader.read(65536)
            writer.close()
            await writer.wait_closed()
            return response
        return self.loop.run_until_complete(_do())

    def _parse_response(self, response):
        header, _, body = response.partition(b"\r\n\r\n")
        status_line = header.split(b"\r\n")[0].decode()
        status_code = int(status_line.split()[1])
        return status_code, body

    def test_get_status(self):
        resp = self._request("GET", "/status")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["connected"])

    def test_get_help(self):
        resp = self._request("GET", "/help")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn("endpoints", data)

    def test_get_web_ui(self):
        resp = self._request("GET", "/")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)
        self.assertIn(b"vnc2ipkvm", body)

    def test_post_video_setting(self):
        resp = self._request("POST", "/video/brightness/100")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])

    def test_post_exclusive(self):
        resp = self._request("POST", "/exclusive/on")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)

    def test_404(self):
        resp = self._request("GET", "/nonexistent")
        code, body = self._parse_response(resp)
        self.assertEqual(code, 404)

    def test_bad_request(self):
        async def _do():
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.write(b"INVALID\r\n\r\n")
            await writer.drain()
            response = await reader.read(65536)
            writer.close()
            await writer.wait_closed()
            return response
        resp = self.loop.run_until_complete(_do())
        code, _ = self._parse_response(resp)
        self.assertEqual(code, 400)

    def test_keyboard_type_integration(self):
        text = b"hi"
        resp = self._request("POST", "/keyboard/type", text)
        code, body = self._parse_response(resp)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["chars_total"], 2)


if __name__ == "__main__":
    unittest.main()
