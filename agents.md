# agents.md — V-MUX Replacement Controller Project

## Project identity

**Project:** Braun ambulance V-MUX controller replacement  
**Vehicle:** 2013 Braun ambulance with Weldon V-MUX RS-485 multiplexed electrical system  
**Goal:** Replace failing factory V-MUX controller (MasterTech IV / Vista IV) with a custom embedded system providing reconfigurable HMI, multi-message macro automation, and full V-MUX bus control  
**Current phase:** Phase 1 — passive bus capture and protocol reverse engineering  

---

## Agent instructions

You are assisting an experienced professional embedded systems engineer on this project. The user has deep electronics and firmware expertise. Do not over-explain fundamentals. Be direct, technically precise, and engineering-focused.

**Communication style:**
- Technical prose, minimal hedging
- Use code, schematics, and tables where they add clarity
- Raise design tradeoffs explicitly — don't silently pick the conservative option
- Flag risks and failure modes proactively
- Sentence case for all labels and headings

---

## System under reverse engineering

### V-MUX bus (physical layer)
- **Protocol:** Weldon V-MUX, proprietary message-based network
- **Physical layer:** RS-485, 2-wire half-duplex twisted pair
- **Cable:** Weldon specifies 0L20-1600-xx as the only acceptable cable (V-MUX Essentials p.5), but Braun dwg 49384 calls out **Belden 8760** shielded twisted pair (clear = A, black = B, bare = shield) for this vehicle
- **Topology:** Bus (daisy-chain), up to 32 nodes (Connector Spec p.7). This vehicle per dwg 49384: nodes 1 & 2 in PDQ, node 3 above OSS #2, display nodes 4 (MCC, cab) and 5 (ACP, streetside), joined by DT04-3P-P007 Y adapters
- **Shield:** single-point grounded (Weldon: one drain for the whole network, away from node grounds). Braun 49384: bare wire pinned at node 1 only, cut off elsewhere. **The tap must never connect the shield.**
- **Node loading:** unpowered A–B ≈ 180 kΩ (3rd-gen nodes), ≈ 315–430 kΩ (4th-gen); network ≈ 180 kΩ / n (Essentials p.7). No termination or low-impedance bias network exists
- **Local echo:** nodes hear their own transmissions (Essentials p.8)
- **Termination:** NONE — Weldon explicitly forbids termination resistors on this network
- **Network access port (J1):** Deutsch DT04-4P receptacle (Weldon 0K15-1436-00). Weldon kit 0K90-3111-00 = "V-MUX network forward tap" (Connector Spec p.6–7). On this vehicle: dwg 49384 Detail C, DT04-4P "DL", Braun connector 19126, located **behind ACP or in PDQ**.
  - **Pin 1: comms A** (Weldon: white; Braun Belden 8760: clear) — VMUX_A
  - **Pin 2: comms B** (black) — VMUX_B
  - **Pin 3: ground** — Weldon "optional ground" / "not used"; Braun: wire 18-BK-GND
  - **Pin 4: power** — Weldon "optional +V Batt" / "not used"; Braun: wire 18-312-OG (circuit 312; E6207-1 shows 312 on the fused Vista/MCC power feed — battery-hot vs ignition-switched not confirmed)
  - Shield: not on J1 (bare wire cut off at the DT04-4P per Detail C)
  - Sources: Weldon V-MUX Connector Spec 2024-05 p.8; V-MUX Essentials p.5; Braun 6207 prints, dwg 49384 Detail C (PDF p.34). **Earlier pinout (pin 1 GND, 2 A, 3 B) was wrong.**
  - Still to confirm on the vehicle: that the DL connector is populated as drawn (meter pin 3 to battery negative, pin 4 voltage key-off/key-on, pins 1/2 ≈ 180 kΩ/n to each other unpowered).

### V-MUX protocol (documented behaviour)
- **SYNC message:** Node 1 broadcasts every ~4 seconds; coordinates flash patterns and confirms network health
- **`VM_OUT_OF_NETWORK`:** Any node missing SYNC transmits this distress message
- **Message structure (hypothesis):** `[msg_code: 1 byte][state: 1 byte (0x00=OFF / 0x01=ON)][node: 1 byte][...][checksum?]`
- **Packet framing:** Idle-gap based — no length prefix or explicit delimiter
- **Packet boundary threshold:** ~10 ms idle gap (tunable)
- **Direction control (SH-U11F):** UNVERIFIED. FT232 CBUS TXDEN is a plausible implementation, but the SH-U11F user guide does not document direction control. Passive capture does not depend on it. Confirm empirically (scope on A/B while transmitting from generator) before relying on it.
- **Bus collisions (BC counter):** Any incrementing BC count = serious fault; vehicle must not return to service
- **Diagnostics tool:** Weldon V-MUX Diagnostics v1.4.2 (Windows only); uses `.dav` database files mapping numeric codes to human labels

### Message codes — PLACEHOLDERS, NOT from Weldon documentation
**These codes are working assumptions used by the capture tool and generator for display/testing. None are confirmed against the vehicle. Replace with captured data in Phase 1.**
| Code | Label |
|------|-------|
| 0x04 | Reverse |
| 0x01 | Forward |
| 0x02 | Park |
| 0x10 | Emergency Master |
| 0x11 | Front Light Bar |
| 0x12 | Grill Lights |
| 0x13 | Warning Lights Front |
| 0x14 | Warning Lights Rear |
| 0x20 | Scene Lights |
| 0x40 | Door Cab Left |
| 0x50 | Sync (Node 1) |
| 0xFF | Ping / Reply |

*Full message map to be built during Phase 1 capture sessions.*

---

## Hardware

### Development board
- **Board:** WeAct Studio STM32F4 64-pin Core Board V1.0
- **MCU:** STM32F405RGT6 (Cortex-M4, 168 MHz, 1 MB Flash, 192 KB SRAM + 64 KB CCM)
- **Schematic:** https://github.com/WeActStudio/WeActStudio.STM32F4_64Pin_CoreBoard
- **Key onboard features:** USB-C, 8 MHz HSE crystal, 32.768 kHz LSE, MicroSD slot (SDIO), SWD header, BOOT0 + NRST buttons, user LED PB2, user button PC13

### Bus tap and isolation circuit (designed, not yet built)
- **File:** `vmux_bus_tap_schematic.html`
- **Signal chain:** J1 tap → L1 CMC (Würth 744235601) → D1 TVS (SM712) → R1/R2 (100Ω series) → U2 RS-485 transceiver (MAX3485/SP3485) → U1 digital isolator (ISO7241/ADuM1201) → MCU
- **Isolation:** 2500Vrms galvanic isolation between bus GND and MCU GND
- **Power:** Isolated DC/DC (Murata MEE1S0512SC) + bus-side LDO (AMS1117-3.3)
- **Critical rules:**
  - No termination resistors anywhere on bus-side
  - Two separate ground planes (GND and GND_ISO) — never connect
  - DIR pin default Low = receive-only at power-on
  - **DE strategy (recommended, pending decision):** STM32F405 USART has no hardware DE (no DEM/DEAT/DEDT; high confidence, RM0090 not in repo). Recommended: GPIO DE (PA1) asserted before TX, released in the USART **TC** interrupt (not DMA-complete); DE pulldown so reset = receive; IWDG; firmware cap on DE-high time; `/RE` tied low so every TX byte is read back for collision detection (V-MUX nodes already rely on local echo, Essentials p.8); transmit only after bus idle >= gap threshold. Rejected: auto-direction transceivers (MAX13487-style) — they drive only the dominant state and rely on bus bias/termination for recessive; V-MUX has neither (no termination, nodes 180–430 kΩ A–B). Alternatives: TIM one-pulse DE (hardware-timed release, more coupling to TX start); MCU with hardware DE (G4/L4/H7) — decide at Phase 2 PCB.
  - **Bus-side transceiver:** with no termination/bias, the idle bus differential is set only by node leakage. Prefer a **true fail-safe** receiver (guaranteed defined output at 0 V differential) and ≤ 1/8 unit load; MAX3485 choice to be re-checked against this.

### USB-RS485 adapter (Phase 1 capture)
- **Device:** DSD TECH SH-U11F isolated USB to RS485/RS422 converter
- **Chip:** FTDI-based (exact part not confirmed)
- **Isolation:** advertised as isolated; isolation voltage and isolator part not confirmed from the user guide
- **Termination:** 120Ω is a jumper, default disabled (per user guide); shell is detachable. Jumper location not documented — **confirm disabled before connecting to V-MUX bus**
- **FTDI latency timer:** driver default is 16 ms, which batches reads and defeats ~10 ms gap detection. **Set to 1 ms** (Windows: Device Manager → port Advanced; Linux: `/sys/bus/usb-serial/devices/ttyUSBx/latency_timer`). Per-byte timestamps in the capture tool are interpolated, so inter-packet gaps inside one batched chunk are lost.
- **Baud range:** 300 bps to 3 Mbps
- **OS:** Windows / Linux / Mac (FTDI VCP driver)
- **Connection:** RTS=Low, DTR=Low (never drive the bus); A+ → J1 pin 1, B− → J1 pin 2, GND → J1 pin 3 (18-BK-GND, after meter check). Adapter is isolated, so GND here only sets the receiver common mode

### Oscilloscope connection
- **CH1:** Probe tip → J1 pin 1 (BUS A), clip → J1 pin 3 (GND)
- **CH2:** Probe tip → J1 pin 2 (BUS B), clip → J1 pin 3 (GND) — same point as CH1 clip
- **Math:** CH1 − CH2 = true differential RS-485 signal (use this for triggering and decoding)
- **Settings:** DC coupling, 1V/div, 1ms/div start, trigger CH1 rising ~1V, BW limit 20 MHz on
- **Safety:** ground clips to J1 pin 3 only, **never to pin 1 or 2** (bus A/B). Pin 3 is vehicle ground, so a mains-earthed scope ties vehicle ground to earth; with shoreline power connected the vehicle may already be earthed. Prefer a differential probe or a battery-powered/isolated scope

---

## Software

### Capture tool
- **File:** `vmux_capture.py` — current version: **v3**
- **Dependency:** `pyserial` (`pip install -r requirements.txt`)
- **Key classes:**
  - `PacketAssembler` — idle-gap framing, byte-by-byte feed with interpolated timestamps, emits `VmuxPacket`
  - `SyncDetector` — detects SYNC period to confirm baud rate; intervals propagated to `CaptureStats`
  - `CaptureLogger` — writes CSV + binary log (64-bit epoch_ms) per session
  - `Display` — colour-coded terminal output (magenta=SYNC, green=known, yellow=unknown); ANSI cursor save/restore on status bar
  - `_process_packet()` — helper: stats update, SYNC detection, display, log for a completed packet
- **Output:** `vmux_capture_YYYYMMDD_HHMMSS.csv` + `.bin` per session
- **Baud detection:** `--detect` flag tries 9600 / 19200 / 38400 / 57600 / 115200

### Capture tool — v3 key implementation decisions
| Issue | Solution |
|-------|----------|
| Timestamp collapse on OS-buffered chunks | Per-byte interpolated timestamps: `t_last - (n-1-i) * byte_time_s` |
| SYNC packet displayed 4s late | Idle-gap flush fires in `else` branch of blocking read loop |
| Windows 15.6ms timer floor with `sleep(0.001)` | `ser.timeout = gap_ms/2000.0`; main loop uses blocking `ser.read(1)` |
| `detect_baud` read inefficiency | `ser.read(ser.in_waiting or 1)` drains buffer instantly |
| `global VMUX_IDLE_GAP_MS` anti-pattern | `gap_ms` passed as explicit param: `main()→capture()→PacketAssembler` |
| FTDI RTS/DTR transient on port open | 100ms settle delay after `rts=False/dtr=False` before `reset_input_buffer()` |
| 8N1 assumption undocumented | Explicit comment in `Serial()` constructor; flag for 9-bit review if needed |

### Test message generator
- **File:** `vmux_generator.py` — current version: **v1**
- **Dependency:** `pyserial` (same `requirements.txt`)
- **Purpose:** Simulate realistic V-MUX bus traffic on a bench RS-485 link to validate `vmux_capture.py` without connecting to the vehicle
- **Runs on:** Separate laptop with its own SH-U11F (or any USB-RS485 adapter)

**Key classes:**

| Class | Role |
|-------|------|
| `BusTx` | Serial port wrapper with RS-485 direction control, TX shift-register wait, per-packet logging |
| `SyncScheduler` | Fires SYNC from Node 1 every ~4s with ±50ms jitter; call `tick()` frequently in scenario loops |

**Packet format (hypothesis, matches capture tool):**
```
[msg_code: 1B][state: 1B (0x00=OFF, 0x01=ON)][node: 1B][xor_checksum: 1B]
```
Checksum = `msg_code XOR state XOR node`

**Scenarios:**

| Scenario | Duration | What it tests |
|----------|----------|---------------|
| `idle` | 30s | SYNC only — baud detection, SyncDetector interval |
| `basic` | 90s | All known commands ON/OFF — packet framing, decode |
| `burst` | 45s | 4 packets at 5 ms intra-burst gap (< 10 ms threshold). **Expected: each burst captured as ONE 16-byte packet** (merge is correct for idle-gap framing). Fail = burst split, or SYNC/bursts merged. No SYNC is sent inside a burst |
| `threshold` | 45s | Packet pairs at threshold+margin (state 01, must split 2×4 B) and threshold−margin (state 00, must merge 1×8 B). `--threshold` (=capture `--gap`), `--margin` (default 3 ms). Statistical on hardware: host/USB jitter ~0–2 ms; capture measures arrival-to-arrival (idle + 1 byte time) |
| `multinode` | 60s | Nodes 1/2/3 interleaved — node byte parsing |
| `unknown` | 45s | Mixed known/unknown codes — yellow-highlight path in Display |
| `full` | ~3.5 min | All scenarios in sequence (incl. threshold) — complete validation run |
| `interactive` | ∞ | Manual packet injection with background SYNC |

**Direction control:**
- SH-U11F direction control is not documented (see above). Default assumption: automatic; if the generator's packets do not appear on the bus, try `--manual-de` (RTS=High before write, Low after shift-register wait) and check on a scope.

**Common commands:**
```bash
python vmux_generator.py --scan
python vmux_generator.py --port COM4 --baud 19200 --scenario idle
python vmux_generator.py --port COM4 --baud 19200 --scenario full
python vmux_generator.py --port COM4 --baud 19200 --scenario interactive
python vmux_generator.py --port COM4 --baud 19200 --scenario burst --gap 5
```

### Physical bench connection (generator ↔ capture)
```
GENERATOR LAPTOP                        CAPTURE LAPTOP
vmux_generator.py                       vmux_capture.py
     │                                       │
SH-U11F (USB-RS485)                    SH-U11F (USB-RS485)
R120 jumper: ABSENT                    R120 jumper: ABSENT
     │                                       │
┌────┴─────────────────────────────────────┴────┐
│  A+  ─────────────────────────────────── A+   │
│  B-  ─────────────────────────────────── B-   │  RS-485 twisted pair
│ GND  ──────────────────────────────────  GND  │  <1m bench cable
└───────────────────────────────────────────────┘
```

**Runtime configuration checklist:**
1. Both R120 jumpers absent on both adapters
2. Baud rate identical on both tools (`--baud 19200`)
3. Start capture tool first, then generator
4. Each tool uses a different COM port (run `--scan` on each laptop)
5. If capture shows only garbage: swap A+/B− on one adapter (polarity inversion)

**Expected capture tool output during `full` scenario:**
- Within 5s: first SYNC packet (magenta)
- Within 14s: `SYNC CONFIRMED OK (avg=4.xx s)` — baud rate verified
- All known codes appear green; unknown codes appear yellow
- `--map` output shows all transmitted codes with correct frequencies

### Common commands — capture tool
```bash
python vmux_capture.py --scan                          # find port
python vmux_capture.py --port COM3 --detect            # auto baud
python vmux_capture.py --port COM3 --baud 19200        # capture
python vmux_capture.py --map vmux_capture_*.csv        # build message map
```

### Phase 1 vehicle capture procedure
1. Confirm SH-U11F 120Ω termination is off (jumper location undocumented; meter A–B on the unpowered adapter)
2. Meter J1 (DL connector behind ACP / in PDQ): pin 3 to battery negative ≈ 0 Ω; pin 4 voltage; pins 1–2 high resistance
3. Connect A+ → J1 pin 1, B− → J1 pin 2, GND → J1 pin 3
4. Oscilloscope CH1 on pin 1, CH2 on pin 2, both clips to pin 3; Math=CH1−CH2
5. Power vehicle, verify SYNC burst visible on scope every ~4s
6. Run `--scan` to identify adapter port
7. Run `--detect` to confirm baud rate (SYNC interval should be 3.5–4.5s)
8. Run capture; operate each vehicle function one at a time; log ~60s per function
9. Run `--map` against captured CSV to build annotated message table
10. Update `VMUX_KNOWN_COMMANDS` dict with confirmed mappings

---

## Firmware architecture (planned)

### RTOS: FreeRTOS on STM32F405RGT6

| Layer | Task | Priority | Description |
|-------|------|----------|-------------|
| 4 | HMI renderer | 1 | LVGL on SPI display, config loaded from SD `layout.json` |
| 3 | Macro engine | 3 | Sequenced multi-message macros triggered by HMI or GPIO |
| 2 | Bus driver | 5 | USART2 DMA RX/TX, DE control (strategy open — see DE section), SYNC watchdog |
| 1 | HAL/peripherals | — | USART3, SPI1, I2C1, SDMMC, GPIO, TIM, IWDG |
| 0 | V-MUX bus | — | Physical RS-485 network |

### Peripheral allocation (STM32F405RGT6, LQFP64)
| Function | Peripheral | Pins |
|----------|-----------|------|
| V-MUX RS-485 | USART2 | PA2 TX, PA3 RX, PA1 DE (GPIO on F405 — no hardware DE; PA1 is USART2_RTS on F405, check AF table if porting to G4) |
| SD card config | SDIO | PC8–PC11 D0–D3, PC12 CLK, PD2 CMD |
| SPI display | SPI1 | PA5 SCK, PA6 MISO, PA7 MOSI |
| Touch controller | I2C1 | PB6 SCL, PB7 SDA |
| CAN1 (future chassis) | CAN1 | PD0 RX, PD1 TX |
| SWD debug | SWD | PA13 SWDIO, PA14 SWDCLK |
| Status LED | GPIO | PB2 |
| Switch inputs / LEDs | GPIO + MCP23017 | remaining GPIO + I2C2 |

### Macro format
```c
typedef struct {
    uint16_t msg_code;
    uint8_t  state;      // 0x00=OFF, 0x01=ON
    uint16_t delay_ms;   // post-send delay before next step
} macro_step_t;

typedef struct {
    char         name[32];
    macro_step_t steps[16];
    uint8_t      step_count;
} macro_t;
```

### HMI config format (`layout.json` on SD card)
```jsonc
{ "buttons": [
  { "label": "Scene lights",  "x": 0, "y": 0, "msg": "SCENE_LIGHTS" },
  { "label": "Load patient",  "x": 1, "y": 0, "macro": "load_patient_sequence" },
  { "label": "En route",      "x": 2, "y": 0, "macro": "en_route_lights" }
]}
```

---

## Project requirements (factory controller deficiencies)

| Requirement | Detail |
|-------------|--------|
| Display replacement | Factory display is failing and replacement is cost-prohibitive; need affordable SPI display alternative |
| Macro automation | Factory controller has no macro/sequence capability; need multi-message sequenced automation |
| Reconfigurable UX | Factory layout is hardcoded; need runtime-reconfigurable button layout via config file |

---

## Project phases

| Phase | Status | Description |
|-------|--------|-------------|
| 1 — Bus analysis | In progress | Passive capture with SH-U11F + oscilloscope; build message map |
| 2 — Hardware design | Planned | PCB design for bus tap + isolation + MCU carrier board |
| 3 — Firmware | Planned | FreeRTOS firmware: bus driver, macro engine, HMI renderer, config store |
| 4 — Integration | Planned | Vehicle installation, field testing, SYNC watchdog validation |

---

## Key design constraints

- **No termination resistors** on the V-MUX bus under any circumstances
- **Galvanic isolation** required between vehicle RS-485 bus GND and MCU/laptop GND
- **RS-485 direction control** must be deterministic with default receive; strategy on F4 is an open decision (see bus tap section)
- **DIR default Low** (receive-only) at all power-on and reset states
- **Passive tap only** during Phase 1 — the adapter must never drive the bus
- **SYNC monitoring** required in firmware — `VM_OUT_OF_NETWORK` from any node = fault
- **No termination** — confirmed twice because it is the most common mistake on this bus

---

## Files in this project

**Repository:** https://github.com/bhunting/weldon_vmux_sniffer

| File | Description |
|------|-------------|
| `vmux_capture.py` | Python RS-485 bus capture and protocol analysis tool (**v3**) |
| `vmux_generator.py` | Python RS-485 test message generator for bench validation (**v1**) |
| `requirements.txt` | Python dependencies (pyserial) — shared by both tools |
| `README.md` | Usage guide for the capture tool |
| `agents.md` | This file — agent context and project state |
| `tests/` | pytest suite (no hardware): packet/checksum, PacketAssembler, SyncDetector, BusTx via `loop://`, burst/threshold scenarios on a fake clock |
| `.githooks/pre-commit` | `py_compile` + pytest; enable with `git config core.hooksPath .githooks` |

---

## Phase 1 open items — next actions

When the SH-U11F adapter arrives and the first vehicle capture session runs, the following are the immediate outstanding unknowns that must be resolved before Phase 2 hardware design can begin:

| Item | Unknown | How to resolve |
|------|---------|----------------|
| Baud rate | 9600 / 19200 / 38400 / other | `--detect` + SYNC interval confirmation on scope |
| Packet framing | Exact byte count per message; whether checksum byte exists | Capture SYNC (known periodic packet) and count bytes |
| Message code space | How many codes are in use on this specific vehicle | Full function-by-function capture session |
| Node count | How many nodes respond on the bus | Observe Ping/Reply traffic in capture output |
| SYNC code byte | Confirm 0x50 or discover actual value | First capture; SYNC is the most frequent packet |
| Bus baud — 9-bit? | Standard 8N1 assumed; some V-MUX versions use 9-bit addressing | If framing errors occur at all baud rates, try 8N2/8E1 |
| Checksum algorithm | Unknown — last byte of packet may be XOR, CRC8, or sum | Capture same command multiple times; compare trailing byte |

---

## Verification status (2026-11-14)

Sources in `docs/reference/` (PDFs are git-ignored; they carry Braun/Weldon
proprietary notices): `V-MUXConnectorSpec202405.pdf` (Weldon, May 2024),
`V-MUX_Essentials.pdf` (Weldon), `6207 Electrical Prints.pdf` (Braun; vector
drawings with no text layer, read visually). Not present: SH-U11F manual,
Weldon Diagnostics manual, RM0090, component datasheets.

| Item | Status | Source |
|------|--------|--------|
| J1 = DT04-4P: pin 1 A, pin 2 B | **Confirmed (docs)** | Connector Spec p.8; Essentials p.5; Braun 49384 Detail C |
| J1 pin 3 ground, pin 4 power | **Confirmed in Braun print** (18-BK-GND; 18-312-OG). Weldon generic: optional / not used | Braun 49384 Detail C. Meter on vehicle before use |
| Pin 4 circuit 312 is battery-hot or ignition-switched | **Unconfirmed** | Meter key-off/key-on |
| J1 location on this vehicle | Per print: behind ACP or in PDQ ("DL") | Braun 49384 |
| Kit 0K90-3111-00 = network forward tap | **Confirmed (part exists, description)**; not confirmed that Braun used this kit | Connector Spec p.6–7 |
| Cable on this vehicle | **Belden 8760**, not Weldon 0L20-1600 | Braun 49384 note 1 |
| No termination; ≤ 32 nodes | **Confirmed** | Connector Spec p.7; Essentials p.5–7 |
| SYNC from node 1, others expect it ≥ every 4 s, else OUT OF NETWORK | **Confirmed** | Essentials p.8 |
| Local echo (node hears own TX) | **Confirmed** (Hercules nodes) | Essentials p.8 |
| Node A–B loading ≈ 180–430 kΩ unpowered | **Confirmed** | Essentials p.7 |
| Shield single-point ground; Braun pins it at node 1 only | **Confirmed** | Essentials p.5; Braun 49384 Detail A |
| Baud, framing, checksum, message codes | **Not in any supplied doc** | Phase 1 capture |
| BC counter, Diagnostics v1.4.2, `.dav` files | **Not in supplied docs** (from chat) | Weldon Diagnostics manual |
| SH-U11F 120 Ω = jumper, default off | Per user guide (as reported in chat; guide not in repo) | Place guide in `docs/reference/` |
| SH-U11F jumper location / label | **Undocumented** | Open the shell |
| SH-U11F FTDI part, TXDEN auto-direction | **Unverified** | Scope A/B while transmitting; read FTDI EEPROM CBUS config (FT_PROG) |
| SH-U11F isolation voltage / isolator part | **Unverified** | Vendor datasheet |
| STM32F405 USART has no DEM/DEAT/DEDT | High confidence (F4 USART_CR3 has no DEM bit) but not checked against a local RM0090 | RM0090 USART_CR3 |
| Schematic D1 "SM712, 5 V bidirectional, ±6 V" | **Likely wrong.** SM712 is an asymmetric RS-485 TVS (≈ −7 V / +12 V working). Fix wording when datasheet is in hand | SM712 datasheet |
| Schematic U1 "ISO7241 or ADuM1201" | **Inconsistent.** ADuM1201 is 2-channel; TX + RX + DE needs ≥ 3 | Datasheets |
| Isolation rating 2500 Vrms | **Unverified.** System rating is the weakest of isolator and DC/DC (MEE1 series rating unconfirmed) | Datasheets |

## Known software issues (open)

- `SyncDetector` hypothesis 2 ignores timing: any 2–4 byte packet seen 3× in the window is called SYNC (strict xfail test documents it).
- `_process_packet` prints the "Baud rate OK" banner only when `pkt_num == 2`, which is almost never true.
- `VMUX_MAX_PACKET_BYTES` is declared but not enforced by `PacketAssembler`.
- `PacketAssembler` measures arrival-to-arrival, so the effective idle threshold is `gap − 1 byte time` (≈1 ms at 9600).

## Reference documents

- Weldon V-MUX Diagnostics User Manual (September 2012) — bus protocol, SYNC behaviour, BC counter, load management
- Weldon V-MUX Parts, Connectors, and Accessories Specification (March 2020) — physical layer, cable part numbers, connector kits, Port F pinout
- STM32F405 Reference Manual RM0090 — peripheral configuration
- WeAct STM32F4 64-pin Core Board schematic — board-level pin assignments
- FTDI FT232RN datasheet — USB-RS485 reference circuit (Fig 7.2, TXDEN CBUS option)
- DSD TECH SH-U11F user guide — jumper configuration, termination disable
