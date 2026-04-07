"""Color conversion between 8-bit RGB332 and 32-bit RGBA/RGB888.

The Belkin IP-KVM uses an 8-bit color model with DirectColorModel(8, 7, 56, 192):
  - Red:   bits 0-2 (mask 0x07), 3 bits, range 0-7
  - Green: bits 3-5 (mask 0x38), 3 bits, range 0-7
  - Blue:  bits 6-7 (mask 0xC0), 2 bits, range 0-3

The SetPixelFormat message confirms: redMax=7, greenMax=7, blueMax=3,
redShift=0, greenShift=3, blueShift=6.
"""

import struct

# Pre-computed lookup table: RGB332 byte -> 0xAARRGGBB (32-bit ARGB)
# Java's Color.getRGB() returns 0xAARRGGBB with alpha=0xFF.
RGB332_TO_ARGB = [0] * 256

# Pre-computed lookup table: RGB332 byte -> (R8, G8, B8) tuple
RGB332_TO_RGB = [(0, 0, 0)] * 256

def _init_tables():
    for i in range(256):
        r3 = i & 0x07
        g3 = (i >> 3) & 0x07
        b2 = (i >> 6) & 0x03

        r8 = (r3 * 255 + 3) // 7  # round
        g8 = (g3 * 255 + 3) // 7
        b8 = (b2 * 255 + 1) // 3

        RGB332_TO_ARGB[i] = 0xFF000000 | (r8 << 16) | (g8 << 8) | b8
        RGB332_TO_RGB[i] = (r8, g8, b8)

_init_tables()

# Sub-palettes used by Tight encoding
PALETTE_2 = [0x00000000, 0x00FFFFFF]  # black, white (without alpha for now)
PALETTE_4 = [0x00000000, 0x00808080, 0x00C0C0C0, 0x00FFFFFF]
PALETTE_16_GRAY = [
    0x00000000, 0x00212121, 0x00323232, 0x00434343,
    0x005C5C5C, 0x00696969, 0x00757575, 0x00868686,
    0x00979797, 0x00A3A3A3, 0x00B2B2B2, 0x00C1C1C1,
    0x00D1D1D1, 0x00E2E2E2, 0x004F4F4F, 0x00FFFFFF,
]
PALETTE_16_COLOR = [
    0x00000000, 0x00800000, 0x00FF0000, 0x00008000,
    0x00808000, 0x00FFFF00, 0x0000FF00, 0x00000080,
    0x00800080, 0x00008080, 0x00808080, 0x00C0C0C0,
    0x00FF00FF, 0x0000FFFF, 0x00FFFFFF, 0x000000FF,
]

# Add alpha to sub-palettes
for _pal in (PALETTE_2, PALETTE_4, PALETTE_16_GRAY, PALETTE_16_COLOR):
    for _i in range(len(_pal)):
        _pal[_i] = 0xFF000000 | _pal[_i]


def rgb332_to_rgb888_pixel(pixel: int) -> bytes:
    """Convert a single RGB332 pixel to 3 bytes of RGB888."""
    r, g, b = RGB332_TO_RGB[pixel & 0xFF]
    return bytes([r, g, b])


def rgb332_to_rgba_pixel(pixel: int) -> int:
    """Convert a single RGB332 pixel to 32-bit 0xRRGGBB00 (RGBX for VNC)."""
    r, g, b = RGB332_TO_RGB[pixel & 0xFF]
    return (r << 24) | (g << 16) | (b << 8)


def convert_framebuffer_row(src: bytes, dst: bytearray, dst_offset: int, width: int, bpp: int = 4):
    """Convert a row of RGB332 pixels to the VNC pixel format in dst.

    bpp=4: BGRX (32-bit, little-endian as typical VNC uses)
    """
    for i in range(width):
        r, g, b = RGB332_TO_RGB[src[i] & 0xFF]
        off = dst_offset + i * bpp
        # Standard VNC 32-bit: pixel = B | G<<8 | R<<16 (little-endian BGRX)
        dst[off] = b
        dst[off + 1] = g
        dst[off + 2] = r
        dst[off + 3] = 0
