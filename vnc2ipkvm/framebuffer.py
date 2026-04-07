"""Shared framebuffer that stores the KVM's 8-bit image and serves VNC clients.

The framebuffer stores pixels as 8-bit indices (as received from the KVM).
When VNC clients request updates, it converts to 32-bit BGRX using a colour
map that can be updated by SetColourMapEntries messages from the server.
A dirty region tracker keeps track of which areas have changed since the last
VNC update was sent.
"""

import threading
from vnc2ipkvm.color import RGB332_TO_RGB


class Framebuffer:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height)  # 8-bit pixel indices
        self.lock = threading.Lock()
        # Colour map: 256 entries of (R8, G8, B8), default is RGB332
        self._colourmap: list[tuple[int, int, int]] = list(RGB332_TO_RGB)

        # Dirty tracking
        self._dirty = False
        self._dirty_x1 = width
        self._dirty_y1 = height
        self._dirty_x2 = 0
        self._dirty_y2 = 0

    def set_colourmap(self, colourmap: list[tuple[int, int, int]]):
        """Update the colour map used for pixel conversion."""
        with self.lock:
            self._colourmap = list(colourmap)

    def resize(self, width: int, height: int):
        with self.lock:
            self.width = width
            self.height = height
            self.pixels = bytearray(width * height)
            self._mark_all_dirty()

    def put_raw(self, x: int, y: int, w: int, h: int, data: bytes):
        """Write raw 8-bit pixels into the framebuffer."""
        with self.lock:
            stride = self.width
            src_off = 0
            dst_off = y * stride + x
            for row in range(h):
                self.pixels[dst_off:dst_off + w] = data[src_off:src_off + w]
                src_off += w
                dst_off += stride
            self._mark_dirty(x, y, w, h)

    def copy_rect(self, src_x: int, src_y: int, dst_x: int, dst_y: int, w: int, h: int):
        """Copy a rectangle within the framebuffer."""
        with self.lock:
            stride = self.width
            # Handle overlapping copies
            if src_y < dst_y or (src_y == dst_y and src_x < dst_x):
                # Copy bottom-to-top, right-to-left
                for row in range(h - 1, -1, -1):
                    s = (src_y + row) * stride + src_x
                    d = (dst_y + row) * stride + dst_x
                    self.pixels[d:d + w] = self.pixels[s:s + w]
            else:
                for row in range(h):
                    s = (src_y + row) * stride + src_x
                    d = (dst_y + row) * stride + dst_x
                    self.pixels[d:d + w] = self.pixels[s:s + w]
            self._mark_dirty(dst_x, dst_y, w, h)

    def fill_rect(self, x: int, y: int, w: int, h: int, color: int):
        """Fill a rectangle with a single 8-bit color."""
        with self.lock:
            stride = self.width
            row_data = bytes([color & 0xFF]) * w
            off = y * stride + x
            for row in range(h):
                self.pixels[off:off + w] = row_data
                off += stride
            self._mark_dirty(x, y, w, h)

    def put_pixel(self, x: int, y: int, color: int):
        """Set a single pixel (no lock - caller must hold lock)."""
        self.pixels[y * self.width + x] = color & 0xFF

    def get_dirty_region(self):
        """Return the dirty bounding box and clear it. Returns (x, y, w, h) or None."""
        with self.lock:
            if not self._dirty:
                return None
            x1, y1, x2, y2 = self._dirty_x1, self._dirty_y1, self._dirty_x2, self._dirty_y2
            self._dirty = False
            self._dirty_x1 = self.width
            self._dirty_y1 = self.height
            self._dirty_x2 = 0
            self._dirty_y2 = 0
            return (x1, y1, x2 - x1, y2 - y1)

    def get_full_region(self):
        """Return the full framebuffer as a region. Clears dirty state."""
        with self.lock:
            self._dirty = False
            self._dirty_x1 = self.width
            self._dirty_y1 = self.height
            self._dirty_x2 = 0
            self._dirty_y2 = 0
            return (0, 0, self.width, self.height)

    def to_rgb888(self, x: int, y: int, w: int, h: int) -> bytes:
        """Convert a region to RGB888 bytes (3 bytes per pixel)."""
        result = bytearray(w * h * 3)
        stride = self.width
        dst = 0
        cmap = self._colourmap
        with self.lock:
            for row in range(h):
                src_off = (y + row) * stride + x
                for col in range(w):
                    r, g, b = cmap[self.pixels[src_off + col]]
                    result[dst] = r
                    result[dst + 1] = g
                    result[dst + 2] = b
                    dst += 3
        return bytes(result)

    def to_bgrx(self, x: int, y: int, w: int, h: int) -> bytes:
        """Convert a region to BGRX bytes (4 bytes per pixel, standard VNC 32-bit)."""
        result = bytearray(w * h * 4)
        stride = self.width
        dst = 0
        cmap = self._colourmap
        with self.lock:
            for row in range(h):
                src_off = (y + row) * stride + x
                for col in range(w):
                    r, g, b = cmap[self.pixels[src_off + col]]
                    result[dst] = b
                    result[dst + 1] = g
                    result[dst + 2] = r
                    result[dst + 3] = 0
                    dst += 4
        return bytes(result)

    def _mark_dirty(self, x: int, y: int, w: int, h: int):
        """Expand the dirty bounding box (caller must hold lock)."""
        self._dirty = True
        self._dirty_x1 = min(self._dirty_x1, x)
        self._dirty_y1 = min(self._dirty_y1, y)
        self._dirty_x2 = max(self._dirty_x2, x + w)
        self._dirty_y2 = max(self._dirty_y2, y + h)

    def _mark_all_dirty(self):
        self._dirty = True
        self._dirty_x1 = 0
        self._dirty_y1 = 0
        self._dirty_x2 = self.width
        self._dirty_y2 = self.height
