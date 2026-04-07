"""VNC (RFB) server that presents the KVM framebuffer to standard VNC clients.

Implements RFB protocol version 3.8 with:
  - No authentication (SecurityType None)
  - 32-bit BGRX pixel format (standard for most VNC clients)
  - Raw encoding for framebuffer updates
  - KeyEvent and PointerEvent forwarding
"""

import asyncio
import logging
import struct
import time

from vnc2ipkvm.framebuffer import Framebuffer
from vnc2ipkvm.keyboard import KeyboardTranslator, ModifierTracker

logger = logging.getLogger(__name__)

# VNC client -> server message types
VNC_SET_PIXEL_FORMAT = 0
VNC_SET_ENCODINGS = 2
VNC_FB_UPDATE_REQUEST = 3
VNC_KEY_EVENT = 4
VNC_POINTER_EVENT = 5
VNC_CLIENT_CUT_TEXT = 6


class VNCServer:
    """Listens for VNC client connections and serves the KVM framebuffer."""

    def __init__(self, framebuffer: Framebuffer, listen_host: str = "0.0.0.0",
                 listen_port: int = 5900, server_name: str = "Belkin IP-KVM",
                 keyboard: KeyboardTranslator | None = None):
        self.fb = framebuffer
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.server_name = server_name
        self.keyboard = keyboard or KeyboardTranslator("en_US")
        self._server: asyncio.Server | None = None
        self._clients: list[VNCClientHandler] = []

        # Callbacks for input forwarding (set by the bridge)
        self.on_key_event = None      # (scancode: int, pressed: bool)
        self.on_pointer_event = None  # (x: int, y: int, button_mask: int)
        self.on_clipboard = None      # (text: str)
        self.on_client_disconnect = None  # (held_keys: list[int]) - keys to release

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.listen_host, self.listen_port)
        addr = self._server.sockets[0].getsockname()
        logger.info("VNC server listening on %s:%d", addr[0], addr[1])

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Close all client connections — closing the writer will cause
        # readexactly() in the message loop to raise, unblocking the handler
        for client in self._clients[:]:
            await client.close()
        # Give handlers a moment to finish, then they'll be cleaned up
        # by the event loop
        if self._clients:
            await asyncio.sleep(0.1)

    async def _handle_client(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        logger.info("VNC client connected from %s:%d", addr[0], addr[1])

        client = VNCClientHandler(reader, writer, self)
        self._clients.append(client)
        try:
            self._client_task = asyncio.current_task()
            await client.run()
        except (Exception, asyncio.CancelledError) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.info("VNC client %s:%d disconnected: %s", addr[0], addr[1], e)
        finally:
            if client in self._clients:
                self._clients.remove(client)
            # Release any held keys when client disconnects
            held = client._modifier_tracker.release_all()
            if held and self.on_client_disconnect:
                self.on_client_disconnect(held)
            await client.close()

    def send_bell(self):
        """Send a Bell message to all connected VNC clients."""
        for client in self._clients:
            client.queue_bell()

    def notify_resize(self, width: int, height: int):
        """Notify all clients of a framebuffer resize."""
        for client in self._clients:
            client.queue_resize()

    def send_clipboard(self, text: str):
        """Send clipboard text to all connected VNC clients."""
        for client in self._clients:
            client.queue_clipboard(text)


class VNCClientHandler:
    """Handles a single VNC client connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 server: VNCServer):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.fb = server.fb
        self._closed = False
        self._update_requested = asyncio.Event()
        self._full_update = False
        self._pending_bell = False
        self._pending_resize = False
        self._pending_clipboard: str | None = None
        self._modifier_tracker = ModifierTracker()

        # Client pixel format (default 32-bit BGRX)
        self.bpp = 32
        self.depth = 24
        self.big_endian = False
        self.true_color = True
        self.red_max = 255
        self.green_max = 255
        self.blue_max = 255
        self.red_shift = 16
        self.green_shift = 8
        self.blue_shift = 0

    async def run(self):
        """Main client handler: handshake then message loop."""
        await self._handshake()

        # Run message reader and update sender concurrently
        reader_task = asyncio.create_task(self._message_loop())
        sender_task = asyncio.create_task(self._update_loop())

        try:
            done, pending = await asyncio.wait(
                [reader_task, sender_task],
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                task.result()  # re-raise exceptions
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass

    async def _handshake(self):
        """Perform the RFB 3.8 handshake."""
        # Send protocol version
        self.writer.write(b"RFB 003.008\n")
        await self.writer.drain()

        # Read client version
        client_version = await self.reader.readexactly(12)
        logger.debug("VNC client version: %r", client_version)

        # Security types: offer None (type 1)
        self.writer.write(bytes([1, 1]))  # 1 security type, type 1 (None)
        await self.writer.drain()

        # Read client's chosen security type
        chosen = await self.reader.readexactly(1)
        if chosen[0] != 1:
            # Reject
            self.writer.write(struct.pack(">I", 1))  # failure
            await self.writer.drain()
            raise ConnectionError(f"Client chose unsupported security type {chosen[0]}")

        # SecurityResult: OK
        self.writer.write(struct.pack(">I", 0))
        await self.writer.drain()

        # Client init: shared flag
        shared = await self.reader.readexactly(1)

        # Server init
        name_bytes = self.server.server_name.encode("latin-1")
        server_init = struct.pack(">HH BBBB HHH BBB BBB I",
                                  self.fb.width, self.fb.height,
                                  32, 24, 0, 1,           # bpp=32, depth=24, big_endian=false, true_color=true
                                  255, 255, 255,           # RGB max
                                  16, 8, 0,                # RGB shift (BGRX layout)
                                  0, 0, 0,                 # padding
                                  len(name_bytes))
        self.writer.write(server_init + name_bytes)
        await self.writer.drain()

        logger.info("VNC handshake complete, serving %dx%d framebuffer",
                     self.fb.width, self.fb.height)

    async def _message_loop(self):
        """Read and handle messages from the VNC client."""
        while not self._closed:
            msg_type = (await self.reader.readexactly(1))[0]

            if msg_type == VNC_SET_PIXEL_FORMAT:
                await self._handle_set_pixel_format()
            elif msg_type == VNC_SET_ENCODINGS:
                await self._handle_set_encodings()
            elif msg_type == VNC_FB_UPDATE_REQUEST:
                await self._handle_fb_update_request()
            elif msg_type == VNC_KEY_EVENT:
                await self._handle_key_event()
            elif msg_type == VNC_POINTER_EVENT:
                await self._handle_pointer_event()
            elif msg_type == VNC_CLIENT_CUT_TEXT:
                await self._handle_client_cut_text()
            else:
                logger.warning("Unknown VNC message type: %d", msg_type)
                break

    async def _handle_set_pixel_format(self):
        """Handle SetPixelFormat (type 0)."""
        data = await self.reader.readexactly(19)  # 3 padding + 16 pixel format
        # Parse but we'll always serve in our default format
        self.bpp = data[3]
        self.depth = data[4]
        self.big_endian = bool(data[5])
        self.true_color = bool(data[6])
        self.red_max = struct.unpack(">H", data[7:9])[0]
        self.green_max = struct.unpack(">H", data[9:11])[0]
        self.blue_max = struct.unpack(">H", data[11:13])[0]
        self.red_shift = data[13]
        self.green_shift = data[14]
        self.blue_shift = data[15]
        logger.debug("Client pixel format: %dbpp depth=%d RGB max=(%d,%d,%d) shift=(%d,%d,%d)",
                      self.bpp, self.depth, self.red_max, self.green_max, self.blue_max,
                      self.red_shift, self.green_shift, self.blue_shift)

    async def _handle_set_encodings(self):
        """Handle SetEncodings (type 2)."""
        data = await self.reader.readexactly(3)  # 1 padding + 2 count
        num_encodings = struct.unpack(">H", data[1:3])[0]
        enc_data = await self.reader.readexactly(num_encodings * 4)
        encodings = []
        for i in range(num_encodings):
            enc = struct.unpack(">i", enc_data[i*4:i*4+4])[0]
            encodings.append(enc)
        logger.debug("Client encodings: %s", encodings)

    async def _handle_fb_update_request(self):
        """Handle FramebufferUpdateRequest (type 3)."""
        data = await self.reader.readexactly(9)
        incremental = data[0]
        x = struct.unpack(">H", data[1:3])[0]
        y = struct.unpack(">H", data[3:5])[0]
        w = struct.unpack(">H", data[5:7])[0]
        h = struct.unpack(">H", data[7:9])[0]

        if not incremental:
            self._full_update = True
        self._update_requested.set()

    async def _handle_key_event(self):
        """Handle KeyEvent (type 4): translate keysym and forward to KVM."""
        data = await self.reader.readexactly(7)
        down = data[0]
        keysym = struct.unpack(">I", data[3:7])[0]

        scancode = self.server.keyboard.keysym_to_scancode(keysym)
        if scancode is not None:
            if down:
                self._modifier_tracker.key_pressed(scancode)
            else:
                self._modifier_tracker.key_released(scancode)
            if self.server.on_key_event:
                self.server.on_key_event(scancode, bool(down))
        else:
            logger.debug("Unmapped keysym: 0x%04x", keysym)

    async def _handle_pointer_event(self):
        """Handle PointerEvent (type 5): forward to KVM."""
        data = await self.reader.readexactly(5)
        button_mask = data[0]
        x = struct.unpack(">H", data[1:3])[0]
        y = struct.unpack(">H", data[3:5])[0]

        if self.server.on_pointer_event:
            self.server.on_pointer_event(x, y, button_mask)

    async def _handle_client_cut_text(self):
        """Handle ClientCutText (type 6)."""
        data = await self.reader.readexactly(7)  # 3 padding + 4 length
        length = struct.unpack(">I", data[3:7])[0]
        text_data = await self.reader.readexactly(length)
        text = text_data.decode("latin-1", errors="replace")
        if self.server.on_clipboard:
            self.server.on_clipboard(text)

    async def _update_loop(self):
        """Send framebuffer updates to the client when requested."""
        while not self._closed:
            await self._update_requested.wait()
            self._update_requested.clear()

            try:
                if self._pending_bell:
                    self._pending_bell = False
                    self.writer.write(bytes([2]))  # Bell
                    await self.writer.drain()

                if self._pending_clipboard is not None:
                    text = self._pending_clipboard
                    self._pending_clipboard = None
                    text_bytes = text.encode("latin-1", errors="replace")
                    msg = struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes
                    self.writer.write(msg)
                    await self.writer.drain()

                if self._full_update:
                    self._full_update = False
                    region = self.fb.get_full_region()
                else:
                    region = self.fb.get_dirty_region()

                if region is None:
                    # No changes - send empty update
                    self.writer.write(struct.pack(">BxH", 0, 0))
                    await self.writer.drain()
                    continue

                x, y, w, h = region
                if w <= 0 or h <= 0:
                    continue

                await self._send_fb_update(x, y, w, h)
            except (ConnectionError, BrokenPipeError):
                break

    async def _send_fb_update(self, x: int, y: int, w: int, h: int):
        """Send a FramebufferUpdate with Raw encoding."""
        # Convert to the client's pixel format
        pixel_data = self._convert_pixels(x, y, w, h)

        # FramebufferUpdate header
        header = struct.pack(">BxH", 0, 1)  # type 0, 1 rectangle

        # Rectangle header: Raw encoding (type 0)
        rect_header = struct.pack(">HHHHi", x, y, w, h, 0)

        self.writer.write(header + rect_header + pixel_data)
        await self.writer.drain()

    def _convert_pixels(self, x: int, y: int, w: int, h: int) -> bytes:
        """Convert framebuffer region to the client's pixel format."""
        if (self.bpp == 32 and self.true_color and
                self.red_shift == 16 and self.green_shift == 8 and self.blue_shift == 0 and
                self.red_max == 255 and self.green_max == 255 and self.blue_max == 255 and
                not self.big_endian):
            # Standard BGRX - use optimized path
            return self.fb.to_bgrx(x, y, w, h)
        elif (self.bpp == 32 and self.true_color and not self.big_endian):
            # Non-standard shifts but still 32-bit
            return self._convert_pixels_generic(x, y, w, h)
        else:
            # Fall back to BGRX
            return self.fb.to_bgrx(x, y, w, h)

    def _convert_pixels_generic(self, x: int, y: int, w: int, h: int) -> bytes:
        """Generic pixel conversion for non-standard VNC pixel formats."""
        result = bytearray(w * h * (self.bpp // 8))
        stride = self.fb.width
        dst = 0
        bytes_per_pixel = self.bpp // 8

        with self.fb.lock:
            cmap = self.fb._colourmap
            for row in range(h):
                src_off = (y + row) * stride + x
                for col in range(w):
                    r, g, b = cmap[self.fb.pixels[src_off + col]]
                    pixel = ((r & self.red_max) << self.red_shift |
                             (g & self.green_max) << self.green_shift |
                             (b & self.blue_max) << self.blue_shift)
                    if self.big_endian:
                        for i in range(bytes_per_pixel - 1, -1, -1):
                            result[dst + i] = pixel & 0xFF
                            pixel >>= 8
                    else:
                        for i in range(bytes_per_pixel):
                            result[dst + i] = pixel & 0xFF
                            pixel >>= 8
                    dst += bytes_per_pixel

        return bytes(result)

    def queue_bell(self):
        self._pending_bell = True
        self._update_requested.set()

    def queue_resize(self):
        self._pending_resize = True
        self._full_update = True
        self._update_requested.set()

    def queue_clipboard(self, text: str):
        self._pending_clipboard = text
        self._update_requested.set()

    async def close(self):
        self._closed = True
        self._update_requested.set()
        try:
            # Feed EOF to the reader to unblock any pending readexactly()
            if not self.reader.at_eof():
                self.reader.feed_eof()
        except Exception:
            pass
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
        except Exception:
            pass
