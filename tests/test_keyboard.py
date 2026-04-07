"""Tests for the keyboard translation module."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vnc2ipkvm.keyboard import (
    build_keymap, KeyboardTranslator, ModifierTracker,
    get_translator, keysym_to_scancode, make_key_event,
    AVAILABLE_LAYOUTS, MODIFIER_SCANCODES, RELEASE_FLAG,
    _SPECIAL_KEYS, _CHARS_EN_US,
)

import unittest


class TestBuildKeymap(unittest.TestCase):

    def test_en_us_includes_special_keys(self):
        km = build_keymap("en_US")
        # F1
        self.assertEqual(km[0xFFBE], 60)
        # Enter
        self.assertEqual(km[0xFF0D], 27)
        # Escape
        self.assertEqual(km[0xFF1B], 59)

    def test_en_us_includes_letters(self):
        km = build_keymap("en_US")
        self.assertEqual(km[0x61], 29)  # 'a'
        self.assertEqual(km[0x41], 29)  # 'A'
        self.assertEqual(km[0x7A], 43)  # 'z'

    def test_en_us_includes_digits(self):
        km = build_keymap("en_US")
        self.assertEqual(km[0x31], 1)   # '1'
        self.assertEqual(km[0x30], 10)  # '0'

    def test_en_us_includes_space(self):
        km = build_keymap("en_US")
        self.assertEqual(km[0x20], 56)

    def test_de_de_yz_swap(self):
        km = build_keymap("de_DE")
        # German QWERTZ: y -> scan 43 (Z position), z -> scan 20 (Y position)
        self.assertEqual(km[0x79], 43)  # y
        self.assertEqual(km[0x7A], 20)  # z

    def test_de_de_umlauts(self):
        km = build_keymap("de_DE")
        self.assertEqual(km[0xF6], 38)  # ö
        self.assertEqual(km[0xE4], 39)  # ä
        self.assertEqual(km[0xFC], 25)  # ü

    def test_fr_fr_azerty_swaps(self):
        km = build_keymap("fr_FR")
        self.assertEqual(km[0x61], 15)  # a -> Q position
        self.assertEqual(km[0x71], 29)  # q -> A position
        self.assertEqual(km[0x77], 43)  # w -> Z position
        self.assertEqual(km[0x7A], 16)  # z -> W position

    def test_unknown_layout_falls_back(self):
        km = build_keymap("xx_XX")
        # Should fall back to en_US
        self.assertEqual(km[0x61], 29)  # 'a' same as en_US

    def test_all_layouts_build(self):
        for layout in AVAILABLE_LAYOUTS:
            km = build_keymap(layout)
            self.assertIsInstance(km, dict)
            self.assertGreater(len(km), 50)

    def test_all_layouts_include_special_keys(self):
        for layout in AVAILABLE_LAYOUTS:
            km = build_keymap(layout)
            for keysym, scancode in _SPECIAL_KEYS.items():
                self.assertIn(keysym, km,
                              f"Layout {layout} missing special key 0x{keysym:04X}")
                self.assertEqual(km[keysym], scancode,
                                 f"Layout {layout}: 0x{keysym:04X} expected {scancode} got {km[keysym]}")


class TestKeyboardTranslator(unittest.TestCase):

    def test_basic_translation(self):
        t = KeyboardTranslator("en_US")
        self.assertEqual(t.keysym_to_scancode(0x61), 29)  # 'a'
        self.assertEqual(t.keysym_to_scancode(0xFFBE), 60)  # F1

    def test_unmapped_returns_none(self):
        t = KeyboardTranslator("en_US")
        self.assertIsNone(t.keysym_to_scancode(0x12345))

    def test_layout_stored(self):
        t = KeyboardTranslator("de_DE")
        self.assertEqual(t.layout, "de_DE")


class TestGetTranslator(unittest.TestCase):

    def test_returns_translator(self):
        t = get_translator("en_US")
        self.assertIsInstance(t, KeyboardTranslator)
        self.assertEqual(t.layout, "en_US")

    def test_caches_same_layout(self):
        t1 = get_translator("en_GB")
        t2 = get_translator("en_GB")
        self.assertIs(t1, t2)

    def test_different_layout_creates_new(self):
        t1 = get_translator("en_US")
        t2 = get_translator("de_DE")
        self.assertIsNot(t1, t2)


class TestModuleLevelFunction(unittest.TestCase):

    def test_keysym_to_scancode(self):
        # Reset module state
        get_translator("en_US")
        self.assertEqual(keysym_to_scancode(0x61), 29)  # 'a'

    def test_keysym_to_scancode_none(self):
        get_translator("en_US")
        self.assertIsNone(keysym_to_scancode(0xDEAD))


class TestMakeKeyEvent(unittest.TestCase):

    def test_press(self):
        result = make_key_event(29, True)
        self.assertEqual(result, bytes([0x04, 29]))

    def test_release(self):
        result = make_key_event(29, False)
        self.assertEqual(result, bytes([0x04, 29 | 0x80]))

    def test_release_flag_value(self):
        self.assertEqual(RELEASE_FLAG, 0x80)

    def test_key_event_masks_byte(self):
        result = make_key_event(0x1FF, True)
        self.assertEqual(result[1], 0xFF)


class TestModifierTracker(unittest.TestCase):

    def test_initial_empty(self):
        mt = ModifierTracker()
        self.assertEqual(mt.get_held_keys(), set())

    def test_key_press(self):
        mt = ModifierTracker()
        mt.key_pressed(41)  # Shift
        self.assertIn(41, mt.get_held_keys())

    def test_key_release(self):
        mt = ModifierTracker()
        mt.key_pressed(41)
        mt.key_released(41)
        self.assertNotIn(41, mt.get_held_keys())

    def test_release_nonexistent_no_error(self):
        mt = ModifierTracker()
        mt.key_released(99)  # should not raise

    def test_release_all(self):
        mt = ModifierTracker()
        mt.key_pressed(41)
        mt.key_pressed(54)
        mt.key_pressed(29)
        released = mt.release_all()
        self.assertEqual(set(released), {41, 54, 29})
        self.assertEqual(mt.get_held_keys(), set())

    def test_release_all_empty(self):
        mt = ModifierTracker()
        released = mt.release_all()
        self.assertEqual(released, [])

    def test_is_modifier_held(self):
        mt = ModifierTracker()
        self.assertFalse(mt.is_modifier_held())
        mt.key_pressed(41)  # Shift_L is a modifier
        self.assertTrue(mt.is_modifier_held())

    def test_non_modifier_not_flagged(self):
        mt = ModifierTracker()
        mt.key_pressed(29)  # 'a' is not a modifier
        self.assertFalse(mt.is_modifier_held())

    def test_get_held_keys_returns_copy(self):
        mt = ModifierTracker()
        mt.key_pressed(41)
        held = mt.get_held_keys()
        held.add(999)
        self.assertNotIn(999, mt.get_held_keys())


class TestModifierScancodes(unittest.TestCase):

    def test_expected_modifiers(self):
        # L-Shift=41, R-Shift=53, L-Ctrl=54, R-Ctrl=58, L-Alt=55, AltGr=57
        self.assertEqual(MODIFIER_SCANCODES, {41, 53, 54, 58, 55, 57})


class TestAvailableLayouts(unittest.TestCase):

    def test_expected_layouts(self):
        expected = ["en_US", "en_GB", "de_DE", "de_CH", "fr_FR", "fr_CH",
                    "sv_SE", "no_NO", "ja_JP"]
        self.assertEqual(AVAILABLE_LAYOUTS, expected)


if __name__ == "__main__":
    unittest.main()
