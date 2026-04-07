"""Tests for the framebuffer module."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vnc2ipkvm.framebuffer import Framebuffer

import unittest


class TestFramebufferInit(unittest.TestCase):

    def test_initial_size(self):
        fb = Framebuffer(640, 480)
        self.assertEqual(fb.width, 640)
        self.assertEqual(fb.height, 480)
        self.assertEqual(len(fb.pixels), 640 * 480)

    def test_initial_pixels_zero(self):
        fb = Framebuffer(10, 10)
        self.assertTrue(all(p == 0 for p in fb.pixels))

    def test_initial_not_dirty(self):
        fb = Framebuffer(100, 100)
        self.assertIsNone(fb.get_dirty_region())


class TestFramebufferResize(unittest.TestCase):

    def test_resize_updates_dimensions(self):
        fb = Framebuffer(100, 100)
        fb.resize(200, 150)
        self.assertEqual(fb.width, 200)
        self.assertEqual(fb.height, 150)
        self.assertEqual(len(fb.pixels), 200 * 150)

    def test_resize_marks_dirty(self):
        fb = Framebuffer(100, 100)
        # Clear initial dirty state
        fb.get_dirty_region()
        fb.resize(200, 150)
        region = fb.get_dirty_region()
        self.assertIsNotNone(region)
        self.assertEqual(region, (0, 0, 200, 150))

    def test_resize_clears_pixels(self):
        fb = Framebuffer(10, 10)
        fb.fill_rect(0, 0, 10, 10, 0xFF)
        fb.resize(10, 10)
        self.assertTrue(all(p == 0 for p in fb.pixels))


class TestPutRaw(unittest.TestCase):

    def test_put_raw_single_row(self):
        fb = Framebuffer(10, 10)
        data = bytes([1, 2, 3, 4, 5])
        fb.put_raw(0, 0, 5, 1, data)
        for i in range(5):
            self.assertEqual(fb.pixels[i], i + 1)

    def test_put_raw_with_offset(self):
        fb = Framebuffer(10, 10)
        data = bytes([0xAA, 0xBB])
        fb.put_raw(3, 2, 2, 1, data)
        self.assertEqual(fb.pixels[2 * 10 + 3], 0xAA)
        self.assertEqual(fb.pixels[2 * 10 + 4], 0xBB)

    def test_put_raw_multi_row(self):
        fb = Framebuffer(10, 10)
        data = bytes([1, 2, 3, 4, 5, 6])
        fb.put_raw(0, 0, 3, 2, data)
        self.assertEqual(fb.pixels[0:3], bytearray([1, 2, 3]))
        self.assertEqual(fb.pixels[10:13], bytearray([4, 5, 6]))

    def test_put_raw_marks_dirty(self):
        fb = Framebuffer(100, 100)
        fb.get_dirty_region()  # clear
        fb.put_raw(10, 20, 5, 3, bytes(15))
        region = fb.get_dirty_region()
        self.assertEqual(region, (10, 20, 5, 3))


class TestCopyRect(unittest.TestCase):

    def test_non_overlapping_copy(self):
        fb = Framebuffer(10, 10)
        fb.put_raw(0, 0, 3, 1, bytes([1, 2, 3]))
        fb.copy_rect(0, 0, 5, 0, 3, 1)
        self.assertEqual(fb.pixels[5], 1)
        self.assertEqual(fb.pixels[6], 2)
        self.assertEqual(fb.pixels[7], 3)

    def test_overlapping_copy_forward(self):
        fb = Framebuffer(10, 1)
        fb.put_raw(0, 0, 5, 1, bytes([1, 2, 3, 4, 5]))
        fb.copy_rect(0, 0, 2, 0, 3, 1)
        self.assertEqual(fb.pixels[2], 1)
        self.assertEqual(fb.pixels[3], 2)
        self.assertEqual(fb.pixels[4], 3)

    def test_copy_rect_marks_dirty(self):
        fb = Framebuffer(100, 100)
        fb.put_raw(0, 0, 3, 1, bytes(3))
        fb.get_dirty_region()  # clear
        fb.copy_rect(0, 0, 10, 10, 3, 1)
        region = fb.get_dirty_region()
        self.assertEqual(region, (10, 10, 3, 1))


class TestFillRect(unittest.TestCase):

    def test_fill_small(self):
        fb = Framebuffer(10, 10)
        fb.fill_rect(2, 3, 4, 2, 0xAB)
        for row in range(2):
            for col in range(4):
                self.assertEqual(fb.pixels[(3 + row) * 10 + (2 + col)], 0xAB)

    def test_fill_masks_color(self):
        fb = Framebuffer(10, 10)
        fb.fill_rect(0, 0, 1, 1, 0x1FF)
        self.assertEqual(fb.pixels[0], 0xFF)

    def test_fill_rect_marks_dirty(self):
        fb = Framebuffer(100, 100)
        fb.get_dirty_region()
        fb.fill_rect(5, 5, 10, 10, 0x42)
        region = fb.get_dirty_region()
        self.assertEqual(region, (5, 5, 10, 10))


class TestDirtyTracking(unittest.TestCase):

    def test_multiple_operations_expand_dirty(self):
        fb = Framebuffer(100, 100)
        fb.get_dirty_region()  # clear
        fb.fill_rect(10, 10, 5, 5, 1)
        fb.fill_rect(50, 50, 5, 5, 2)
        region = fb.get_dirty_region()
        # Should encompass both rects
        self.assertEqual(region, (10, 10, 45, 45))

    def test_get_dirty_clears_state(self):
        fb = Framebuffer(100, 100)
        fb.fill_rect(0, 0, 10, 10, 1)
        fb.get_dirty_region()  # consume
        self.assertIsNone(fb.get_dirty_region())

    def test_get_full_region(self):
        fb = Framebuffer(200, 150)
        region = fb.get_full_region()
        self.assertEqual(region, (0, 0, 200, 150))

    def test_get_full_region_clears_dirty(self):
        fb = Framebuffer(100, 100)
        fb.fill_rect(0, 0, 10, 10, 1)
        fb.get_full_region()
        self.assertIsNone(fb.get_dirty_region())


class TestToBGRX(unittest.TestCase):

    def test_single_black_pixel(self):
        fb = Framebuffer(1, 1)
        result = fb.to_bgrx(0, 0, 1, 1)
        self.assertEqual(result, bytes([0, 0, 0, 0]))

    def test_single_white_pixel(self):
        fb = Framebuffer(1, 1)
        fb.pixels[0] = 0xFF
        result = fb.to_bgrx(0, 0, 1, 1)
        self.assertEqual(result, bytes([255, 255, 255, 0]))

    def test_red_pixel(self):
        fb = Framebuffer(1, 1)
        fb.pixels[0] = 0x07  # Pure red in RGB332
        result = fb.to_bgrx(0, 0, 1, 1)
        # BGRX: B, G, R, X
        self.assertEqual(result[0], 0)    # B
        self.assertEqual(result[1], 0)    # G
        self.assertEqual(result[2], 255)  # R
        self.assertEqual(result[3], 0)    # X

    def test_subregion(self):
        fb = Framebuffer(10, 10)
        fb.pixels[3 * 10 + 5] = 0xFF
        result = fb.to_bgrx(5, 3, 1, 1)
        self.assertEqual(result, bytes([255, 255, 255, 0]))

    def test_output_size(self):
        fb = Framebuffer(100, 100)
        result = fb.to_bgrx(0, 0, 10, 5)
        self.assertEqual(len(result), 10 * 5 * 4)


class TestToRGB888(unittest.TestCase):

    def test_single_black_pixel(self):
        fb = Framebuffer(1, 1)
        result = fb.to_rgb888(0, 0, 1, 1)
        self.assertEqual(result, bytes([0, 0, 0]))

    def test_single_white_pixel(self):
        fb = Framebuffer(1, 1)
        fb.pixels[0] = 0xFF
        result = fb.to_rgb888(0, 0, 1, 1)
        self.assertEqual(result, bytes([255, 255, 255]))

    def test_output_size(self):
        fb = Framebuffer(100, 100)
        result = fb.to_rgb888(0, 0, 10, 5)
        self.assertEqual(len(result), 10 * 5 * 3)


class TestPutPixel(unittest.TestCase):

    def test_put_pixel(self):
        fb = Framebuffer(10, 10)
        with fb.lock:
            fb.put_pixel(5, 3, 0xAB)
        self.assertEqual(fb.pixels[3 * 10 + 5], 0xAB)

    def test_put_pixel_masks_color(self):
        fb = Framebuffer(10, 10)
        with fb.lock:
            fb.put_pixel(0, 0, 0x1AB)
        self.assertEqual(fb.pixels[0], 0xAB)


if __name__ == "__main__":
    unittest.main()
