"""e-RIC RFB protocol client - connects to and communicates with the Belkin IP-KVM.

This module handles the full protocol lifecycle:
  1. TCP/SSL connection
  2. Authentication handshake
  3. Framebuffer parameter negotiation
  4. Receiving and decoding framebuffer updates
  5. Sending keyboard/mouse input
  6. Session keepalive (ping/pong)
"""

import asyncio
import logging
import ssl
import struct
import zlib
from dataclasses import dataclass, field

from vnc2ipkvm.color import RGB332_TO_ARGB, RGB332_TO_RGB, PALETTE_2, PALETTE_4, PALETTE_16_GRAY, PALETTE_16_COLOR
from vnc2ipkvm.framebuffer import Framebuffer
from vnc2ipkvm.keyboard import keysym_to_scancode, make_key_event, RELEASE_FLAG

logger = logging.getLogger(__name__)

# Message types: Server -> Client
MSG_FRAMEBUFFER_UPDATE = 0
MSG_SET_COLOURMAP = 1
MSG_BELL = 2
MSG_DISCONNECT = 3
MSG_SERVER_CUT_TEXT = 7
MSG_EXTENDED_INFO = 8
MSG_DEVICE_INFO = 9
MSG_UPDATE_PALETTE = 16
MSG_SYNC = 17
MSG_DESKTOP_SIZE = 128
MSG_SERVER_STATUS = 131
MSG_SERVER_COMMAND = 132
MSG_PING = 148
MSG_BANDWIDTH_TEST = 150
MSG_MODE_SWITCH = 161

# Encoding types
ENC_RAW = 0
ENC_COPYRECT = 1
ENC_HEXTILE = 5
ENC_TIGHT_8BIT = 7
ENC_EXTENDED = 9
ENC_TIGHT_PACKED = 10


@dataclass
class VideoSettings:
    """Video settings reported by the KVM (from extended info message type 8)."""
    brightness: int = 0
    contrast: int = 0          # or contrast_red in advanced mode
    contrast_green: int = 0
    contrast_blue: int = 0
    clock: int = 0
    phase: int = 0
    h_offset: int = 0
    v_offset: int = 0
    h_resolution: int = 0
    v_resolution: int = 0
    refresh_rate: int = 0
    v_offset_max: int = 0


@dataclass
class KVMConfig:
    host: str
    port: int = 443
    ssl_port: int = 443
    applet_id: str = ""
    protocol_version: str = "01.00"
    port_id: int = 0
    share_desktop: bool = True
    use_ssl: bool = True
    encodings: list = field(default_factory=lambda: [255, 7, -250])
    norbox: str = "no"              # "no", "ipv4", or "ipv6"
    norbox_ipv4_target: str = ""    # IPv4 target for NORBOX routing
    norbox_ipv6_target: str = ""    # IPv6 target for NORBOX routing


class ERICProtocol:
    """Client for the e-RIC RFB protocol used by Belkin IP-KVM devices."""

    def __init__(self, config: KVMConfig, framebuffer: Framebuffer):
        self.config = config
        self.fb = framebuffer
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.server_version_major = 0
        self.server_version_minor = 0
        self.server_name = ""
        self.width = 0
        self.height = 0
        self.bpp = 8             # bits per pixel reported by server
        self.bytes_per_pixel = 1 # bpp / 8
        self._inflaters: list[zlib.decompressobj | None] = [None] * 4
        self._running = False
        self._write_lock = asyncio.Lock()
        # Colour map: maps 8-bit pixel values to (R8, G8, B8) tuples.
        # Initialized to RGB332, but updated by SetColourMapEntries from server.
        self._colourmap: list[tuple[int, int, int]] = list(RGB332_TO_RGB)
        self._colourmap_applied = False  # True after first colour map is applied

        # Video settings from the KVM
        self.video_settings = VideoSettings()

        # Mode and state tracking
        self.exclusive_mode: bool | None = None  # None = unknown
        self.rdp_mode: bool = False
        self.host_direct_mode: bool = False
        self.rdp_available: bool | None = None
        self.connected_users: int | None = None
        self.current_port: int = config.port_id
        self.server_message: str = ""       # Status/info message from KVM
        self.server_message_blackout: bool = False  # True = black out screen behind message

        # Callbacks
        self.on_bell = None
        self.on_resize = None
        self.on_clipboard = None
        self.on_disconnect = None
        self.on_video_settings = None   # (VideoSettings)
        self.on_server_command = None   # (key: str, value: str)
        self.on_server_message = None   # (message: str, blackout: bool, duration_ms: int)

    async def connect(self):
        """Connect to the KVM and perform the full handshake."""
        if self.config.use_ssl:
            try:
                await self._connect_ssl()
            except Exception as e:
                logger.warning("SSL connection failed: %s, trying plain TCP", e)
                await self._connect_plain()
        else:
            await self._connect_plain()

        await self._handshake()
        self.connected = True
        logger.info("Connected to KVM at %s:%d (%dx%d)",
                     self.config.host, self.config.port,
                     self.width, self.height)

    async def _connect_ssl(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection(
            self.config.host, self.config.ssl_port, ssl=ctx)

    async def _connect_plain(self):
        self.reader, self.writer = await asyncio.open_connection(
            self.config.host, self.config.port)

    async def _read_exactly(self, n: int) -> bytes:
        data = await self.reader.readexactly(n)
        return data

    async def _read_byte(self) -> int:
        data = await self._read_exactly(1)
        return data[0]

    async def _read_u16(self) -> int:
        data = await self._read_exactly(2)
        return struct.unpack(">H", data)[0]

    async def _read_s16(self) -> int:
        data = await self._read_exactly(2)
        return struct.unpack(">h", data)[0]

    async def _read_u32(self) -> int:
        data = await self._read_exactly(4)
        return struct.unpack(">I", data)[0]

    async def _read_compact_len(self) -> int:
        """Read a variable-length integer (1-3 bytes)."""
        b0 = await self._read_byte()
        value = b0 & 0x7F
        if b0 & 0x80:
            b1 = await self._read_byte()
            value |= (b1 & 0x7F) << 7
            if b1 & 0x80:
                b2 = await self._read_byte()
                value |= (b2 & 0xFF) << 14
        return value

    async def _write(self, data: bytes):
        async with self._write_lock:
            self.writer.write(data)
            await self.writer.drain()

    # ---- Handshake ----

    async def _handshake(self):
        """Perform the full e-RIC RFB handshake."""
        # 0. NORBOX target routing (if enabled)
        if self.config.norbox == "ipv4" and self.config.norbox_ipv4_target:
            target = f"IPV4TARGET={self.config.norbox_ipv4_target},"
            await self._write(target.encode("iso-8859-1"))
        elif self.config.norbox == "ipv6" and self.config.norbox_ipv6_target:
            target = f"IPV6TARGET={self.config.norbox_ipv6_target},"
            await self._write(target.encode("iso-8859-1"))

        # 1. Send authentication
        auth_prefix = b"e-RIC AUTH=" + self.config.applet_id.encode("iso-8859-1")
        auth_msg = auth_prefix.ljust(75, b'\x00')
        await self._write(auth_msg)

        # 2. Read auth response / server version
        first_byte = await self._read_byte()

        if first_byte == 3:
            # Disconnect with reason
            reason_code = await self._read_u16()
            reasons = {
                1: "no permission",
                2: "exclusive access active",
                3: "manually rejected",
                4: "server password disabled",
                5: "loopback connection senseless",
                6: "authentication failed",
                7: "access to this KVM port denied",
            }
            msg = reasons.get(reason_code, f"unknown error {reason_code}")
            raise ConnectionRefusedError(f"KVM rejected connection: {msg}")

        # first_byte should be ord('e') = 0x65
        # Read remaining 15 bytes of version string: "-RIC RFB XX.YY\n"
        rest = await self._read_exactly(15)
        version_str = bytes([first_byte]) + rest
        logger.debug("Server version: %r", version_str)

        # Validate format
        if (rest[0:8] != b"-RIC RFB" or rest[8:9] != b" " or
                rest[14:15] != b"\n"):
            # Try alternate: first_byte is 'e', rest starts with "-RIC RFB "
            pass  # Be lenient

        self.server_version_major = (rest[9] - 48) * 10 + (rest[10] - 48)
        self.server_version_minor = (rest[12] - 48) * 10 + (rest[13] - 48)
        logger.info("Server protocol version: %d.%02d",
                     self.server_version_major, self.server_version_minor)

        # 3. Read padding byte + server name
        # The Java client reads two bytes before the name string:
        #   ap.cfr_renamed_7() reads 1 byte, then l() internally reads
        #   another padding byte before readUTF (2-byte length + string).
        await self._read_byte()  # padding (ap.cfr_renamed_7, line 261)
        await self._read_byte()  # padding (inside l(), before readUTF)
        self.server_name = await self._read_string()
        logger.info("Server name: %s", self.server_name)

        # 4. Read padding byte + server info block
        await self._read_byte()  # padding
        info = await self._read_server_info()

        # 5. Send client protocol version
        ver_str = f"e-RIC RFB {self.config.protocol_version}\n"
        await self._write(ver_str.encode("iso-8859-1"))

        # 6. Send share desktop / port ID
        share_byte = 1 if self.config.share_desktop else 0
        await self._write(bytes([share_byte, self.config.port_id & 0xFF]))

        # 7. Read padding byte + framebuffer parameters
        await self._read_byte()  # padding (ap.cfr_renamed_7, line 267)
        await self._read_fb_params()

    async def _read_string(self) -> str:
        """Read a uint16-length-prefixed string."""
        length = await self._read_u16()
        data = await self._read_exactly(length)
        return data.decode("iso-8859-1", errors="replace")

    async def _read_server_info(self):
        """Read the server info block after the server name."""
        has_password = await self._read_byte()
        session_id = await self._read_u16()
        info_len = await self._read_u16()
        info_str = await self._read_exactly(info_len)
        logger.debug("Server info: hasPassword=%d sessionId=%d info=%r",
                      has_password, session_id, info_str)
        return info_str

    async def _read_fb_params(self):
        """Read the framebuffer initialization message (18 data + 3 padding = 21 bytes).

        Java k() reads: 1 byte fbUpdateRequired, 2 width, 2 height, 1 bpp,
        1 depth, 1 bigEndian, 1 trueColor, 2 redMax, 2 greenMax, 2 blueMax,
        1 redShift, 1 greenShift, 1 blueShift, then 3 bytes trailing padding.
        """
        data = await self._read_exactly(21)
        fb_update_required = data[0]
        self.width = struct.unpack(">H", data[1:3])[0]
        self.height = struct.unpack(">H", data[3:5])[0]
        self.bpp = data[5]
        self.bytes_per_pixel = max(1, self.bpp // 8)
        depth = data[6]
        big_endian = data[7]
        true_color = data[8]
        red_max = struct.unpack(">H", data[9:11])[0]
        green_max = struct.unpack(">H", data[11:13])[0]
        blue_max = struct.unpack(">H", data[13:15])[0]
        red_shift = data[15]
        green_shift = data[16]
        blue_shift = data[17]
        # data[18:21] = 3 bytes padding (discarded)

        logger.info("Framebuffer: %dx%d, %dbpp depth=%d, RGB max=(%d,%d,%d) shift=(%d,%d,%d)",
                     self.width, self.height, self.bpp, depth,
                     red_max, green_max, blue_max,
                     red_shift, green_shift, blue_shift)

        self.fb.resize(self.width, self.height)

    # ---- Client -> Server messages ----

    async def send_set_encodings(self):
        """Send SetEncodings (type 0x02)."""
        encs = self.config.encodings
        n = len(encs)
        msg = struct.pack(">BBH", 2, 0, n)
        for enc in encs:
            msg += struct.pack(">i", enc)
        await self._write(msg)

    async def send_fb_update_request(self, x=0, y=0, w=None, h=None, incremental=True):
        """Send FramebufferUpdateRequest (type 0x03)."""
        if w is None:
            w = self.width
        if h is None:
            h = self.height
        msg = struct.pack(">BBHHHH", 3, 1 if incremental else 0, x, y, w, h)
        await self._write(msg)

    async def send_key_event(self, scancode: int, pressed: bool):
        """Send a key event (type 0x04)."""
        await self._write(make_key_event(scancode, pressed))

    async def send_pointer_event(self, x: int, y: int, button_mask: int, wheel: int = 0):
        """Send a pointer event (type 0x05 or 0x93 with wheel).

        Java ap.a(): always 8 bytes = type(1) + buttons(1) + x(2) + y(2) + wheel(2).
        Type is 0x93 for wheel events, 0x05 for regular movement.
        """
        x = max(0, min(x, self.width - 1))
        y = max(0, min(y, self.height - 1))
        if wheel != 0:
            msg = struct.pack(">BBHHh", 0x93, button_mask & 0xFF, x, y, wheel)
        else:
            msg = struct.pack(">BBHHh", 5, button_mask & 0xFF, x, y, 0)
        await self._write(msg)

    async def send_ping_response(self, value: int = 0):
        """Send ping response (type 0x95)."""
        msg = struct.pack(">Bbbbi", 0x95, 0, 0, 0, value)
        await self._write(msg[:8])

    async def send_bandwidth_response(self, phase: int):
        """Send bandwidth response (type 0x97)."""
        await self._write(bytes([0x97, phase & 0xFF]))

    async def send_set_pixel_format(self):
        """Send SetPixelFormat (type 0x00) with 8-bit RGB332 format."""
        msg = struct.pack(">BBBB BBBB HHH BBB BBB",
                          0, 0, 0, 0,           # type + padding
                          8, 8, 0, 1,           # bpp=8, depth=8, big_endian=false, true_color=true
                          7, 7, 3,              # red/green/blue max
                          0, 3, 6,              # red/green/blue shift
                          0, 0, 0)              # padding
        await self._write(msg)

    async def send_command(self, key: str, value: str):
        """Send a key=value command (type 0x87).

        Used for: exclusive access ("exclusive", "on"/"off"),
        exclusive mouse ("exclusive_mouse", "yes"/"no"), etc.
        """
        key_bytes = key.encode("iso-8859-1")
        val_bytes = value.encode("iso-8859-1")
        header = bytes([0x87, len(key_bytes) & 0xFF, len(val_bytes) & 0xFF])
        await self._write(header + key_bytes + val_bytes)

    async def send_single_command(self, cmd: int):
        """Send a single-byte command (type 0x86)."""
        await self._write(bytes([0x86, cmd & 0xFF]))

    async def send_string_command(self, text: str):
        """Send a string command (type 0x88)."""
        text_bytes = text.encode("iso-8859-1")
        await self._write(bytes([0x88, len(text_bytes) & 0xFF]) + text_bytes)

    async def send_kvm_port_switch(self, port: int):
        """Send KVM port switch command (type 0x89)."""
        msg = struct.pack(">BxH", 0x89, port & 0xFFFF)
        await self._write(msg)
        self.current_port = port

    async def send_video_setting(self, setting_id: int, value: int):
        """Send a video setting change (type 0x90).

        Setting IDs: 0=brightness, 1=contrast(red), 2=contrast_green,
        3=contrast_blue, 4=clock, 5=phase, 6=h_offset, 7=v_offset,
        8=reset_all, 9=reset_mode, 10=save, 11=undo, 12=auto_adjust
        """
        msg = struct.pack(">BbH", 0x90, setting_id & 0xFF, value & 0xFFFF)
        await self._write(msg)

    async def send_video_settings_request(self, cmd: int = 1):
        """Send video settings request (type 0x91). cmd=1 to open, cmd=2 to close."""
        await self._write(bytes([0x91, cmd & 0xFF]))

    async def send_mode_command(self, cmd: int):
        """Send RDP/Host Direct mode command (type 0xA0).

        cmd=0: enter RDP mode (Java: "Remote Desktop Mode" menu item)
        cmd=2: enter Host Direct mode (Java: "Host Acceleration Mode" menu item)
        cmd=3: exit mode (sent on session teardown)
        """
        await self._write(bytes([0xA0, cmd & 0xFF]))

    async def send_exclusive_access(self, enable: bool):
        """Convenience: request or release exclusive access."""
        await self.send_command("exclusive", "on" if enable else "off")

    async def send_exclusive_mouse(self, enable: bool):
        """Convenience: enable or disable exclusive (single) mouse mode."""
        await self.send_command("exclusive_mouse", "yes" if enable else "no")

    async def send_auto_adjust_video(self):
        """Send auto-adjust video settings command."""
        await self.send_video_setting(12, 0)

    async def send_release_all_modifiers(self):
        """Release all held modifier keys on the KVM.

        The Java client sends release events for normal modifiers (type 1)
        then for permanent/toggle modifiers (type 3). We approximate this
        by sending release for all common modifier scan codes.
        """
        modifier_scancodes = [41, 53, 54, 58, 55, 57, 28, 85]  # shifts, ctrls, alts, caps, numlock
        for sc in modifier_scancodes:
            await self.send_key_event(sc, pressed=False)

    # ---- Main receive loop ----

    async def run(self):
        """Main protocol loop: send initial requests and process server messages."""
        self._running = True

        # Send SetEncodings first, then SetPixelFormat for 8-bit RGB332.
        # Order matters: Java client (x.java a()) sends SetEncodings first,
        # then SetPixelFormat via ByteColorRFBRenderer.cfr_renamed_9().
        await self.send_set_encodings()
        await self.send_set_pixel_format()
        # After requesting 8bpp, update our state to match
        self.bpp = 8
        self.bytes_per_pixel = 1

        # Request the initial full framebuffer update
        await self.send_fb_update_request(incremental=False)

        # Request current video settings so the control API has values
        await self.send_video_settings_request(1)

        while self._running:
            try:
                # Use a timeout on read so we can send periodic keepalive
                # FBUpdateRequests even when the server is idle. The KVM
                # disconnects after ~60 seconds without traffic.
                msg_type = await asyncio.wait_for(
                    self._read_byte(), timeout=30.0)
            except asyncio.TimeoutError:
                # No message received for 30 seconds — send keepalive
                try:
                    await self.send_fb_update_request(incremental=True)
                except Exception:
                    break
                continue
            except asyncio.IncompleteReadError:
                logger.info("Connection closed by KVM")
                break
            except ConnectionResetError:
                logger.info("Connection reset by KVM")
                break

            try:
                await self._handle_message(msg_type)
            except asyncio.IncompleteReadError:
                logger.error("Unexpected end of stream during message %d", msg_type)
                break
            except Exception:
                logger.exception("Error handling message type %d", msg_type)
                break

            # Send incremental FBUpdateRequest after each message as a
            # keepalive (matching Java client x.java line 183).
            # Skip after SetColourMapEntries and UpdatePalette — these are
            # palette metadata and sending FBUpdateRequest after them creates
            # a feedback loop where the server responds with more palette
            # messages instead of framebuffer data.
            if msg_type not in (MSG_SET_COLOURMAP, MSG_UPDATE_PALETTE):
                try:
                    await self.send_fb_update_request(incremental=True)
                except Exception:
                    break

        self.connected = False
        self._running = False
        if self.on_disconnect:
            self.on_disconnect()

    async def _handle_message(self, msg_type: int):
        if msg_type == MSG_FRAMEBUFFER_UPDATE:
            await self._handle_fb_update()
        elif msg_type == MSG_SET_COLOURMAP:
            await self._handle_set_colourmap()
        elif msg_type == MSG_BELL:
            if self.on_bell:
                self.on_bell()
        elif msg_type == MSG_DISCONNECT:
            reason = await self._read_u16()
            logger.info("Server disconnect: code %d", reason)
            self._running = False
        elif msg_type == MSG_SERVER_CUT_TEXT:
            text = await self._read_cut_text()
            if self.on_clipboard:
                self.on_clipboard(text)
        elif msg_type == MSG_DESKTOP_SIZE:
            await self._handle_desktop_resize()
        elif msg_type == MSG_SERVER_STATUS:
            await self._handle_server_status()
        elif msg_type == MSG_SERVER_COMMAND:
            await self._handle_server_command()
        elif msg_type == MSG_UPDATE_PALETTE:
            await self._handle_palette_update()
        elif msg_type == MSG_PING:
            await self._handle_ping()
        elif msg_type == MSG_BANDWIDTH_TEST:
            await self._handle_bandwidth_test()
        elif msg_type == MSG_EXTENDED_INFO:
            await self._handle_extended_info()
        elif msg_type == MSG_DEVICE_INFO:
            await self._handle_device_info()
        elif msg_type == MSG_SYNC:
            await self._read_exactly(2)  # discard
        elif msg_type == MSG_MODE_SWITCH:
            status = await self._read_byte()
            modes = {0: "Entered RDP Mode", 1: "Left RDP Mode",
                     2: "RDP Mode unavailable", 3: "Entered Host Direct Mode",
                     4: "Left Host Direct Mode", 5: "Host Direct Mode unavailable"}
            logger.info("Mode switch: %s", modes.get(status, f"unknown {status}"))
            if status == 0:
                self.rdp_mode = True
            elif status in (1, 2):
                self.rdp_mode = False
                if status == 2:
                    self.rdp_available = False
                if status == 1:
                    # Left RDP mode — request full framebuffer refresh
                    await self.send_fb_update_request(incremental=False)
            elif status == 3:
                self.host_direct_mode = True
            elif status in (4, 5):
                self.host_direct_mode = False
                if status == 4:
                    # Left Host Direct mode — request full framebuffer refresh
                    await self.send_fb_update_request(incremental=False)
        else:
            # Unknown message type — log but don't disconnect. The stream
            # may be misaligned, but stopping is worse than trying to continue.
            logger.warning("Unknown message type: %d (0x%02x) — skipping", msg_type, msg_type)

    # ---- Framebuffer update handling ----

    async def _handle_fb_update(self):
        """Process a FramebufferUpdate (type 0x00)."""
        await self._read_byte()  # padding
        num_rects = await self._read_u16()
        if num_rects > 0:
            logger.debug("FB update: %d rectangles", num_rects)

        for i in range(num_rects):
            x = await self._read_u16()
            y = await self._read_u16()
            w = await self._read_u16()
            h = await self._read_u16()
            enc_bytes = await self._read_exactly(4)
            enc = struct.unpack(">i", enc_bytes)[0]

            if enc == ENC_RAW:
                await self._decode_raw(x, y, w, h)
            elif enc == ENC_COPYRECT:
                await self._decode_copyrect(x, y, w, h)
            elif enc == ENC_HEXTILE:
                await self._decode_hextile(x, y, w, h)
            elif enc == ENC_TIGHT_8BIT:
                await self._decode_tight(x, y, w, h)
            elif enc == ENC_EXTENDED:
                await self._decode_extended(x, y, w, h)
            elif enc == ENC_TIGHT_PACKED:
                await self._decode_tight_packed(x, y, w, h)
            else:
                raise IOError(f"Unknown encoding type {enc} (0x{enc & 0xFFFFFFFF:08x})")

        # After processing all rects, peek at stream for alignment check
        if num_rects > 0 and hasattr(self.reader, '_buffer') and len(self.reader._buffer) > 0:
            next_byte = self.reader._buffer[0]
            if next_byte not in (0, 1, 2, 3, 7, 8, 9, 16, 17, 128, 131, 132, 148, 150, 161):
                logger.warning("  POST-FB stream peek: 0x%02x — possible desync! "
                               "buffer: %s", next_byte,
                               bytes(self.reader._buffer[:16]).hex(' '))

    async def _decode_raw(self, x: int, y: int, w: int, h: int):
        """Decode Raw encoding (type 0): w*h*bytes_per_pixel bytes of pixel data."""
        data = await self._read_exactly(w * h * self.bytes_per_pixel)
        if self.bytes_per_pixel == 1:
            self.fb.put_raw(x, y, w, h, data)
        else:
            # Convert multi-byte pixels to 8-bit RGB332 for the framebuffer
            self.fb.put_raw(x, y, w, h, self._convert_to_rgb332(data, w * h))

    async def _decode_copyrect(self, x: int, y: int, w: int, h: int):
        """Decode CopyRect encoding (type 1)."""
        src_x = await self._read_u16()
        src_y = await self._read_u16()
        self.fb.copy_rect(src_x, src_y, x, y, w, h)

    async def _read_pixel(self) -> int:
        """Read one pixel in the server's native format and return RGB332."""
        if self.bytes_per_pixel == 1:
            return await self._read_byte()
        data = await self._read_exactly(self.bytes_per_pixel)
        if self.bytes_per_pixel == 2:
            pixel = (data[0] << 8) | data[1]
            r5 = (pixel >> 11) & 0x1F
            g6 = (pixel >> 5) & 0x3F
            b5 = pixel & 0x1F
            r3 = (r5 * 7 + 15) // 31
            g3 = (g6 * 7 + 31) // 63
            b2 = (b5 * 3 + 15) // 31
            return r3 | (g3 << 3) | (b2 << 6)
        return data[0]

    async def _decode_hextile(self, x: int, y: int, w: int, h: int):
        """Decode Hextile encoding (type 5): 16x16 tiles."""
        bg_color = 0
        fg_color = 0
        bpp = self.bytes_per_pixel
        first_tile = True

        for ty in range(y, y + h, 16):
            for tx in range(x, x + w, 16):
                tw = min(16, x + w - tx)
                th = min(16, y + h - ty)

                flags = await self._read_byte()
                if first_tile:
                    logger.debug("    hextile: flags=0x%02x bpp=%d tiles=%dx%d",
                                 flags, bpp,
                                 (w + 15) // 16, (h + 15) // 16)
                    first_tile = False

                if flags & 0x01:
                    # Raw tile
                    tile_data = await self._read_exactly(tw * th * bpp)
                    if bpp == 1:
                        self.fb.put_raw(tx, ty, tw, th, tile_data)
                    else:
                        self.fb.put_raw(tx, ty, tw, th,
                                        self._convert_to_rgb332(tile_data, tw * th))
                    continue

                if flags & 0x02:
                    bg_color = await self._read_pixel()
                self.fb.fill_rect(tx, ty, tw, th, bg_color)

                if flags & 0x04:
                    fg_color = await self._read_pixel()

                if flags & 0x08:
                    num_subrects = await self._read_byte()

                    if flags & 0x10:
                        # Colored subrectangles
                        for _ in range(num_subrects):
                            color = await self._read_pixel()
                            xy_byte = await self._read_byte()
                            wh_byte = await self._read_byte()
                            sx = (xy_byte >> 4) & 0xF
                            sy = xy_byte & 0xF
                            sw = ((wh_byte >> 4) & 0xF) + 1
                            sh = (wh_byte & 0xF) + 1
                            self.fb.fill_rect(tx + sx, ty + sy, sw, sh, color)
                    else:
                        # Monochrome subrectangles
                        for _ in range(num_subrects):
                            xy_byte = await self._read_byte()
                            wh_byte = await self._read_byte()
                            sx = (xy_byte >> 4) & 0xF
                            sy = xy_byte & 0xF
                            sw = ((wh_byte >> 4) & 0xF) + 1
                            sh = (wh_byte & 0xF) + 1
                            self.fb.fill_rect(tx + sx, ty + sy, sw, sh, fg_color)

    async def _decode_tight(self, x: int, y: int, w: int, h: int):
        """Decode Tight encoding (type 7): complex encoding with zlib and palettes."""
        await self._decode_tight_common(x, y, w, h)

    async def _decode_tight_packed(self, x: int, y: int, w: int, h: int):
        """Decode Tight Packed encoding (type 10).

        This reads a control byte first:
          - If bit 0 is clear: falls through to plain Raw decoding.
          - If bit 0 is set: reads w*h bytes in 16x16 tile order, then
            de-interleaves to raster order (matching Java cfr_renamed_11).
        """
        control = await self._read_byte()

        if (control & 1) == 0:
            await self._decode_raw(x, y, w, h)
            return

        # Read all pixel data (in 16x16 tile order)
        n_pixels = w * h
        packed = await self._read_exactly(n_pixels)

        # Clip to framebuffer bounds
        cw = min(w, self.fb.width - x)
        ch = min(h, self.fb.height - y)

        # De-tile: the Java code iterates row-by-row in output order,
        # reading from the packed buffer which is organized in 16x16 blocks.
        # Blocks are laid out left-to-right, top-to-bottom, and within each
        # block pixels are stored column-major (all rows of col 0, then col 1...).
        result = bytearray(cw * ch)
        tile_w = 16
        tile_h = 16

        src_idx = 0
        dst_idx = 0
        blk_base = 0   # start of current block-row in source
        blk_row_count = 0  # row within current block-row
        blk_col_idx = 0  # which block column we're reading from

        for row in range(ch):
            col_in_tile = 0
            src_idx = blk_base + blk_row_count * tile_w
            for col in range(cw):
                result[dst_idx] = packed[src_idx]
                dst_idx += 1
                src_idx += 1
                col_in_tile += 1
                if col_in_tile == tile_w:
                    # Jump to next tile column: skip remaining rows in this tile
                    src_idx += (tile_h - 1) * tile_w
                    col_in_tile = 0

            blk_row_count += 1
            if blk_row_count == tile_h:
                blk_base += tile_h * w
                blk_row_count = 0

        self.fb.put_raw(x, y, cw, ch, bytes(result))

    async def _decode_tight_common(self, x: int, y: int, w: int, h: int):
        """Common tight encoding decoder used by both tight variants."""
        # Read control byte
        control = await self._read_byte()

        # Check for stream resets (low 4 bits)
        for i in range(4):
            if (control >> i) & 1:
                self._inflaters[i] = None

        sub_enc = (control >> 4) & 0x0F

        # Solid fill (always 1-byte RGB332 in tight 8-bit encoding)
        if sub_enc == 0x08:
            color = await self._read_byte()
            self.fb.fill_rect(x, y, w, h, color)
            return

        # Palette fill
        if sub_enc == 0x0F:
            palette_type = await self._read_byte()
            color_byte = await self._read_byte()
            palette = self._get_tight_palette(palette_type)
            if palette:
                color_val = palette[color_byte % len(palette)]
            else:
                color_val = color_byte
            self.fb.fill_rect(x, y, w, h, self._argb_to_rgb332(color_val))
            return

        # Determine packed pixel width based on sub-encoding
        filter_id = sub_enc & 0x03
        stream_idx = (sub_enc >> 2) & 0x03 if sub_enc < 8 else 0

        # Check for 2-color palette mode
        palette_colors = None
        row_bytes = w  # default: 1 byte per pixel

        if (sub_enc | 3) == 7:
            # Has filter
            filter_byte = await self._read_byte()
            filter_type = filter_byte & 0x0F
            palette_depth = (filter_byte >> 4) & 0x0F
            if filter_type == 1:
                # Palette filter
                num_colors = (await self._read_byte()) + 1
                if num_colors == 2:
                    # Read packed 2-color palette
                    palette_colors = [0, 0]
                    if palette_depth == 1:
                        pb = await self._read_byte()
                        palette_colors[0] = PALETTE_2[pb >> 1]
                        palette_colors[1] = PALETTE_2[pb & 1]
                    elif palette_depth == 2:
                        pb = await self._read_byte()
                        palette_colors[0] = PALETTE_4[pb >> 2]
                        palette_colors[1] = PALETTE_4[pb & 3]
                    elif palette_depth == 3:
                        pb = await self._read_byte()
                        palette_colors[0] = PALETTE_16_GRAY[pb >> 4]
                        palette_colors[1] = PALETTE_16_GRAY[pb & 0xF]
                    elif palette_depth == 4:
                        pb = await self._read_byte()
                        palette_colors[0] = PALETTE_16_COLOR[pb >> 4]
                        palette_colors[1] = PALETTE_16_COLOR[pb & 0xF]
                    else:
                        palette_colors[0] = RGB332_TO_ARGB[await self._read_byte()]
                        palette_colors[1] = RGB332_TO_ARGB[await self._read_byte()]
                    row_bytes = (w + 7) // 8
                else:
                    # Multi-color palette: num_colors * bpp bytes for palette,
                    # then indexed pixel data
                    palette_lut = []
                    for _ in range(num_colors):
                        palette_lut.append(await self._read_pixel())
                    # Each pixel is an index into the palette, 1 byte per pixel
                    row_bytes = w
            elif filter_type != 0:
                raise IOError(f"Unsupported tight filter type: {filter_type}")
        else:
            # No explicit filter, sub_enc determines packing
            if sub_enc == 10:
                row_bytes = (w + 7) // 8
            elif sub_enc == 11:
                row_bytes = (w + 3) // 4
            elif sub_enc in (12, 13):
                row_bytes = (w + 1) // 2

        # Read pixel data (possibly zlib compressed)
        data_len = h * row_bytes
        if data_len < 12:
            pixel_data = await self._read_exactly(data_len)
        else:
            zlib_len = await self._read_compact_len()
            zlib_data = await self._read_exactly(zlib_len)
            pixel_data = self._inflate(stream_idx, zlib_data, data_len)

        # Decode into framebuffer
        if palette_colors is not None:
            # 2-color palette: 1 bit per pixel
            self._decode_tight_2color(x, y, w, h, pixel_data, palette_colors, row_bytes)
        else:
            self._decode_tight_pixels(x, y, w, h, pixel_data, sub_enc, row_bytes)

    def _get_tight_palette(self, palette_type: int):
        palettes = {1: PALETTE_2, 2: PALETTE_4, 3: PALETTE_16_GRAY, 4: PALETTE_16_COLOR}
        return palettes.get(palette_type)

    def _convert_to_rgb332(self, data: bytes, num_pixels: int) -> bytes:
        """Convert pixel data from the server's native format to 8-bit RGB332.

        Handles 16-bit RGB565 (redMax=31, greenMax=63, blueMax=31,
        redShift=11, greenShift=5, blueShift=0) and other formats.
        """
        result = bytearray(num_pixels)
        bpp = self.bytes_per_pixel
        for i in range(num_pixels):
            if bpp == 2:
                # Big-endian 16-bit pixel
                pixel = (data[i * 2] << 8) | data[i * 2 + 1]
                # Extract RGB565 components and convert to RGB332
                r5 = (pixel >> 11) & 0x1F
                g6 = (pixel >> 5) & 0x3F
                b5 = pixel & 0x1F
                # Scale: R 5-bit->3-bit, G 6-bit->3-bit, B 5-bit->2-bit
                r3 = (r5 * 7 + 15) // 31
                g3 = (g6 * 7 + 31) // 63
                b2 = (b5 * 3 + 15) // 31
                result[i] = r3 | (g3 << 3) | (b2 << 6)
            else:
                # For other bpp, take first byte as-is
                result[i] = data[i * bpp]
        return bytes(result)

    def _argb_to_rgb332(self, argb: int) -> int:
        """Convert ARGB888 to RGB332."""
        r = (argb >> 16) & 0xFF
        g = (argb >> 8) & 0xFF
        b = argb & 0xFF
        return ((r * 7 + 127) // 255) | (((g * 7 + 127) // 255) << 3) | (((b * 3 + 127) // 255) << 6)

    def _inflate(self, stream_idx: int, data: bytes, expected_len: int) -> bytes:
        """Decompress data using the specified zlib stream."""
        if self._inflaters[stream_idx] is None:
            self._inflaters[stream_idx] = zlib.decompressobj()
        result = self._inflaters[stream_idx].decompress(data, expected_len)
        if len(result) < expected_len:
            # May need more data from the stream
            result += self._inflaters[stream_idx].flush()
        return result[:expected_len]

    def _decode_tight_2color(self, x, y, w, h, data, palette, row_bytes):
        """Decode 1-bit-per-pixel data with a 2-color palette."""
        row = bytearray(w)
        src = 0
        for j in range(h):
            px = 0
            for byte_idx in range(w // 8):
                b = data[src + byte_idx]
                for bit in range(7, -1, -1):
                    color = palette[(b >> bit) & 1]
                    row[px] = self._argb_to_rgb332(color)
                    px += 1
            # Handle remaining pixels
            if w % 8:
                b = data[src + w // 8]
                for bit in range(7, 7 - (w % 8), -1):
                    color = palette[(b >> bit) & 1]
                    row[px] = self._argb_to_rgb332(color)
                    px += 1
            src += row_bytes
            self.fb.put_raw(x, y + j, w, 1, bytes(row[:w]))

    def _decode_tight_pixels(self, x, y, w, h, data, sub_enc, row_bytes):
        """Decode tight pixel data into the framebuffer."""
        row = bytearray(w)
        src = 0

        for j in range(h):
            if sub_enc == 10:
                # 1-bit: 2-color greyscale palette
                px = 0
                for byte_idx in range(w // 8):
                    b = data[src + byte_idx]
                    for bit in range(7, -1, -1):
                        idx = (b >> bit) & 1
                        row[px] = self._argb_to_rgb332(PALETTE_2[idx])
                        px += 1
                if w % 8:
                    b = data[src + w // 8]
                    for bit in range(7, 7 - (w % 8), -1):
                        idx = (b >> bit) & 1
                        row[px] = self._argb_to_rgb332(PALETTE_2[idx])
                        px += 1
            elif sub_enc == 11:
                # 2-bit: 4-color greyscale
                px = 0
                for byte_idx in range(w // 4):
                    b = data[src + byte_idx]
                    for shift in (6, 4, 2, 0):
                        idx = (b >> shift) & 3
                        row[px] = self._argb_to_rgb332(PALETTE_4[idx])
                        px += 1
                rem = w % 4
                if rem:
                    b = data[src + w // 4]
                    for s in range(rem):
                        idx = (b >> (6 - 2 * s)) & 3
                        row[px] = self._argb_to_rgb332(PALETTE_4[idx])
                        px += 1
            elif sub_enc in (12, 13):
                # 4-bit: 16-color
                palette = PALETTE_16_COLOR if sub_enc == 13 else PALETTE_16_GRAY
                px = 0
                for byte_idx in range(w // 2):
                    b = data[src + byte_idx]
                    row[px] = self._argb_to_rgb332(palette[(b >> 4) & 0xF])
                    px += 1
                    row[px] = self._argb_to_rgb332(palette[b & 0xF])
                    px += 1
                if w % 2:
                    b = data[src + w // 2]
                    row[px] = self._argb_to_rgb332(palette[(b >> 4) & 0xF])
                    px += 1
            else:
                # 8-bit: direct RGB332
                row[:w] = data[src:src + w]

            src += row_bytes
            self.fb.put_raw(x, y + j, w, 1, bytes(row[:w]))

    async def _decode_extended(self, x: int, y: int, w: int, h: int):
        """Decode Extended encoding (type 9).

        This is a complex encoding with tile-level compression control.
        We handle the basics; fall back to raw-like decoding for edge cases.
        """
        control = await self._read_byte()
        stream_idx = (control >> 4) & 0x03
        sub_enc = control & 0x0F
        tile_size = 16

        # Determine compression parameters
        match sub_enc:
            case 1: pixels_per_byte = 8
            case 2: pixels_per_byte = 4
            case 3 | 4: pixels_per_byte = 2
            case 8: pixels_per_byte = 1
            case _: return  # unsupported

        # Calculate aligned tile rows
        y_aligned_end = ((y + h) // tile_size) * tile_size - y if (y + h) % tile_size else h
        num_tile_entries = (w // tile_size) * (y_aligned_end // tile_size)

        # Read tile control data
        if num_tile_entries > 0:
            if num_tile_entries < 12:
                tile_data = await self._read_exactly(num_tile_entries)
            else:
                zlib_len = await self._read_compact_len()
                zlib_data = await self._read_exactly(zlib_len)
                tile_data = self._inflate(stream_idx, zlib_data, num_tile_entries)
        else:
            tile_data = b''

        # Now decode the actual pixel data using the tight common path
        await self._decode_tight_common(x, y, w, h)

    # ---- Other message handlers ----

    async def _read_cut_text(self) -> str:
        await self._read_byte()  # padding
        text = ""
        # The server uses writeUTF-style encoding
        try:
            length = await self._read_u16()
            data = await self._read_exactly(length)
            text = data.decode("utf-8", errors="replace")
        except Exception:
            pass
        return text

    async def _handle_set_colourmap(self):
        """Handle SetColourMapEntries (type 0x01): update the colour map.

        Standard RFB format: 1 padding + 2 first-colour + 2 num-colours,
        then num-colours * 6 bytes (uint16 R, G, B each).

        NOTE: The Java client throws on msg type 1 — it never reads this
        message. The e-RIC format may differ from standard RFB. We log
        the raw header bytes to verify the format.
        """
        # Read the 5-byte header and log raw values for diagnostics
        header = await self._read_exactly(5)
        padding = header[0]
        first_colour = (header[1] << 8) | header[2]
        num_colours = (header[3] << 8) | header[4]
        body_len = num_colours * 6

        logger.debug("SetColourMapEntries: first=%d count=%d",
                     first_colour, num_colours)

        # Sanity check — if values look wrong, the format may differ
        if num_colours > 256 or first_colour > 255:
            logger.warning("SetColourMapEntries: suspicious values "
                           "first=%d count=%d — possible format mismatch, "
                           "next bytes: %s",
                           first_colour, num_colours,
                           (await self._read_exactly(min(16, body_len))).hex(' ')
                           if body_len > 0 else "")
            return

        data = await self._read_exactly(body_len)

        # Update the colour map with the new entries
        for i in range(num_colours):
            off = i * 6
            # Each component is uint16, take the high byte for 8-bit
            r = data[off] if off < len(data) else 0
            g = data[off + 2] if off + 2 < len(data) else 0
            b = data[off + 4] if off + 4 < len(data) else 0
            idx = first_colour + i
            if idx < 256:
                self._colourmap[idx] = (r, g, b)

        # Apply the colour map to the framebuffer rendering.
        colourmap_changed = self._colourmap != self.fb._colourmap
        if colourmap_changed or not self._colourmap_applied:
            self.fb.set_colourmap(self._colourmap)
            if not self._colourmap_applied:
                logger.info("Colour map applied (%d entries)", num_colours)
            self._colourmap_applied = True

    async def _handle_desktop_resize(self):
        """Handle desktop size change (type 0x80)."""
        old_w, old_h = self.width, self.height
        await self._read_fb_params()
        # _read_fb_params reads the server's native pixel format (e.g. 16bpp)
        # but we've already sent SetPixelFormat requesting 8bpp RGB332.
        # Override bpp back to what we requested.
        self.bpp = 8
        self.bytes_per_pixel = 1
        logger.info("Desktop resized: %dx%d -> %dx%d", old_w, old_h, self.width, self.height)
        if self.on_resize:
            self.on_resize(self.width, self.height)

    async def _handle_server_status(self):
        """Handle server status message (type 0x83).

        Java ap.d(): 3 padding bytes + readInt (4 bytes) + data.
        """
        await self._read_exactly(3)  # padding
        length = await self._read_u32()
        data = await self._read_exactly(length)
        msg = data.decode("iso-8859-1", errors="replace")
        logger.info("Server status: %s", msg)

    async def _handle_server_command(self):
        """Handle server command (type 0x84): key=value pairs."""
        await self._read_byte()  # padding
        key_len = await self._read_u16()
        val_len = await self._read_u16()
        key = (await self._read_exactly(key_len)).decode("iso-8859-1", errors="replace")
        val = (await self._read_exactly(val_len)).decode("iso-8859-1", errors="replace")
        logger.info("Server command: %s=%s", key, val)
        if self.on_server_command:
            self.on_server_command(key, val)

    async def _handle_palette_update(self):
        """Handle palette update (type 0x10).

        This shares the same format as the handshake server info: 1 byte
        hasPassword flag, 2 bytes sessionId (used as display duration in ms),
        2 bytes info_length, then the info string.

        The Java applet displays the info string as a 20px banner overlaid
        on the framebuffer. If hasPassword is set, the screen behind is
        blacked out. The banner auto-dismisses after sessionId milliseconds.
        """
        has_password = await self._read_byte()
        duration_ms = await self._read_u16()
        data_len = await self._read_u16()
        data = await self._read_exactly(data_len)
        info = data.decode("iso-8859-1", errors="replace") if data_len > 0 else ""

        self.server_message = info
        self.server_message_blackout = (has_password == 1)
        if info:
            logger.info("KVM message: %s (duration=%dms, blackout=%s)",
                        info, duration_ms, self.server_message_blackout)
        if self.on_server_message:
            self.on_server_message(info, has_password == 1, duration_ms)

    async def _handle_ping(self):
        """Handle ping (type 0x94) and send response.

        Java ap.b(): 3 padding bytes + readInt (4 bytes) = 7 bytes total.
        """
        await self._read_byte()  # padding
        await self._read_byte()  # padding
        await self._read_byte()  # padding
        value = await self._read_u32()
        await self.send_ping_response(0)
        logger.debug("Ping received (value=%d), pong sent", value)

    async def _handle_bandwidth_test(self):
        """Handle bandwidth test (type 0x96).

        Java ap.cfr_renamed_0(): 1 padding byte + readShort (2 bytes) + data.
        """
        await self.send_bandwidth_response(1)
        await self._read_byte()  # padding (Java: cfr_renamed_12 before cfr_renamed_5)
        data_len = await self._read_s16()
        await self._read_exactly(data_len)
        await self.send_bandwidth_response(2)
        logger.debug("Bandwidth test completed (%d bytes)", data_len)

    async def _handle_extended_info(self):
        """Handle extended info (type 0x08): video settings from KVM.

        Java ap.e(): 1 padding + 4 bytes (brightness, contrast, contrast_green,
        contrast_blue) + 8 uint16s (clock, phase, h/v_offset, h/v_resolution,
        refresh_rate, v_offset_max) = 21 bytes total.
        """
        await self._read_byte()  # padding
        vs = self.video_settings
        vs.brightness = await self._read_byte()
        vs.contrast = await self._read_byte()
        vs.contrast_green = await self._read_byte()
        vs.contrast_blue = await self._read_byte()
        vs.clock = await self._read_u16()
        vs.phase = await self._read_u16()
        vs.h_offset = await self._read_u16()
        vs.v_offset = await self._read_u16()
        vs.h_resolution = await self._read_u16()
        vs.v_resolution = await self._read_u16()
        vs.refresh_rate = await self._read_u16()
        vs.v_offset_max = await self._read_u16()
        logger.info("Video settings: %dx%d @%dHz, brightness=%d contrast=%d",
                     vs.h_resolution, vs.v_resolution, vs.refresh_rate,
                     vs.brightness, vs.contrast)
        if self.on_video_settings:
            self.on_video_settings(vs)

    async def _handle_device_info(self):
        """Handle device info (type 0x09)."""
        await self._read_byte()  # padding
        length = await self._read_u16()
        data = await self._read_exactly(length)
        logger.info("Device info: %s", data.decode("iso-8859-1", errors="replace"))

    async def disconnect(self):
        """Close the connection."""
        self._running = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False
