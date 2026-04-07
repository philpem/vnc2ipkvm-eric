"""Tests for the color conversion module."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vnc2ipkvm.color import (
    RGB332_TO_ARGB, RGB332_TO_RGB,
    rgb332_to_rgb888_pixel, rgb332_to_rgba_pixel,
    convert_framebuffer_row,
    PALETTE_2, PALETTE_4, PALETTE_16_GRAY, PALETTE_16_COLOR,
)

import unittest


class TestRGB332Tables(unittest.TestCase):
    """Test the pre-computed RGB332 lookup tables."""

    def test_table_sizes(self):
        self.assertEqual(len(RGB332_TO_ARGB), 256)
        self.assertEqual(len(RGB332_TO_RGB), 256)

    def test_black(self):
        # RGB332 0x00 = R=0, G=0, B=0
        r, g, b = RGB332_TO_RGB[0x00]
        self.assertEqual((r, g, b), (0, 0, 0))
        # ARGB should have alpha=0xFF
        self.assertEqual(RGB332_TO_ARGB[0x00], 0xFF000000)

    def test_white(self):
        # RGB332 0xFF = R=7, G=7, B=3
        r, g, b = RGB332_TO_RGB[0xFF]
        self.assertEqual(r, 255)
        self.assertEqual(g, 255)
        self.assertEqual(b, 255)
        self.assertEqual(RGB332_TO_ARGB[0xFF], 0xFFFFFFFF)

    def test_pure_red(self):
        # R=7(bits 0-2), G=0(bits 3-5), B=0(bits 6-7) => 0x07
        r, g, b = RGB332_TO_RGB[0x07]
        self.assertEqual(r, 255)
        self.assertEqual(g, 0)
        self.assertEqual(b, 0)

    def test_pure_green(self):
        # R=0, G=7(bits 3-5), B=0 => 0x38
        r, g, b = RGB332_TO_RGB[0x38]
        self.assertEqual(r, 0)
        self.assertEqual(g, 255)
        self.assertEqual(b, 0)

    def test_pure_blue(self):
        # R=0, G=0, B=3(bits 6-7) => 0xC0
        r, g, b = RGB332_TO_RGB[0xC0]
        self.assertEqual(r, 0)
        self.assertEqual(g, 0)
        self.assertEqual(b, 255)

    def test_mid_values(self):
        # R=4, G=4, B=2 => 0x04 | (0x04<<3) | (0x02<<6) = 4 | 32 | 128 = 0xA4
        r, g, b = RGB332_TO_RGB[0xA4]
        # r = (4*255+3)//7 = 146
        # g = (4*255+3)//7 = 146
        # b = (2*255+1)//3 = 170
        self.assertEqual(r, (4 * 255 + 3) // 7)
        self.assertEqual(g, (4 * 255 + 3) // 7)
        self.assertEqual(b, (2 * 255 + 1) // 3)

    def test_all_entries_have_alpha(self):
        for i in range(256):
            self.assertEqual(RGB332_TO_ARGB[i] >> 24, 0xFF,
                             f"Entry {i} missing alpha")

    def test_rgb_components_in_range(self):
        for i in range(256):
            r, g, b = RGB332_TO_RGB[i]
            self.assertGreaterEqual(r, 0)
            self.assertLessEqual(r, 255)
            self.assertGreaterEqual(g, 0)
            self.assertLessEqual(g, 255)
            self.assertGreaterEqual(b, 0)
            self.assertLessEqual(b, 255)


class TestPixelConversion(unittest.TestCase):
    """Test individual pixel conversion functions."""

    def test_rgb332_to_rgb888_pixel_black(self):
        result = rgb332_to_rgb888_pixel(0x00)
        self.assertEqual(result, bytes([0, 0, 0]))

    def test_rgb332_to_rgb888_pixel_white(self):
        result = rgb332_to_rgb888_pixel(0xFF)
        self.assertEqual(result, bytes([255, 255, 255]))

    def test_rgb332_to_rgb888_pixel_masks_to_byte(self):
        # Should mask input to 0xFF
        result = rgb332_to_rgb888_pixel(0x1FF)
        self.assertEqual(result, rgb332_to_rgb888_pixel(0xFF))

    def test_rgb332_to_rgba_pixel_black(self):
        result = rgb332_to_rgba_pixel(0x00)
        self.assertEqual(result, 0x00000000)

    def test_rgb332_to_rgba_pixel_white(self):
        result = rgb332_to_rgba_pixel(0xFF)
        # R=255, G=255, B=255 => 0xFFFFFF00
        self.assertEqual(result, 0xFFFFFF00)

    def test_rgb332_to_rgba_pixel_red(self):
        result = rgb332_to_rgba_pixel(0x07)
        # R=255, G=0, B=0 => 0xFF000000
        self.assertEqual(result, 0xFF000000)


class TestFramebufferRowConversion(unittest.TestCase):
    """Test convert_framebuffer_row function."""

    def test_single_black_pixel(self):
        src = bytes([0x00])
        dst = bytearray(4)
        convert_framebuffer_row(src, dst, 0, 1)
        # BGRX: B=0, G=0, R=0, X=0
        self.assertEqual(dst, bytearray([0, 0, 0, 0]))

    def test_single_white_pixel(self):
        src = bytes([0xFF])
        dst = bytearray(4)
        convert_framebuffer_row(src, dst, 0, 1)
        self.assertEqual(dst, bytearray([255, 255, 255, 0]))

    def test_multiple_pixels(self):
        src = bytes([0x00, 0xFF])
        dst = bytearray(8)
        convert_framebuffer_row(src, dst, 0, 2)
        self.assertEqual(dst[0:4], bytearray([0, 0, 0, 0]))
        self.assertEqual(dst[4:8], bytearray([255, 255, 255, 0]))

    def test_offset(self):
        src = bytes([0xFF])
        dst = bytearray(8)
        convert_framebuffer_row(src, dst, 4, 1)
        self.assertEqual(dst[0:4], bytearray([0, 0, 0, 0]))
        self.assertEqual(dst[4:8], bytearray([255, 255, 255, 0]))

    def test_red_pixel_bgrx_order(self):
        src = bytes([0x07])  # Pure red in RGB332
        dst = bytearray(4)
        convert_framebuffer_row(src, dst, 0, 1)
        # BGRX: B=0, G=0, R=255, X=0
        self.assertEqual(dst[0], 0)    # B
        self.assertEqual(dst[1], 0)    # G
        self.assertEqual(dst[2], 255)  # R
        self.assertEqual(dst[3], 0)    # X


class TestPalettes(unittest.TestCase):
    """Test that palette tables are properly initialized."""

    def test_palette_2_size(self):
        self.assertEqual(len(PALETTE_2), 2)

    def test_palette_4_size(self):
        self.assertEqual(len(PALETTE_4), 4)

    def test_palette_16_gray_size(self):
        self.assertEqual(len(PALETTE_16_GRAY), 16)

    def test_palette_16_color_size(self):
        self.assertEqual(len(PALETTE_16_COLOR), 16)

    def test_palettes_have_alpha(self):
        for pal_name, pal in [("PALETTE_2", PALETTE_2), ("PALETTE_4", PALETTE_4),
                               ("PALETTE_16_GRAY", PALETTE_16_GRAY),
                               ("PALETTE_16_COLOR", PALETTE_16_COLOR)]:
            for i, val in enumerate(pal):
                self.assertEqual(val >> 24, 0xFF,
                                 f"{pal_name}[{i}] = 0x{val:08X} missing alpha")

    def test_palette_2_black_and_white(self):
        self.assertEqual(PALETTE_2[0], 0xFF000000)  # black
        self.assertEqual(PALETTE_2[1], 0xFFFFFFFF)  # white


if __name__ == "__main__":
    unittest.main()
