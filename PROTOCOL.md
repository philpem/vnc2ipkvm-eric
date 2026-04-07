# Belkin IP-KVM Protocol Specification (e-RIC RFB)

This document describes the proprietary "e-RIC RFB" protocol used by Belkin IP-KVM devices, reverse-engineered from the decompiled Java client applet (`rc.jar`).

The protocol is a heavily modified variant of VNC/RFB, with a custom authentication scheme, different message types, and 8-bit color.

---

## 1. Connection Establishment

The client connects via TCP (plain or SSL) to the KVM device. Multiple connection methods are tried in order:

1. SSL to `HOST:SSLPORT` (if SSL not disabled)
2. Plain TCP to `HOST:PORT`
3. HTTP CONNECT tunnel via `PROXY_HOST:PROXY_PORT` to `HOST:PORT`
4. SSL without certificate validation to `HOST:SSLPORT`

### HTTP Proxy Tunneling

When using a proxy, the client sends:
```
CONNECT <target_host>:<target_port> HTTP/1.0\r\n
User-Agent: e-RIC Remote Console Applet\r\n
\r\n
```
Expected response: `HTTP/1.0 200 ...`

---

## 2. Authentication Handshake

### 2.1 NORBOX Target (optional)

If NORBOX mode is enabled, the client first sends a target string:
```
IPV4TARGET=<ip_address>,\n    (for IPv4)
IPV6TARGET=<ip_address>,\n    (for IPv6)
```

### 2.2 Authentication Request

Client sends a 75-byte authentication message:
```
Offset  Length  Description
0       11     ASCII "e-RIC AUTH="
11      64     APPLET_ID string, null-padded to fill 75 bytes total
```

### 2.3 Authentication Response

Server responds with a single byte. The first byte is read; if it equals `0x03` (manually rejected / disconnect), the client reads a disconnect reason.

Otherwise, the byte is treated as the first byte of the server version string (it should be `0x65` = ASCII `'e'`).

Auth error codes (when disconnect byte `0x03` is received, the subsequent int indicates):
| Code | Meaning |
|------|---------|
| 1 | No permission |
| 2 | Exclusive access active |
| 3 | Manually rejected |
| 4 | Server password disabled |
| 5 | Loopback connection senseless |
| 6 | Authentication failed |
| 7 | Access to this KVM port denied |

### 2.4 Server Version String

The first byte of the auth response (if not `0x03`) is the start of a 16-byte version greeting. The full format is:

```
Byte 0:     'e' (0x65) - already read during auth check
Bytes 1-15: "-RIC RFB XX.YY\n"
```

Combined: `"e-RIC RFB XX.YY\n"` (16 bytes total)

Where XX and YY are two-digit decimal version numbers (e.g., `03.08`).

Validation: bytes must match the pattern exactly, with digits 0-9 in version positions and `\n` terminator.

### 2.5 Server Name

After the version string, the server sends:
1. One padding byte (read and discarded)
2. A length-prefixed string: 2-byte big-endian length, then that many bytes of server name text

### 2.6 Server Info Block

Next, the server sends an info block:
1. One byte: `hasPassword` (1 = yes, 0 = no)
2. Two bytes: `sessionId` (big-endian uint16)
3. Two bytes: `infoLength` (big-endian uint16)
4. `infoLength` bytes: info string

### 2.7 Client Protocol Version

Client sends its protocol version string:
```
"e-RIC RFB XX.YY\n"
```
Padded/truncated to fit in a byte array derived from the string. The version comes from the `PROTOCOL_VERSION` applet parameter (default `"01.00"`).

### 2.8 Share Desktop / Port ID

Client sends 2 bytes:
```
Byte 0: shareDesktop (1 = share, 0 = exclusive)
Byte 1: PORT_ID (KVM port number)
```

### 2.9 Framebuffer Parameters (Server Init)

Server sends 20 bytes:
```
Offset  Length  Type     Description
0       1       uint8    framebufferUpdateRequired (0 or 1)
1       2       uint16   framebufferWidth
3       2       uint16   framebufferHeight
5       1       uint8    bitsPerPixel (typically 8)
6       1       uint8    depth
7       1       uint8    bigEndian (0 or 1)
8       1       uint8    trueColor (0 or 1)
9       2       uint16   redMax
11      2       uint16   greenMax
13      2       uint16   blueMax
15      1       uint8    redShift
16      1       uint8    greenShift
17      1       uint8    blueShift
18      3       bytes    padding
```

All multi-byte values are big-endian.

The client uses a fixed **8-bit color model**: `DirectColorModel(8, 7, 56, 192)` = RGB 3-3-2 (3 bits red, 3 bits green, 2 bits blue), giving 256 colors.

---

## 3. Post-Handshake Initialization

### 3.1 Set Encodings (Client -> Server)

Message type `0x02`:
```
Offset  Length  Type     Description
0       1       uint8    messageType = 0x02
1       1       uint8    padding
2       2       uint16   numberOfEncodings
4       4*N     int32[]  encoding types (big-endian, signed)
```

Typical encoding list: `[255, 7, 6]` or configured via applet params.

### 3.2 Framebuffer Update Request (Client -> Server)

Message type `0x03`:
```
Offset  Length  Type     Description
0       1       uint8    messageType = 0x03
1       1       uint8    incremental (0 = full, 1 = incremental)
2       2       uint16   x
4       2       uint16   y
6       2       uint16   width
8       2       uint16   height
```

The client immediately requests a full update after init, then sends incremental requests in the main loop.

---

## 4. Server -> Client Messages

### 4.1 Framebuffer Update (type 0x00)

```
Offset  Length  Description
0       1       messageType = 0x00
1       1       padding
2       2       numberOfRectangles (uint16)
```

Followed by N rectangles, each:
```
Offset  Length  Type     Description
0       2       uint16   x
2       2       uint16   y
4       2       uint16   width
6       2       uint16   height
8       4       int32    encodingType
```

#### Encoding Type 0: Raw
Raw pixel data, `width * height` bytes. Each byte is an 8-bit RGB332 pixel.

#### Encoding Type 1: CopyRect
```
Offset  Length  Type     Description
0       2       uint16   srcX
2       2       uint16   srcY
```
Copy a rectangle from (srcX, srcY) to (x, y) within the framebuffer.

#### Encoding Type 5: Hextile
Processes the rectangle in 16x16 tiles (last tile may be smaller).

Per tile:
```
Byte 0: tileFlags
  bit 0 (0x01): raw - tile is raw pixels (16*16 bytes max)
  bit 1 (0x02): backgroundSpecified - 1 byte follows
  bit 2 (0x04): foregroundSpecified - 1 byte follows
  bit 3 (0x08): anySubrects - subrectangle data follows
  bit 4 (0x10): subrectColored - each subrect has its own color
```

If raw: read `tileWidth * tileHeight` raw bytes.

Otherwise:
- If backgroundSpecified: read 1 byte (background color)
- If foregroundSpecified: read 1 byte (foreground color)
- If anySubrects: read 1 byte (count), then per subrect:
  - If subrectColored: 1 byte color
  - 1 byte: packed x/y position (high nibble = x, low nibble = y)
  - 1 byte: packed width/height (high nibble = width-1, low nibble = height-1)

#### Encoding Type 7: Tight (8-bit)
Uses zlib compression streams. Details in the renderer classes.

#### Encoding Type 9: Extended
Extended encoding supporting JPEG and other compressed formats.

#### Encoding Type 10: Tight Packed
Variant of Tight encoding with packed pixel data.

### 4.2 SetColourMapEntries (type 0x01)
Not supported - throws an exception if received.

### 4.3 Bell (type 0x02)
No payload. Client beeps.

### 4.4 Server Cut Text (type 0x07)
Clipboard text from server. Client reads a length-prefixed UTF string.

### 4.5 Extended Message Info (type 0x08)
Server sends extended capabilities:
```
1 byte:  padding
Then 12 uint16 fields (24 bytes) describing device capabilities
```

### 4.6 Device Info (type 0x09)
```
1 byte:  padding
2 bytes: stringLength (uint16)
N bytes: info string
```

### 4.7 Update Palette (type 0x10)
Updates the color palette:
```
1 byte:  hasCustomPalette (1 = yes)
2 bytes: sessionId (uint16)
2 bytes: paletteDataLength (uint16)
N bytes: palette data string
```

### 4.8 Sync (type 0x11)
```
2 bytes: read and discarded (padding/sync)
```

### 4.9 Desktop Size Change (type 0x80)
Server re-sends the 20-byte framebuffer parameters (same format as section 2.9). Client resizes its display.

### 4.10 Server Status (type 0x83)
```
3 bytes: padding
4 bytes: textLength (int32, compact encoding)
N bytes: status message text
```

### 4.11 Server Command (type 0x84)
Key-value command:
```
1 byte:  padding
2 bytes: keyLength (uint16)
2 bytes: valueLength (uint16)
N bytes: key string
M bytes: value string
```

Known commands:
| Key | Values | Description |
|-----|--------|-------------|
| `exclusive_mode` | `"active"` / `"inactive"` | Exclusive access state |
| `rc_users` | integer string | Number of connected users |
| `wlan_quality` | 0-100 | WiFi signal quality |
| `rdp_exit` | - | Terminate RDP session |
| `rdp_enabled` | `"yes"` / `"no"` | RDP availability |
| `rdp_username` | string | RDP username |
| `rdp_password` | string | RDP password |

### 4.12 Ping (type 0x94 / 148)
```
3 bytes: padding (read via readByte x3)
4 bytes: ping data (int32, compact)
```
Client must respond with a ping response (see 5.7).

### 4.13 Bandwidth Test (type 0x96 / 150)
```
2 bytes: data length (int16)
N bytes: test data (discarded)
```
Client sends bandwidth response messages (type 0x97) with phases 1 and 2.

### 4.14 Mode Switch (type 0xA1 / 161)
```
1 byte: mode status
```
| Status | Meaning |
|--------|---------|
| 0 | Entered RDP Mode |
| 1 | Left RDP Mode |
| 2 | RDP Mode not available |
| 3 | Entered Host Direct Mode |
| 4 | Left Host Direct Mode |
| 5 | Host Direct Mode not available |

---

## 5. Client -> Server Messages

### 5.1 Set Pixel Format (type 0x00)
```
20 bytes total (same format as standard RFB SetPixelFormat)
```
Sent during init but the KVM uses fixed 8-bit color.

### 5.2 Set Encodings (type 0x02)
See section 3.1.

### 5.3 Framebuffer Update Request (type 0x03)
See section 3.2.

### 5.4 Key Event (type 0x04)
```
Offset  Length  Description
0       1       messageType = 0x04
1       1       keyCode (device scan code)
```

Key codes are NOT standard VNC keysyms. They are device-specific scan codes mapped from Java AWT key codes via locale-specific keyboard layout tables. The key release is signaled by adding 128 (0x80) to the keycode.

### 5.5 Pointer Event - Standard (type 0x05)
```
Offset  Length  Description
0       1       messageType = 0x05
1       1       buttonMask
2       2       x position (uint16)
4       2       y position (uint16)
6       2       wheel delta (int16)
```

Button mask bits:
| Bit | Button |
|-----|--------|
| 0 | Left |
| 1 | Middle |
| 2 | Right |

### 5.6 Pointer Event - Extended (type 0x93 / 147)
Same format as 0x05 but with extended wheel support. The client uses 0x93 when `wheelEnabled` is true, 0x05 otherwise.

### 5.7 Ping Response (type 0x95 / 149 → actually -107 signed = 0x95)
```
Offset  Length  Description
0       1       messageType = 0x95
1       3       padding (zeros)
4       4       response data (int32, typically 0)
```

### 5.8 Bandwidth Response (type 0x97 / 151 → -105 signed)
```
Offset  Length  Description
0       1       messageType = 0x97
1       1       phase (1 = start, 2 = done)
```

### 5.9 Server Cut Text ACK (type 0x07)
```
Offset  Length  Description
0       1       messageType = 0x07
1       1       padding (zero)
Then:   UTF     clipboard text string
```

### 5.10 LED/Command Control (type 0x87 / 135 → -121 signed)
```
Offset  Length  Description
0       1       messageType = 0x87
1       1       keyLength
2       1       valueLength
3       N       key bytes
3+N     M       value bytes
```

### 5.11 Power Control (type 0xA0 / 160 → -96 signed)
```
Offset  Length  Description
0       1       messageType = 0xA0
1       1       command byte
```

---

## 6. Compact Length Encoding

Several messages use a compact variable-length integer encoding for lengths:

```
Read byte 0:
  value = byte0 & 0x7F
  if (byte0 & 0x80):
    Read byte 1:
      value |= (byte1 & 0x7F) << 7
      if (byte1 & 0x80):
        Read byte 2:
          value |= (byte2 & 0xFF) << 14
```

This encodes values up to 4,194,303 (22 bits) in 1-3 bytes.

---

## 7. Color Model

The KVM uses an **8-bit RGB332 color model**:
- Bits 7-5: Red (3 bits, 0-7)
- Bits 4-2: Green (3 bits, 0-7)
- Bits 1-0: Blue (2 bits, 0-3)

Java `DirectColorModel(8, 0x07, 0x38, 0xC0)` - note the masks are `redMask=7, greenMask=56, blueShift=192`.

Actually from the code: `DirectColorModel(8, 7, 56, 192)` where:
- Red mask = 7 (0x07) = bits 0-2
- Green mask = 56 (0x38) = bits 3-5
- Blue mask = 192 (0xC0) = bits 6-7

To convert to 24-bit RGB:
```
r8 = ((pixel & 0x07) * 255) / 7
g8 = (((pixel >> 3) & 0x07) * 255) / 7
b8 = (((pixel >> 6) & 0x03) * 255) / 3
```

---

## 8. Keyboard Scan Code Mapping

The client translates Java AWT key codes to device-specific scan codes. Key press sends the raw scan code; key release sends `scancode | 0x80` (adds 128).

Supported keyboard layouts: en, en_GB, de, de_CH, fr, fr_CH, it, es, pt, no, sv, da, fi, ru, iw, ja, Mac_de.

---

## 9. Applet Parameters Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HOST` | - | KVM hostname/IP |
| `PORT` | 80 | TCP port |
| `SSLPORT` | 443 | SSL port |
| `APPLET_ID` / `SRV_ID` | - | Session/authentication ID |
| `PROTOCOL_VERSION` | `"01.00"` | Protocol version string |
| `PORT_ID` | 0 | KVM port number |
| `SelEnc` | 0 | Encoding selection |
| `AdvEncIndCR` | - | CopyRect encoding index |
| `AdvEncIndCD` | - | CD encoding index |
| `SSL` | - | `"force"`, `"try"`, or unset |
| `NORBOX` | `"no"` | `"ipv4"`, `"ipv6"`, or `"no"` |
| `PROXY_HOST` | - | HTTP proxy host |
| `PROXY_PORT` | - | HTTP proxy port |
| `REAL_HOST` | - | Fallback host for NORBOX |
| `EXCLUSIVE_PERM` | - | Exclusive access mode |
| `LOCAL_CURSOR` | - | Local cursor rendering |
| `MONITOR_MODE` | - | Monitor-only mode |
| `RDP_ENABLED` | - | Enable RDP support |
