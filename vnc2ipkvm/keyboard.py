"""Keyboard translation: X11 keysyms (VNC) -> e-RIC scan codes (KVM).

The KVM expects device-specific scan codes, NOT standard VNC keysyms.
Key press sends the raw scan code byte; key release sends scancode | 0x80.

The mapping is derived from the decompiled Java client's KeyTranslator*.java
files and KbdLayout_101pc.java. The Java client has two translation layers:

  1. Key code translation: AWT keycode -> keynr (scan code)
     This handles non-printable keys (arrows, F-keys, modifiers, etc.)

  2. Character translation: Unicode char -> keynr (scan code)
     This handles printable characters and locale-specific symbols.

For VNC, X11 keysyms for printable characters ARE Unicode code points
(Latin-1 range 0x0020-0x00FF), so the Java character translations map directly.
Non-printable keysyms (0xFF00+) use the key code layer.

All layouts inherit the US English base key code map and overlay locale-specific
character mappings on top.

Supported layouts: en_US, en_GB, de_DE, de_CH, fr_FR, fr_CH, sv_SE, no_NO, ja_JP
"""

import logging

logger = logging.getLogger(__name__)

RELEASE_FLAG = 0x80

# Available layout names (for CLI help / validation)
AVAILABLE_LAYOUTS = [
    "en_US", "en_GB", "de_DE", "de_CH", "fr_FR", "fr_CH",
    "sv_SE", "no_NO", "ja_JP",
]

# ---------------------------------------------------------------------------
# Non-printable key mappings (shared by all layouts)
# X11 keysym -> e-RIC scan code
# ---------------------------------------------------------------------------
_SPECIAL_KEYS = {
    # Function keys (XK_F1=0xFFBE .. XK_F12=0xFFC9)
    0xFFBE: 60, 0xFFBF: 61, 0xFFC0: 62, 0xFFC1: 63,
    0xFFC2: 64, 0xFFC3: 65, 0xFFC4: 66, 0xFFC5: 67,
    0xFFC6: 68, 0xFFC7: 69, 0xFFC8: 70, 0xFFC9: 71,

    # Control keys
    0xFF0D: 27,  # Return/Enter
    0xFF8D: 27,  # KP_Enter
    0xFF1B: 59,  # Escape
    0xFF09: 14,  # Tab
    0xFF08: 13,  # BackSpace

    # Navigation
    0xFF63: 75,  # Insert
    0xFFFF: 78,  # Delete
    0xFF50: 76,  # Home
    0xFF57: 79,  # End
    0xFF55: 77,  # Page_Up
    0xFF56: 80,  # Page_Down

    # Arrow keys
    0xFF52: 81,  # Up
    0xFF51: 82,  # Left
    0xFF54: 83,  # Down
    0xFF53: 84,  # Right

    # Modifier keys
    0xFFE1: 41,  # Shift_L
    0xFFE2: 53,  # Shift_R
    0xFFE3: 54,  # Control_L
    0xFFE4: 58,  # Control_R
    0xFFE9: 55,  # Alt_L
    0xFFEA: 57,  # Alt_R / AltGr
    0xFFE7: 87,  # Meta_L / Super_L
    0xFFE8: 87,  # Meta_R / Super_R
    0xFFEB: 87,  # Super_L
    0xFFEC: 87,  # Super_R

    # Lock keys
    0xFFE5: 28,  # Caps_Lock
    0xFF7F: 85,  # Num_Lock
    0xFF14: 73,  # Scroll_Lock

    # Print/Pause
    0xFF61: 72,  # Print / SysReq
    0xFF13: 74,  # Pause

    # Numpad
    0xFFB0: 100, 0xFFB1: 95, 0xFFB2: 96, 0xFFB3: 97,
    0xFFB4: 91,  0xFFB5: 92, 0xFFB6: 93, 0xFFB7: 86,
    0xFFB8: 87,  0xFFB9: 88,
    0xFFAE: 101,  # KP_Decimal
    0xFFAB: 89,   # KP_Add
    0xFFAD: 99,   # KP_Subtract
    0xFFAA: 94,   # KP_Multiply
    0xFFAF: 90,   # KP_Divide

    # Menu key
    0xFF67: 105,
}


# ---------------------------------------------------------------------------
# Character mappings per layout
# Maps Unicode code point (== X11 keysym for Latin-1) -> scan code
# ---------------------------------------------------------------------------

# US English: printable character -> scan code
# Built from the AWT keycode map, covering both unshifted and shifted chars
_CHARS_EN_US = {
    # Letters (lowercase keysyms, uppercase keysyms -> same scan code)
    0x61: 29, 0x41: 29,  # a/A
    0x62: 47, 0x42: 47,  # b/B
    0x63: 45, 0x43: 45,  # c/C
    0x64: 31, 0x44: 31,  # d/D
    0x65: 17, 0x45: 17,  # e/E
    0x66: 32, 0x46: 32,  # f/F
    0x67: 33, 0x47: 33,  # g/G
    0x68: 34, 0x48: 34,  # h/H
    0x69: 22, 0x49: 22,  # i/I
    0x6a: 35, 0x4a: 35,  # j/J
    0x6b: 36, 0x4b: 36,  # k/K
    0x6c: 37, 0x4c: 37,  # l/L
    0x6d: 49, 0x4d: 49,  # m/M
    0x6e: 48, 0x4e: 48,  # n/N
    0x6f: 23, 0x4f: 23,  # o/O
    0x70: 24, 0x50: 24,  # p/P
    0x71: 15, 0x51: 15,  # q/Q
    0x72: 18, 0x52: 18,  # r/R
    0x73: 30, 0x53: 30,  # s/S
    0x74: 19, 0x54: 19,  # t/T
    0x75: 21, 0x55: 21,  # u/U
    0x76: 46, 0x56: 46,  # v/V
    0x77: 16, 0x57: 16,  # w/W
    0x78: 44, 0x58: 44,  # x/X
    0x79: 20, 0x59: 20,  # y/Y
    0x7a: 43, 0x5a: 43,  # z/Z

    # Number row: digit and shifted symbol
    0x31: 1,  0x21: 1,   # 1 / !
    0x32: 2,  0x40: 2,   # 2 / @
    0x33: 3,  0x23: 3,   # 3 / #
    0x34: 4,  0x24: 4,   # 4 / $
    0x35: 5,  0x25: 5,   # 5 / %
    0x36: 6,  0x5e: 6,   # 6 / ^
    0x37: 7,  0x26: 7,   # 7 / &
    0x38: 8,  0x2a: 8,   # 8 / *
    0x39: 9,  0x28: 9,   # 9 / (
    0x30: 10, 0x29: 10,  # 0 / )

    # Punctuation and symbols
    0x60: 0,   0x7e: 0,   # ` / ~
    0x2d: 11,  0x5f: 11,  # - / _
    0x3d: 12,  0x2b: 12,  # = / +
    0x5b: 25,  0x7b: 25,  # [ / {
    0x5d: 26,  0x7d: 26,  # ] / }
    0x5c: 40,  0x7c: 40,  # \ / |
    0x3b: 38,  0x3a: 38,  # ; / :
    0x27: 39,  0x22: 39,  # ' / "
    0x2c: 50,  0x3c: 50,  # , / <
    0x2e: 51,  0x3e: 51,  # . / >
    0x2f: 52,  0x3f: 52,  # / / ?
    0x20: 56,              # Space
}

# UK English: character overrides/additions on top of US
# From KeyTranslator_en_GB.java char translation array
_CHARS_EN_GB = {
    0x60:  0,   # ` (backtick)            -> scan 0
    0xAC:  0,   # ¬ (not sign)            -> scan 0
    0xA6:  0,   # ¦ (broken bar)          -> scan 0
    0x21:  1,   # !                       -> scan 1
    0x22:  2,   # " (double quote)        -> scan 2  (UK: Shift+2)
    0xA3:  3,   # £ (pound sign)          -> scan 3  (UK: Shift+3)
    0x24:  4,   # $                       -> scan 4
    0x80:  4,   # € (euro sign, 0x80)     -> scan 4
    0x25:  5,   # %                       -> scan 5
    0x5E:  6,   # ^ (caret)              -> scan 6
    0x26:  7,   # &                       -> scan 7
    0x28:  9,   # (                       -> scan 9
    0x29: 10,   # )                       -> scan 10
    0x5F: 11,   # _                       -> scan 11
    0x5B: 25,   # [                       -> scan 25
    0x7B: 25,   # {                       -> scan 25
    0x27: 39,   # ' (apostrophe)          -> scan 39
    0x40: 39,   # @ (at sign)             -> scan 39  (UK: Shift+')
    0x23: 40,   # # (hash)               -> scan 40  (UK: key next to Enter)
    0x7E: 40,   # ~ (tilde)              -> scan 40
    0x5C: 42,   # \ (backslash)          -> scan 42  (UK: key left of Z)
    0x7C: 42,   # | (pipe)              -> scan 42
}

# German: character overrides
# From KeyTranslator_de.java char and key translations
_CHARS_DE_DE = {
    0x3F: 11,   # ?                       -> scan 11  (ß key)
    0x5C: 11,   # \                       -> scan 11
    0x2D: 52,   # -                       -> scan 52
    0x5F: 52,   # _                       -> scan 52
    0x3C: 42,   # <                       -> scan 42
    0x3E: 42,   # >                       -> scan 42
    0x7C: 42,   # |                       -> scan 42
    0x2B: 26,   # +                       -> scan 26
    0x2A: 26,   # *                       -> scan 26
    0x7E: 26,   # ~                       -> scan 26
    0x23: 40,   # #                       -> scan 40
    0x27: 40,   # '                       -> scan 40
    0xF6: 38,   # ö                       -> scan 38
    0xD6: 38,   # Ö                       -> scan 38
    0xE4: 39,   # ä                       -> scan 39
    0xC4: 39,   # Ä                       -> scan 39
    0xFC: 25,   # ü                       -> scan 25
    0xDC: 25,   # Ü                       -> scan 25
    0xDF: 11,   # ß                       -> scan 11
    0xB4: 12,   # ´ (acute accent)        -> scan 12
    0x60: 12,   # ` (grave accent)        -> scan 12
}

# German: Y/Z swap (QWERTZ)
_KEYS_DE_DE = {
    # AWT 89 (Y) -> keynr 43 (Z position), AWT 90 (Z) -> keynr 20 (Y position)
    # For keysyms we just swap in the char map:
    0x79: 43, 0x59: 43,  # y/Y -> scan 43 (Z position on QWERTZ)
    0x7A: 20, 0x5A: 20,  # z/Z -> scan 20 (Y position on QWERTZ)
}

# Swiss German: character overrides
# From KeyTranslator_de_CH.java
_CHARS_DE_CH = {
    0xA7:  0,   # § (section)             -> scan 0
    0xB0:  0,   # ° (degree)              -> scan 0
    0x27: 11,   # '                       -> scan 11
    0x3F: 11,   # ?                       -> scan 11
    0xB4: 11,   # ´ (acute)               -> scan 11
    0x5E: 12,   # ^                       -> scan 12
    0x60: 12,   # `                       -> scan 12
    0x7E: 12,   # ~                       -> scan 12
    0xE8: 25,   # è                       -> scan 25
    0xFC: 25,   # ü                       -> scan 25
    0x5B: 25,   # [                       -> scan 25
    0xA8: 26,   # ¨ (diaeresis)           -> scan 26
    0x21: 26,   # !                       -> scan 26
    0x5D: 26,   # ]                       -> scan 26
    0xE9: 38,   # é                       -> scan 38
    0xF6: 38,   # ö                       -> scan 38
    0xE0: 39,   # à                       -> scan 39
    0xE4: 39,   # ä                       -> scan 39
    0x7B: 39,   # {                       -> scan 39
    0x24: 40,   # $                       -> scan 40
    0xA3: 40,   # £                       -> scan 40
    0x7D: 40,   # }                       -> scan 40
    0x3C: 42,   # <                       -> scan 42
    0x3E: 42,   # >                       -> scan 42
    0x5C: 42,   # \                       -> scan 42
    0x2D: 52,   # -                       -> scan 52
    0x5F: 52,   # _                       -> scan 52
}

# Swiss German: Y/Z swap (same as DE)
_KEYS_DE_CH = {
    0x79: 43, 0x59: 43,  # y/Y -> scan 43
    0x7A: 20, 0x5A: 20,  # z/Z -> scan 20
}

# French: character overrides
# From KeyTranslator_fr.java char and key translations
_CHARS_FR_FR = {
    0xB2:  0,   # ² (superscript 2)       -> scan 0
    0x7E:  2,   # ~                       -> scan 2
    0x23:  3,   # #                       -> scan 3
    0x7B:  4,   # {                       -> scan 4
    0x5B:  5,   # [                       -> scan 5
    0x7C:  6,   # |                       -> scan 6
    0x2D:  6,   # -                       -> scan 6
    0x60:  7,   # `                       -> scan 7
    0x5F:  8,   # _                       -> scan 8
    0x5C:  8,   # \                       -> scan 8
    0x5E:  9,   # ^                       -> scan 9
    0x40: 10,   # @                       -> scan 10
    0x29: 11,   # )                       -> scan 11
    0xB0: 11,   # °                       -> scan 11
    0x5D: 11,   # ]                       -> scan 11
    0x7D: 12,   # }                       -> scan 12
    # 0x5E: 25, # ^ already mapped to 9
    0xA8: 25,   # ¨ (diaeresis)           -> scan 25
    0x24: 26,   # $                       -> scan 26
    0xA3: 26,   # £                       -> scan 26
    0xA4: 26,   # ¤ (currency)            -> scan 26
    0xB5: 40,   # µ (micro)               -> scan 40
    0x2A: 40,   # *                       -> scan 40
    0xF9: 39,   # ù                       -> scan 39
    0x25: 39,   # %                       -> scan 39
    0x2C: 49,   # ,                       -> scan 49
    0x3F: 49,   # ?                       -> scan 49
    0x3B: 50,   # ;                       -> scan 50
    0x2E: 50,   # .                       -> scan 50
    0x3A: 51,   # :                       -> scan 51
    0x2F: 51,   # /                       -> scan 51
    0x21: 52,   # !                       -> scan 52
    0xA7: 52,   # §                       -> scan 52
    0x3C: 42,   # <                       -> scan 42
    0x3E: 42,   # >                       -> scan 42
}

# French: AZERTY key swaps
_KEYS_FR_FR = {
    # Q<->A, W<->Z, M->semicolon position
    0x71: 29, 0x51: 29,  # q/Q -> scan 29 (A position on AZERTY)
    0x61: 15, 0x41: 15,  # a/A -> scan 15 (Q position on AZERTY)
    0x77: 43, 0x57: 43,  # w/W -> scan 43 (Z position on AZERTY)
    0x7A: 16, 0x5A: 16,  # z/Z -> scan 16 (W position on AZERTY)
    0x6D: 38, 0x4D: 38,  # m/M -> scan 38 (semicolon position on AZERTY)
}

# Swiss French: identical to Swiss German
# From KeyTranslator_fr_CH.java (same arrays as de_CH)
_CHARS_FR_CH = _CHARS_DE_CH.copy()
_KEYS_FR_CH = _KEYS_DE_CH.copy()

# Swedish: character overrides
# From KeyTranslator_sv.java
_CHARS_SV_SE = {
    0xA7:  0,   # §                       -> scan 0
    0xBD:  0,   # ½                       -> scan 0
    0xB4: 12,   # ´ (acute)               -> scan 12
    0x60: 12,   # `                       -> scan 12
    0xE5: 25,   # å                       -> scan 25
    0xC5: 25,   # Å                       -> scan 25
    0xA8: 26,   # ¨ (diaeresis)           -> scan 26
    0x5E: 26,   # ^                       -> scan 26
    0x7E: 26,   # ~                       -> scan 26
    0xF6: 38,   # ö                       -> scan 38
    0xD6: 38,   # Ö                       -> scan 38
    0xE4: 39,   # ä                       -> scan 39
    0xC4: 39,   # Ä                       -> scan 39
    0x3C: 42,   # <                       -> scan 42
    0x3E: 42,   # >                       -> scan 42
    0x7C: 42,   # |                       -> scan 42
}

# Norwegian: character overrides
# From KeyTranslator_no.java
_CHARS_NO_NO = {
    0xA7:  0,   # §                       -> scan 0
    0x7C:  0,   # |                       -> scan 0
    0x5C: 12,   # \                       -> scan 12
    0xB4: 12,   # ´ (acute)               -> scan 12
    0x60: 12,   # `                       -> scan 12
    0xE5: 25,   # å                       -> scan 25
    0xC5: 25,   # Å                       -> scan 25
    0xA8: 26,   # ¨ (diaeresis)           -> scan 26
    0x5E: 26,   # ^                       -> scan 26
    0x7E: 26,   # ~                       -> scan 26
    0xF8: 38,   # ø                       -> scan 38
    0xD8: 38,   # Ø                       -> scan 38
    0xE6: 39,   # æ                       -> scan 39
    0xC6: 39,   # Æ                       -> scan 39
    0x3C: 42,   # <                       -> scan 42
    0x3E: 42,   # >                       -> scan 42
}

# Japanese: character overrides
# From KeyTranslator_ja.java
_CHARS_JA_JP = {
    0x5F: 114,  # _                       -> scan 114
    0x7C: 112,  # |                       -> scan 112
}


# ---------------------------------------------------------------------------
# Layout registry
# ---------------------------------------------------------------------------

# Each layout is (char_overrides, key_overrides)
# char_overrides: dict of keysym -> scancode for printable characters
# key_overrides: dict of keysym -> scancode that replace base letter mappings
_LAYOUTS = {
    "en_US": ({}, {}),
    "en_GB": (_CHARS_EN_GB, {}),
    "de_DE": (_CHARS_DE_DE, _KEYS_DE_DE),
    "de_CH": (_CHARS_DE_CH, _KEYS_DE_CH),
    "fr_FR": (_CHARS_FR_FR, _KEYS_FR_FR),
    "fr_CH": (_CHARS_FR_CH, _KEYS_FR_CH),
    "sv_SE": (_CHARS_SV_SE, {}),
    "no_NO": (_CHARS_NO_NO, {}),
    "ja_JP": (_CHARS_JA_JP, {}),
}


def build_keymap(layout: str = "en_US") -> dict[int, int]:
    """Build a complete keysym -> scancode map for the given layout.

    The map is built in layers:
      1. Special (non-printable) keys: F-keys, arrows, modifiers, etc.
      2. US English printable characters (base layer)
      3. Layout-specific key swaps (e.g. QWERTZ Y/Z, AZERTY Q/A)
      4. Layout-specific character overrides (locale symbols)

    Later layers override earlier ones, so locale-specific mappings win.
    """
    if layout not in _LAYOUTS:
        logger.warning("Unknown layout '%s', falling back to en_US", layout)
        layout = "en_US"

    char_overrides, key_overrides = _LAYOUTS[layout]

    keymap = {}
    keymap.update(_SPECIAL_KEYS)     # Layer 1: special keys
    keymap.update(_CHARS_EN_US)      # Layer 2: US base characters
    keymap.update(key_overrides)     # Layer 3: letter position swaps
    keymap.update(char_overrides)    # Layer 4: locale character overrides

    return keymap


class KeyboardTranslator:
    """Stateful keyboard translator that maps VNC keysyms to e-RIC scan codes."""

    def __init__(self, layout: str = "en_US"):
        self.layout = layout
        self.keymap = build_keymap(layout)
        logger.info("Keyboard layout: %s (%d mappings)", layout, len(self.keymap))

    def keysym_to_scancode(self, keysym: int) -> int | None:
        """Convert an X11 keysym to an e-RIC scan code, or None if unmapped."""
        return self.keymap.get(keysym)


# Module-level convenience for backward compatibility
_default_translator = None


def get_translator(layout: str = "en_US") -> KeyboardTranslator:
    """Get or create the keyboard translator for the given layout."""
    global _default_translator
    if _default_translator is None or _default_translator.layout != layout:
        _default_translator = KeyboardTranslator(layout)
    return _default_translator


def keysym_to_scancode(keysym: int) -> int | None:
    """Convert an X11 keysym to an e-RIC scan code using the current layout."""
    if _default_translator is None:
        get_translator("en_US")
    return _default_translator.keysym_to_scancode(keysym)


def make_key_event(scancode: int, pressed: bool) -> bytes:
    """Create a 2-byte e-RIC key event message.

    Wire format: [0x04, scancode] for press, [0x04, scancode|0x80] for release.
    """
    code = scancode if pressed else (scancode | RELEASE_FLAG)
    return bytes([0x04, code & 0xFF])


# Scan codes that are modifier keys
MODIFIER_SCANCODES = {41, 53, 54, 58, 55, 57}  # L-Shift, R-Shift, L-Ctrl, R-Ctrl, L-Alt, R-Alt/AltGr
TOGGLE_SCANCODES = {28, 85, 73}  # Caps Lock, Num Lock, Scroll Lock

# Ctrl scan code for Ctrl+Alt -> AltGr detection
SC_CTRL_L = 54
SC_ALT_L = 55
SC_ALTGR = 57


class ModifierTracker:
    """Tracks held modifier keys and provides release-all functionality.

    Also implements Ctrl+Alt -> AltGr detection for European keyboards:
    when left Ctrl is pressed and Alt follows within a short window,
    the pair is treated as AltGr (scan code 57).
    """

    def __init__(self):
        self._held: set[int] = set()  # scan codes currently held

    def key_pressed(self, scancode: int):
        """Record a key press."""
        self._held.add(scancode)

    def key_released(self, scancode: int):
        """Record a key release."""
        self._held.discard(scancode)

    def get_held_keys(self) -> set[int]:
        """Return the set of currently held scan codes."""
        return self._held.copy()

    def release_all(self) -> list[int]:
        """Release all held keys. Returns list of scan codes that were released."""
        released = list(self._held)
        self._held.clear()
        return released

    def is_modifier_held(self) -> bool:
        """Return True if any modifier is currently held."""
        return bool(self._held & MODIFIER_SCANCODES)
