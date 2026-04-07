#!/usr/bin/env python3
"""VNC-to-IPKVM Protocol Translator for Belkin IP-KVM devices.

Usage:
    python -m vnc2ipkvm --host <kvm-ip> --applet-id <id> [options]

This creates a VNC server on localhost:5900 that bridges to the Belkin IP-KVM
device, allowing any standard VNC client to control the KVM.
"""

import argparse
import asyncio
import logging
import signal
import sys

from vnc2ipkvm.control_api import ControlAPI
from vnc2ipkvm.eric_protocol import ERICProtocol, KVMConfig
from vnc2ipkvm.framebuffer import Framebuffer
from vnc2ipkvm.keyboard import get_translator, AVAILABLE_LAYOUTS
from vnc2ipkvm.vnc_server import VNCServer
from vnc2ipkvm.web_login import fetch_applet_params

logger = logging.getLogger(__name__)

# Default framebuffer size (will be updated during handshake)
DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 600


class Bridge:
    """Bridges the e-RIC KVM protocol and the VNC server together."""

    def __init__(self, kvm_config: KVMConfig, vnc_host: str = "0.0.0.0",
                 vnc_port: int = 5900, auto_reconnect: bool = True,
                 keyboard_layout: str = "en_US",
                 api_host: str = "127.0.0.1", api_port: int = 6900,
                 hotkeys: list | None = None):
        self.kvm_config = kvm_config
        self.vnc_host = vnc_host
        self.vnc_port = vnc_port
        self.auto_reconnect = auto_reconnect
        self.keyboard_layout = keyboard_layout
        self.hotkeys = hotkeys or []

        # Initialize keyboard translator for the chosen layout
        self.kbd = get_translator(keyboard_layout)

        # Shared framebuffer
        self.fb = Framebuffer(DEFAULT_WIDTH, DEFAULT_HEIGHT,
                              bytes_per_pixel=kvm_config.bpp // 8)

        # KVM client
        self.kvm = ERICProtocol(kvm_config, self.fb)

        # VNC server
        self.vnc = VNCServer(self.fb, vnc_host, vnc_port,
                             server_name="Belkin IP-KVM",
                             keyboard=self.kbd)

        # Control API (disabled if port is 0)
        self.api = ControlAPI(self, api_host, api_port,
                              vnc_host="127.0.0.1", vnc_port=vnc_port) if api_port else None

        # Wire up callbacks
        self._setup_callbacks()

        self._running = False
        self._kvm_task: asyncio.Task | None = None

    def _setup_callbacks(self):
        """Connect event callbacks between VNC server and KVM client."""
        # KVM -> VNC
        self.kvm.on_bell = self._on_kvm_bell
        self.kvm.on_resize = self._on_kvm_resize
        self.kvm.on_clipboard = self._on_kvm_clipboard
        self.kvm.on_disconnect = self._on_kvm_disconnect
        self.kvm.on_server_command = self._on_kvm_command
        self.kvm.on_server_message = self._on_kvm_message

        # VNC -> KVM
        self.vnc.on_key_event = self._on_vnc_key
        self.vnc.on_pointer_event = self._on_vnc_pointer
        self.vnc.on_clipboard = self._on_vnc_clipboard
        self.vnc.on_client_disconnect = self._on_vnc_client_disconnect

    # ---- KVM -> VNC callbacks ----

    def _on_kvm_bell(self):
        self.vnc.send_bell()

    def _on_kvm_resize(self, width: int, height: int):
        logger.info("KVM desktop resized to %dx%d", width, height)
        self.vnc.notify_resize(width, height)
        self._notify_ui()

    def _on_kvm_clipboard(self, text: str):
        self.vnc.send_clipboard(text)

    def _on_kvm_disconnect(self):
        logger.warning("KVM connection lost")
        self._notify_ui()
        if self.auto_reconnect and self._running:
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self._reconnect_kvm()))

    def _notify_ui(self):
        """Push status to SSE clients if control API is active."""
        if self.api:
            self.api.notify_clients()

    def _on_kvm_command(self, key: str, value: str):
        """Handle server commands like exclusive_mode, rc_users, etc."""
        key_lower = key.lower()
        if key_lower == "exclusive_mode":
            self.kvm.exclusive_mode = value.lower() in ("on", "active")
            logger.info("Exclusive access: %s", value)
        elif key_lower == "rc_users":
            try:
                self.kvm.connected_users = int(value)
            except ValueError:
                pass
            logger.info("Connected users: %s", value)
        elif key_lower == "wlan_quality":
            logger.info("WLAN quality: %s%%", value)
        elif key_lower == "rdp_enabled":
            self.kvm.rdp_available = (value.lower() == "yes")
            logger.info("RDP available: %s", value)
        self._notify_ui()

    def _on_kvm_message(self, message: str, blackout: bool, duration_ms: int):
        """Handle KVM status messages (e.g. 'Please press Auto-Adjust')."""
        self._notify_ui()
        if message and duration_ms > 0:
            # Auto-clear the message after the specified duration
            async def _clear():
                await asyncio.sleep(duration_ms / 1000.0)
                if self.kvm.server_message == message:
                    self.kvm.server_message = ""
                    self._notify_ui()
            asyncio.ensure_future(_clear())

    # ---- VNC -> KVM callbacks ----

    def _on_vnc_key(self, scancode: int, pressed: bool):
        if self.kvm.connected:
            asyncio.ensure_future(self.kvm.send_key_event(scancode, pressed))

    def _on_vnc_pointer(self, x: int, y: int, button_mask: int):
        if self.kvm.connected:
            # Map VNC button mask to e-RIC button mask
            # VNC: bit0=left, bit1=middle, bit2=right, bit3=wheelup, bit4=wheeldown
            # e-RIC: bit0=left, bit1=middle, bit2=right
            eric_mask = button_mask & 0x07
            wheel = 0
            if button_mask & 0x08:
                wheel = -1  # scroll up
            elif button_mask & 0x10:
                wheel = 1   # scroll down
            asyncio.ensure_future(
                self.kvm.send_pointer_event(x, y, eric_mask, wheel))

    def _on_vnc_clipboard(self, text: str):
        # Could forward to KVM clipboard if needed
        pass

    def _on_vnc_client_disconnect(self, held_keys: list[int]):
        """Release all keys held by a disconnecting VNC client."""
        if self.kvm.connected and held_keys:
            logger.info("Releasing %d held keys from disconnected client", len(held_keys))
            for scancode in held_keys:
                asyncio.ensure_future(self.kvm.send_key_event(scancode, False))

    # ---- Main lifecycle ----

    async def run(self):
        """Start the bridge: connect to KVM, start VNC server, run forever."""
        self._running = True

        # Start VNC server and control API
        await self.vnc.start()
        if self.api:
            await self.api.start()

        # Connect to KVM
        await self._connect_kvm()

        # Wait for shutdown
        try:
            stop_event = asyncio.Event()

            def signal_handler():
                logger.info("Shutdown signal received")
                stop_event.set()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, signal_handler)
                except NotImplementedError:
                    # Windows doesn't support add_signal_handler
                    pass

            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _connect_kvm(self):
        """Connect to the KVM and start the protocol loop."""
        try:
            await self.kvm.connect()
            kvm_name = self.kvm.server_name or "Belkin IP-KVM"
            self.vnc.server_name = f"{kvm_name} ({self.kvm_config.host})"
            self._kvm_task = asyncio.create_task(self.kvm.run())
            logger.info("KVM connected and running")
            self._notify_ui()
        except Exception as e:
            logger.error("Failed to connect to KVM: %s", e)
            if self.auto_reconnect:
                asyncio.ensure_future(self._reconnect_kvm())
            else:
                raise

    async def _reconnect_kvm(self):
        """Attempt to reconnect to the KVM after a delay."""
        delay = 5
        logger.info("Reconnecting to KVM in %d seconds...", delay)
        await asyncio.sleep(delay)

        if not self._running:
            return

        # Create a fresh protocol instance
        self.kvm = ERICProtocol(self.kvm_config, self.fb)
        self._setup_callbacks()

        try:
            await self._connect_kvm()
        except Exception as e:
            logger.error("Reconnection failed: %s", e)
            if self._running:
                asyncio.ensure_future(self._reconnect_kvm())

    async def shutdown(self):
        """Clean shutdown of all components."""
        self._running = False
        logger.info("Shutting down...")

        if self._kvm_task:
            self._kvm_task.cancel()
            try:
                await asyncio.wait_for(self._kvm_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        try:
            await asyncio.wait_for(self.kvm.disconnect(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass

        try:
            await asyncio.wait_for(self.vnc.stop(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass

        if self.api:
            try:
                await asyncio.wait_for(self.api.stop(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

        logger.info("Shutdown complete")


def parse_args():
    parser = argparse.ArgumentParser(
        description="VNC-to-IPKVM Protocol Translator for Belkin IP-KVM devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s --host 192.168.1.100 --user admin --password pass
  %(prog)s --host kvm.local --applet-id ABC123
  %(prog)s --host 192.168.1.100 --no-ssl --applet-id ABC123 --vnc-port 5901
""")

    parser.add_argument("--host", required=True,
                        help="IP-KVM hostname or IP address")
    parser.add_argument("--port", type=int, default=443,
                        help="IP-KVM TCP port (default: 443)")
    parser.add_argument("--ssl-port", type=int, default=None,
                        help="IP-KVM SSL port (default: same as --port)")
    parser.add_argument("--applet-id", default=None,
                        help="Session/authentication ID (auto-fetched if omitted)")
    parser.add_argument("--user", default="",
                        help="KVM web login username (for auto-fetching applet ID)")
    parser.add_argument("--password", default="",
                        help="KVM web login password (for auto-fetching applet ID)")
    parser.add_argument("--http-port", type=int, default=80,
                        help="KVM web interface HTTP port (default: 80)")
    parser.add_argument("--protocol-version", default="01.00",
                        help="Protocol version string (default: 01.00)")
    parser.add_argument("--port-id", type=int, default=0,
                        help="KVM port ID (default: 0)")
    parser.add_argument("--no-share", action="store_true",
                        help="Request exclusive access")
    parser.add_argument("--ssl", action="store_true", default=True,
                        help="Use SSL/TLS (default)")
    parser.add_argument("--no-ssl", action="store_true",
                        help="Disable SSL/TLS")

    parser.add_argument("--vnc-host", default="0.0.0.0",
                        help="VNC server listen address (default: 0.0.0.0)")
    parser.add_argument("--vnc-port", type=int, default=5900,
                        help="VNC server listen port (default: 5900)")
    parser.add_argument("--api-host", default="127.0.0.1",
                        help="Control API listen address (default: 127.0.0.1)")
    parser.add_argument("--api-port", type=int, default=6900,
                        help="Control API listen port (default: 6900, 0 to disable)")

    parser.add_argument("--bpp", type=int, default=16, choices=[8, 16],
                        help="Pixel depth: 16 = native RGB565 (default), "
                             "8 = RGB332 with colour map")

    parser.add_argument("--no-reconnect", action="store_true",
                        help="Disable auto-reconnect to KVM")
    parser.add_argument("--norbox", default="no", choices=["no", "ipv4", "ipv6"],
                        help="NORBOX routing mode (default: no)")
    parser.add_argument("--norbox-target", default="",
                        help="NORBOX IPv4/IPv6 target address")
    parser.add_argument("--layout", default="en_US",
                        choices=AVAILABLE_LAYOUTS,
                        help="Keyboard layout (default: en_US)")
    parser.add_argument("--encodings", default="default",
                        help="Encoding preset or comma-separated list. "
                             "Presets: default (255,7,-250), "
                             "compressed (7,-252), "
                             "corre (5), "
                             "tight (7,-250,9). "
                             "Default: default")

    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity (-v info, -vv debug)")

    return parser.parse_args()


def main():
    args = parse_args()

    # Configure logging
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    # Auto-fetch applet parameters if no applet ID given
    applet_id = args.applet_id
    protocol_version = args.protocol_version
    port_id = args.port_id
    ssl_port = args.ssl_port if args.ssl_port else args.port
    use_ssl = not args.no_ssl
    norbox = args.norbox
    norbox_target = args.norbox_target

    if not applet_id:
        print(f"Fetching session from http://{args.host}:{args.http_port}/ ...")
        try:
            params = fetch_applet_params(
                args.host, port_id=port_id,
                username=args.user, password=args.password,
                use_https=False, http_port=args.http_port)
            applet_id = params["APPLET_ID"]
            # Use server-provided values as defaults (CLI args override)
            if args.protocol_version == "01.00" and "PROTOCOL_VERSION" in params:
                protocol_version = params["PROTOCOL_VERSION"]
            if "PORT" in params and args.port == 443:
                try:
                    args.port = int(params["PORT"])
                except ValueError:
                    pass
            if "SSLPORT" in params and args.ssl_port is None:
                try:
                    ssl_port = int(params["SSLPORT"])
                except ValueError:
                    pass
            if "SSL" in params:
                if params["SSL"].lower() == "off" and not args.ssl:
                    use_ssl = False
            if "NORBOX" in params and norbox == "no":
                norbox = params["NORBOX"].lower()
            if "NORBOX_IPV4TARGET" in params and not norbox_target:
                norbox_target = params["NORBOX_IPV4TARGET"]
            if "NORBOX_IPV6TARGET" in params and not norbox_target:
                norbox_target = params["NORBOX_IPV6TARGET"]
            if "PORT_ID" in params and args.port_id == 0:
                try:
                    port_id = int(params["PORT_ID"])
                except ValueError:
                    pass
            print(f"  Session ID: {applet_id[:16]}...")
            print(f"  Protocol:   {protocol_version}")

            # Extract hotkeys from applet params
            hotkeys = []
            i = 0
            while True:
                name = params.get(f"HOTKEY_{i}", "")
                codes = params.get(f"HOTKEYCODE_{i}", "")
                if not name and not codes:
                    break
                if name and codes:
                    confirm = False
                    label = name
                    if label.lower().startswith("confirm "):
                        confirm = True
                        label = label[8:]
                    hotkeys.append({
                        "label": label.strip(),
                        "codes": codes.strip(),
                        "confirm": confirm,
                    })
                i += 1
            if hotkeys:
                print(f"  Hotkeys:   {', '.join(h['label'] for h in hotkeys)}")

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            print("Provide --applet-id manually, or --user/--password for login.",
                  file=sys.stderr)
            sys.exit(1)

    if 'hotkeys' not in dir():
        hotkeys = []  # manual --applet-id mode, no hotkeys from params

    ENCODING_PRESETS = {
        "default":    [255, 7, -250],
        "compressed": [7, -252],
        "corre":      [5],
        "tight":      [7, -250, 9],
    }
    preset = args.encodings.strip().lower()
    if preset in ENCODING_PRESETS:
        encodings = ENCODING_PRESETS[preset]
    else:
        try:
            encodings = [int(x.strip()) for x in args.encodings.split(",")]
        except ValueError:
            print(f"Error: invalid --encodings value: {args.encodings!r}", file=sys.stderr)
            print(f"Use a preset ({', '.join(ENCODING_PRESETS)}) or comma-separated integers.",
                  file=sys.stderr)
            sys.exit(1)

    config = KVMConfig(
        host=args.host,
        port=args.port,
        ssl_port=ssl_port,
        applet_id=applet_id,
        protocol_version=protocol_version,
        port_id=port_id,
        share_desktop=not args.no_share,
        use_ssl=use_ssl,
        encodings=encodings,
        bpp=args.bpp,
        norbox=norbox,
        norbox_ipv4_target=norbox_target if norbox == "ipv4" else "",
        norbox_ipv6_target=norbox_target if norbox == "ipv6" else "",
    )

    # Create and run bridge
    bridge = Bridge(
        kvm_config=config,
        vnc_host=args.vnc_host,
        vnc_port=args.vnc_port,
        auto_reconnect=not args.no_reconnect,
        keyboard_layout=args.layout,
        api_host=args.api_host,
        api_port=args.api_port,
        hotkeys=hotkeys,
    )

    print(f"VNC-to-IPKVM Bridge")
    print(f"  KVM: {config.host}:{config.port} (SSL={'yes' if use_ssl else 'no'})")
    print(f"  VNC: {args.vnc_host}:{args.vnc_port}")
    print(f"  Applet ID: {config.applet_id}")
    print(f"  Keyboard:  {args.layout}")
    print(f"  Encodings: {encodings}")
    print(f"  Colour:    {args.bpp}-bit ({'RGB565' if args.bpp == 16 else 'RGB332'})")
    if args.api_port:
        print(f"  Control:   http://{args.api_host}:{args.api_port}/")
    print()

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
