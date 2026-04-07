"""WebSocket-to-TCP proxy for bridging browser VNC clients to the VNC server.

Uses the `websockets` library for a standards-compliant WebSocket server,
relaying binary data between the browser and the raw TCP VNC server.

Usage:
    proxy = WebSocketProxy(vnc_host="127.0.0.1", vnc_port=5900)
    await proxy.start(ws_host="127.0.0.1", ws_port=6901)
    ...
    await proxy.stop()
"""

import asyncio
import logging

try:
    import websockets
    import websockets.server
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

logger = logging.getLogger(__name__)


class WebSocketProxy:
    """Bridges WebSocket connections to a raw TCP VNC server."""

    def __init__(self, vnc_host: str = "127.0.0.1", vnc_port: int = 5900):
        self.vnc_host = vnc_host
        self.vnc_port = vnc_port
        self._server = None

    async def start(self, ws_host: str = "127.0.0.1", ws_port: int = 6901):
        """Start the WebSocket proxy server."""
        if not HAS_WEBSOCKETS:
            logger.warning("websockets library not installed — "
                           "WebSocket proxy disabled (pip install websockets)")
            return
        # Suppress noisy per-frame debug logging from the websockets library
        logging.getLogger("websockets").setLevel(logging.INFO)
        self._server = await websockets.serve(
            self._handle_client, ws_host, ws_port,
            subprotocols=["binary"],
        )
        logger.info("WebSocket proxy listening on ws://%s:%d/", ws_host, ws_port)

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _handle_client(self, ws):
        """Handle a single WebSocket client by bridging to VNC."""
        try:
            vnc_reader, vnc_writer = await asyncio.open_connection(
                self.vnc_host, self.vnc_port)
        except OSError as e:
            logger.error("Cannot connect to VNC server %s:%d: %s",
                         self.vnc_host, self.vnc_port, e)
            await ws.close(1011, "VNC server unavailable")
            return

        logger.info("WebSocket proxy: client connected, bridging to %s:%d",
                     self.vnc_host, self.vnc_port)

        async def ws_to_tcp():
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        vnc_writer.write(message)
                        await vnc_writer.drain()
            except websockets.ConnectionClosed:
                pass

        async def tcp_to_ws():
            try:
                while True:
                    data = await vnc_reader.read(65536)
                    if not data:
                        break
                    await ws.send(data)
            except (websockets.ConnectionClosed, ConnectionError, OSError):
                pass

        try:
            tasks = [asyncio.create_task(ws_to_tcp()),
                     asyncio.create_task(tcp_to_ws())]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        finally:
            try:
                vnc_writer.close()
                await vnc_writer.wait_closed()
            except Exception:
                pass
            logger.info("WebSocket proxy: client disconnected")
