# VNC-to-IPKVM Protocol Translator: Implementation Plan

## Goal

Build a standalone proxy server that speaks VNC (RFB 3.x) on the client side and the proprietary e-RIC RFB protocol on the KVM side. This lets any standard VNC viewer (TightVNC, RealVNC, TigerVNC, etc.) control the Belkin IP-KVM without Java.

```
[VNC Client] <--standard RFB--> [Translator Proxy] <--e-RIC RFB--> [Belkin IP-KVM]
```

## Architecture

### Language Choice: Python 3

- Rapid prototyping for protocol work
- Good socket/async support
- Easy to read and modify as protocol understanding evolves
- Libraries: `asyncio` for networking, `struct` for binary parsing, `zlib` for decompression

### Component Breakdown

```
vnc2ipkvm/
  __init__.py
  main.py              # CLI entry point, argument parsing
  config.py            # Configuration (KVM host, port, auth ID, etc.)
  eric_protocol.py     # e-RIC RFB client (connects to KVM)
  eric_encodings.py    # Framebuffer decoding (raw, copyrect, hextile, tight)
  eric_auth.py         # Authentication handshake
  vnc_server.py        # VNC/RFB server (accepts VNC client connections)
  vnc_protocol.py      # Standard RFB protocol messages
  framebuffer.py       # Shared framebuffer (8-bit -> 32-bit color conversion)
  keyboard.py          # VNC keysym -> e-RIC scan code translation
  mouse.py             # VNC pointer events -> e-RIC pointer events
  color.py             # RGB332 <-> RGB888 conversion
```

## Implementation Phases

### Phase 1: e-RIC Protocol Client (talk to the KVM)

**Goal:** Connect to the KVM, authenticate, and receive framebuffer data.

1. **Connection & auth** (`eric_protocol.py`, `eric_auth.py`)
   - TCP/SSL socket connection
   - Send `e-RIC AUTH=<applet_id>` (75 bytes, null-padded)
   - Parse auth response byte
   - Read server version string `e-RIC RFB XX.YY\n`
   - Read server name
   - Read server info block
   - Send client version `e-RIC RFB 01.00\n`
   - Send share/port bytes
   - Read 20-byte framebuffer init

2. **Framebuffer updates** (`eric_encodings.py`)
   - Send SetEncodings (type 0x02)
   - Send FramebufferUpdateRequest (type 0x03)
   - Parse FramebufferUpdate (type 0x00) rectangles
   - Implement decoders: Raw (type 0), CopyRect (type 1), Hextile (type 5)
   - Tight encoding (types 7, 9, 10) can be deferred if the KVM works with simpler encodings

3. **Input sending**
   - Key events (type 0x04): scan code byte, release = code | 0x80
   - Pointer events (type 0x05/0x93): button mask + x,y + wheel

4. **Session maintenance**
   - Respond to pings (type 0x94 -> reply 0x95)
   - Handle bandwidth tests (type 0x96 -> reply 0x97)
   - Handle desktop resize (type 0x80)

### Phase 2: VNC Server (accept VNC clients)

**Goal:** Present a standard VNC/RFB 3.3 or 3.8 server interface.

1. **RFB handshake** (`vnc_server.py`, `vnc_protocol.py`)
   - Send `RFB 003.008\n`
   - Security handshake (None auth or VNC password auth)
   - Send ServerInit with framebuffer dimensions and 32-bit pixel format

2. **Framebuffer serving**
   - Maintain a 32-bit RGBA framebuffer converted from the KVM's 8-bit RGB332
   - On VNC FramebufferUpdateRequest, send updates using Raw encoding initially
   - Later: implement Hextile or ZRLE for better performance

3. **Input forwarding**
   - Receive VNC KeyEvent (keysym + down/up flag)
   - Translate X11 keysym -> e-RIC scan code using keyboard layout tables
   - Receive VNC PointerEvent (buttons + x,y)
   - Forward as e-RIC pointer event

### Phase 3: Bridge / Proxy Logic

**Goal:** Wire the two halves together.

1. **Async event loop** (`main.py`)
   - Connect to KVM on startup
   - Listen for VNC clients on a local port (default 5900)
   - Relay framebuffer updates KVM -> VNC client
   - Relay input events VNC client -> KVM

2. **Color conversion** (`color.py`, `framebuffer.py`)
   - KVM sends 8-bit RGB332 pixels
   - VNC clients expect 32-bit (or 16-bit) pixels
   - Convert on the fly: `r = (pixel & 0x07) * 255 / 7`, etc.

3. **Change tracking**
   - Track dirty rectangles from KVM updates
   - Send only changed regions to VNC client

### Phase 4: Polish & Robustness

1. **SSL support** - connect to KVM over SSL/TLS
2. **Reconnection** - auto-reconnect to KVM on disconnect
3. **Multiple VNC clients** - support multiple viewers (read-only for extras)
4. **Clipboard** - bridge ServerCutText between VNC and e-RIC
5. **Keyboard layouts** - port the layout tables from the Java code
6. **Cursor handling** - forward cursor shape if supported

## Key Technical Challenges

### 1. Keyboard Translation
VNC uses X11 keysyms (e.g., `XK_a = 0x61`). The KVM expects device-specific scan codes. The Java client has ~16 locale-specific mapping tables. We need to port at least the English (US) layout initially.

**Approach:** Extract the key mappings from `KeyTranslator_en.java` and build a keysym-to-scancode lookup table.

### 2. Color Depth Conversion
The KVM uses 8-bit RGB332 (256 colors). VNC clients typically want 24/32-bit color. Every pixel must be upscaled.

**Approach:** Pre-compute a 256-entry lookup table mapping each RGB332 byte to an RGB888 triplet.

### 3. Encoding Translation
The KVM's Hextile/Tight encodings produce 8-bit pixel data. We need to either:
- Decode to a local framebuffer and re-encode for VNC (simplest)
- Pass through compatible encodings with pixel format conversion

**Approach:** Decode everything into a local 32-bit framebuffer, then serve VNC clients using Raw or simple encodings.

### 4. Authentication
The KVM requires an `APPLET_ID` which is typically provided by the KVM's web interface. We'll need to either:
- Accept the APPLET_ID as a CLI parameter
- Scrape it from the KVM's web interface
- Implement the web login flow

**Approach:** Start with CLI parameter, add web scraping later.

## Minimum Viable Product

The MVP needs:
- [x] Protocol documentation (this is done)
- [ ] e-RIC client: connect, auth, receive raw framebuffer data
- [ ] VNC server: accept connection, serve raw framebuffer
- [ ] Keyboard: at minimum US English layout
- [ ] Mouse: absolute positioning with button support
- [ ] Ping/keepalive handling

This should be achievable relatively quickly since the protocol is straightforward (no encryption beyond SSL, simple binary messages, 8-bit color).
