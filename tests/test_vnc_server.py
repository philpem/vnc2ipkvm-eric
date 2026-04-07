"""Tests for the VNC server module."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import struct
import unittest
from unittest.mock import MagicMock, AsyncMock

from vnc2ipkvm.framebuffer import Framebuffer
from vnc2ipkvm.keyboard import KeyboardTranslator
from vnc2ipkvm.vnc_server import (
    VNCServer, VNCClientHandler,
    VNC_SET_PIXEL_FORMAT, VNC_SET_ENCODINGS,
    VNC_FB_UPDATE_REQUEST, VNC_KEY_EVENT,
    VNC_POINTER_EVENT, VNC_CLIENT_CUT_TEXT,
    PSEUDO_DESKTOP_SIZE,
)


class TestVNCServerInit(unittest.TestCase):

    def test_default_values(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        self.assertEqual(server.listen_host, "0.0.0.0")
        self.assertEqual(server.listen_port, 5900)
        self.assertEqual(server.server_name, "Belkin IP-KVM")
        self.assertEqual(server._clients, [])

    def test_custom_values(self):
        fb = Framebuffer(800, 600)
        kbd = KeyboardTranslator("de_DE")
        server = VNCServer(fb, "127.0.0.1", 5901, "My KVM", kbd)
        self.assertEqual(server.listen_host, "127.0.0.1")
        self.assertEqual(server.listen_port, 5901)
        self.assertEqual(server.server_name, "My KVM")
        self.assertEqual(server.keyboard.layout, "de_DE")


class TestVNCServerStartStop(unittest.TestCase):

    def test_start_and_stop(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb, "127.0.0.1", 0)

        async def _test():
            await server.start()
            self.assertIsNotNone(server._server)
            port = server._server.sockets[0].getsockname()[1]
            self.assertGreater(port, 0)
            await server.stop()

        asyncio.get_event_loop().run_until_complete(_test())


class TestVNCServerBroadcasts(unittest.TestCase):

    def test_send_bell(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        client = MagicMock()
        server._clients = [client]
        server.send_bell()
        client.queue_bell.assert_called_once()

    def test_send_bell_no_clients(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        server.send_bell()  # should not raise

    def test_notify_resize(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        client = MagicMock()
        server._clients = [client]
        server.notify_resize(1024, 768)
        client.queue_resize.assert_called_once()

    def test_send_clipboard(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        client = MagicMock()
        server._clients = [client]
        server.send_clipboard("hello")
        client.queue_clipboard.assert_called_once_with("hello")

    def test_multiple_clients(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        c1, c2 = MagicMock(), MagicMock()
        server._clients = [c1, c2]
        server.send_bell()
        c1.queue_bell.assert_called_once()
        c2.queue_bell.assert_called_once()


class TestVNCClientHandlerInit(unittest.TestCase):

    def test_default_pixel_format(self):
        fb = Framebuffer(800, 600)
        server = VNCServer(fb)
        reader = MagicMock()
        writer = MagicMock()
        handler = VNCClientHandler(reader, writer, server)
        self.assertEqual(handler.bpp, 32)
        self.assertEqual(handler.depth, 24)
        self.assertFalse(handler.big_endian)
        self.assertTrue(handler.true_color)
        self.assertEqual(handler.red_max, 255)
        self.assertEqual(handler.green_max, 255)
        self.assertEqual(handler.blue_max, 255)
        self.assertEqual(handler.red_shift, 16)
        self.assertEqual(handler.green_shift, 8)
        self.assertEqual(handler.blue_shift, 0)


class TestVNCHandshake(unittest.TestCase):
    """Test the RFB 3.8 handshake."""

    def _make_handler(self, fb=None):
        if fb is None:
            fb = Framebuffer(800, 600)
        server = VNCServer(fb, server_name="TestKVM")
        reader = asyncio.StreamReader()
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        handler = VNCClientHandler(reader, writer, server)
        return handler, reader, writer

    def test_handshake(self):
        handler, reader, writer = self._make_handler()

        async def _test():
            # Feed client responses into reader
            reader.feed_data(b"RFB 003.008\n")  # client version
            reader.feed_data(bytes([1]))          # choose security type 1 (None)
            reader.feed_data(bytes([1]))          # shared flag

            await handler._handshake()

            # Verify server sent protocol version
            calls = writer.write.call_args_list
            self.assertEqual(calls[0].args[0], b"RFB 003.008\n")

            # Verify security type offer: 1 type, type 1 (None)
            self.assertEqual(calls[1].args[0], bytes([1, 1]))

            # Verify SecurityResult: OK (uint32 = 0)
            self.assertEqual(calls[2].args[0], struct.pack(">I", 0))

            # Verify ServerInit message
            server_init = calls[3].args[0]
            # First 4 bytes: width(2) + height(2)
            w, h = struct.unpack(">HH", server_init[0:4])
            self.assertEqual(w, 800)
            self.assertEqual(h, 600)
            # Pixel format starts at byte 4
            bpp = server_init[4]
            self.assertEqual(bpp, 32)
            # Name length at byte 20
            name_len = struct.unpack(">I", server_init[20:24])[0]
            name = server_init[24:24+name_len].decode("latin-1")
            self.assertEqual(name, "TestKVM")

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handshake_rejects_unsupported_security(self):
        handler, reader, writer = self._make_handler()

        async def _test():
            reader.feed_data(b"RFB 003.008\n")
            reader.feed_data(bytes([2]))  # VNC auth, not None

            with self.assertRaises(ConnectionError):
                await handler._handshake()

        asyncio.get_event_loop().run_until_complete(_test())


class TestVNCMessageHandling(unittest.TestCase):
    """Test individual VNC message handlers."""

    def _make_handler(self, fb=None):
        if fb is None:
            fb = Framebuffer(800, 600)
        server = VNCServer(fb, server_name="TestKVM")
        reader = asyncio.StreamReader()
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        handler = VNCClientHandler(reader, writer, server)
        return handler, reader, writer, server

    def test_handle_set_pixel_format(self):
        handler, reader, writer, _ = self._make_handler()

        async def _test():
            # 3 padding bytes + 16 pixel format bytes = 19
            data = bytearray(19)
            data[3] = 16   # bpp
            data[4] = 15   # depth
            data[5] = 0    # big endian
            data[6] = 1    # true color
            struct.pack_into(">H", data, 7, 31)   # red max
            struct.pack_into(">H", data, 9, 63)   # green max
            struct.pack_into(">H", data, 11, 31)  # blue max
            data[13] = 11  # red shift
            data[14] = 5   # green shift
            data[15] = 0   # blue shift
            reader.feed_data(bytes(data))
            await handler._handle_set_pixel_format()
            self.assertEqual(handler.bpp, 16)
            self.assertEqual(handler.depth, 15)
            self.assertEqual(handler.red_max, 31)
            self.assertEqual(handler.green_max, 63)
            self.assertEqual(handler.blue_max, 31)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_set_encodings(self):
        handler, reader, writer, _ = self._make_handler()

        async def _test():
            # 1 padding + 2 count + n*4 encoding types
            data = struct.pack(">xH", 2)  # 2 encodings
            data += struct.pack(">i", 0)  # Raw
            data += struct.pack(">i", 1)  # CopyRect
            reader.feed_data(data)
            await handler._handle_set_encodings()
            self.assertFalse(handler._supports_desktop_size)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_set_encodings_with_desktop_size(self):
        handler, reader, writer, _ = self._make_handler()

        async def _test():
            data = struct.pack(">xH", 3)  # 3 encodings
            data += struct.pack(">i", 0)  # Raw
            data += struct.pack(">i", PSEUDO_DESKTOP_SIZE)  # DesktopSize
            data += struct.pack(">i", 1)  # CopyRect
            reader.feed_data(data)
            await handler._handle_set_encodings()
            self.assertTrue(handler._supports_desktop_size)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_fb_update_request_full(self):
        handler, reader, writer, _ = self._make_handler()

        async def _test():
            data = struct.pack(">BHHHH", 0, 0, 0, 800, 600)  # non-incremental
            reader.feed_data(data)
            await handler._handle_fb_update_request()
            self.assertTrue(handler._full_update)
            self.assertTrue(handler._update_requested.is_set())

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_fb_update_request_incremental(self):
        handler, reader, writer, _ = self._make_handler()

        async def _test():
            data = struct.pack(">BHHHH", 1, 0, 0, 800, 600)  # incremental
            reader.feed_data(data)
            await handler._handle_fb_update_request()
            self.assertFalse(handler._full_update)
            self.assertTrue(handler._update_requested.is_set())

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_key_event_press(self):
        handler, reader, writer, server = self._make_handler()
        key_events = []
        server.on_key_event = lambda sc, pressed: key_events.append((sc, pressed))

        async def _test():
            # down=1, padding(2), keysym=0x61 ('a')
            data = struct.pack(">BxxI", 1, 0x61)
            reader.feed_data(data)
            await handler._handle_key_event()
            self.assertEqual(len(key_events), 1)
            sc, pressed = key_events[0]
            self.assertEqual(sc, 29)  # 'a' -> scancode 29
            self.assertTrue(pressed)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_key_event_release(self):
        handler, reader, writer, server = self._make_handler()
        key_events = []
        server.on_key_event = lambda sc, pressed: key_events.append((sc, pressed))

        async def _test():
            data = struct.pack(">BxxI", 0, 0x61)  # down=0
            reader.feed_data(data)
            await handler._handle_key_event()
            self.assertEqual(len(key_events), 1)
            _, pressed = key_events[0]
            self.assertFalse(pressed)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_key_event_unmapped(self):
        handler, reader, writer, server = self._make_handler()
        key_events = []
        server.on_key_event = lambda sc, pressed: key_events.append((sc, pressed))

        async def _test():
            data = struct.pack(">BxxI", 1, 0x12345)  # unmapped
            reader.feed_data(data)
            await handler._handle_key_event()
            self.assertEqual(len(key_events), 0)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_key_event_tracks_modifiers(self):
        handler, reader, writer, server = self._make_handler()
        server.on_key_event = lambda sc, pressed: None

        async def _test():
            # Press Shift_L (keysym 0xFFE1 -> scancode 41)
            reader.feed_data(struct.pack(">BxxI", 1, 0xFFE1))
            await handler._handle_key_event()
            self.assertIn(41, handler._modifier_tracker.get_held_keys())

            # Release Shift_L
            reader.feed_data(struct.pack(">BxxI", 0, 0xFFE1))
            await handler._handle_key_event()
            self.assertNotIn(41, handler._modifier_tracker.get_held_keys())

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_pointer_event(self):
        handler, reader, writer, server = self._make_handler()
        pointer_events = []
        server.on_pointer_event = lambda x, y, mask: pointer_events.append((x, y, mask))

        async def _test():
            data = struct.pack(">BHH", 0x01, 100, 200)
            reader.feed_data(data)
            await handler._handle_pointer_event()
            self.assertEqual(len(pointer_events), 1)
            self.assertEqual(pointer_events[0], (100, 200, 0x01))

        asyncio.get_event_loop().run_until_complete(_test())

    def test_handle_client_cut_text(self):
        handler, reader, writer, server = self._make_handler()
        clips = []
        server.on_clipboard = lambda text: clips.append(text)

        async def _test():
            text = b"Hello World"
            data = struct.pack(">xxxI", len(text)) + text
            reader.feed_data(data)
            await handler._handle_client_cut_text()
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0], "Hello World")

        asyncio.get_event_loop().run_until_complete(_test())


class TestPixelConversion(unittest.TestCase):
    """Test _convert_pixels and _convert_pixels_generic."""

    def _make_handler(self, fb=None):
        if fb is None:
            fb = Framebuffer(10, 10)
        server = VNCServer(fb)
        reader = MagicMock()
        writer = MagicMock()
        return VNCClientHandler(reader, writer, server)

    def test_standard_bgrx_fast_path(self):
        fb = Framebuffer(10, 10)
        fb.pixels[0] = 0xFF  # white
        handler = self._make_handler(fb)
        result = handler._convert_pixels(0, 0, 1, 1)
        self.assertEqual(result, bytes([255, 255, 255, 0]))

    def test_non_standard_32bit_generic_path(self):
        fb = Framebuffer(10, 10)
        fb.pixels[0] = 0x07  # pure red in RGB332
        handler = self._make_handler(fb)
        # Set non-standard shifts (RGB instead of BGR)
        handler.red_shift = 0
        handler.green_shift = 8
        handler.blue_shift = 16
        result = handler._convert_pixels(0, 0, 1, 1)
        # Should still produce 4-byte output
        self.assertEqual(len(result), 4)
        # Red channel should be at position 0 (red_shift=0)
        self.assertEqual(result[0], 255)  # R=255 at shift 0

    def test_fallback_to_bgrx(self):
        fb = Framebuffer(10, 10)
        handler = self._make_handler(fb)
        handler.bpp = 16  # non-32-bit falls through to BGRX
        result = handler._convert_pixels(0, 0, 1, 1)
        self.assertEqual(len(result), 4)  # still BGRX


class TestQueueMethods(unittest.TestCase):

    def _make_handler(self):
        fb = Framebuffer(10, 10)
        server = VNCServer(fb)
        reader = MagicMock()
        writer = MagicMock()
        return VNCClientHandler(reader, writer, server)

    def test_queue_bell(self):
        h = self._make_handler()
        h.queue_bell()
        self.assertTrue(h._pending_bell)
        self.assertTrue(h._update_requested.is_set())

    def test_queue_resize(self):
        h = self._make_handler()
        h.queue_resize()
        self.assertTrue(h._pending_resize)
        self.assertTrue(h._full_update)
        self.assertTrue(h._update_requested.is_set())

    def test_queue_clipboard(self):
        h = self._make_handler()
        h.queue_clipboard("test")
        self.assertEqual(h._pending_clipboard, "test")
        self.assertTrue(h._update_requested.is_set())


class TestVNCHandshakeIntegration(unittest.TestCase):
    """Integration test with a real TCP connection."""

    def test_full_vnc_handshake(self):
        fb = Framebuffer(1024, 768)
        server = VNCServer(fb, "127.0.0.1", 0, "IntegrationKVM")

        async def _test():
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # Read server version
            version = await reader.readexactly(12)
            self.assertEqual(version, b"RFB 003.008\n")

            # Send client version
            writer.write(b"RFB 003.008\n")
            await writer.drain()

            # Read security types
            sec = await reader.readexactly(2)
            self.assertEqual(sec[0], 1)  # 1 security type offered
            self.assertEqual(sec[1], 1)  # type None

            # Choose None
            writer.write(bytes([1]))
            await writer.drain()

            # Read SecurityResult
            result = await reader.readexactly(4)
            self.assertEqual(struct.unpack(">I", result)[0], 0)  # OK

            # Send ClientInit (shared)
            writer.write(bytes([1]))
            await writer.drain()

            # Read ServerInit
            server_init = await reader.readexactly(24)
            w, h = struct.unpack(">HH", server_init[0:4])
            self.assertEqual(w, 1024)
            self.assertEqual(h, 768)
            bpp = server_init[4]
            self.assertEqual(bpp, 32)

            name_len = struct.unpack(">I", server_init[20:24])[0]
            name = await reader.readexactly(name_len)
            self.assertEqual(name, b"IntegrationKVM")

            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.1)
            await server.stop()

        asyncio.get_event_loop().run_until_complete(_test())


if __name__ == "__main__":
    unittest.main()
