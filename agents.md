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
- **Cable:** Weldon part 0L20-1600-xx series RS-485 twisted pair
- **Topology:** Bus (daisy-chain), up to 32 nodes
- **Termination:** NONE — Weldon explicitly forbids termination resistors on this network
- **Network access port:** 4-pin connector (Weldon 0K90-3111-00)
  - Pin 1: GND (bus ground — use for all probe references)
  - Pin 2: BUS A (+) — VMUX_A
  - Pin 3: BUS B (−) — VMUX_B
  - Pin 4: +12V vehicle supply

### V-MUX protocol (documented behaviour)
- **SYNC message:** Node 1 broadcasts every ~4 seconds; coordinates flash patterns and confirms network health
- **`VM_OUT_OF_NETWORK`:** Any node missing SYNC transmits this distress message
- **Message structure (hypothesis):** `[msg_code: 1 byte][state: 1 byte (0x00=OFF / 0x01=ON)][node: 1 byte][...][checksum?]`
- **Packet framing:** Idle-gap based — no length prefix or explicit delimiter
- **Packet boundary threshold:** ~10 ms idle gap (tunable)
- **Direction control:** Hardware auto (CBUS TXDEN on FT232 → DE pin on RS-485 transceiver)
- **Bus collisions (BC counter):** Any incrementing BC count = serious fault; vehicle must not return to service
- **Diagnostics tool:** Weldon V-MUX Diagnostics v1.4.2 (Windows only); uses `.dav` database files mapping numeric codes to human labels

### Known message codes (partial — from Weldon documentation)
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
  - TX turnaround <1 µs; use STM32F405 hardware DE pin (USART `DEM` bit)

### USB-RS485 adapter (Phase 1 capture)
- **Device:** DSD TECH SH-U11F isolated USB to RS485/RS422 converter
- **Chip:** FTDI FT232RL (genuine)
- **Isolation:** Yes — ADI galvanic isolator between USB and RS-485 sides
- **Termination:** 120Ω jumper-selectable — **must be removed/disabled before connecting to V-MUX bus**
- **Baud range:** 300 bps to 3 Mbps
- **OS:** Windows / Linux / Mac (FTDI VCP driver)
- **Connection:** RTS=Low, DTR=Low (never drive the bus); A+ to J1 pin 2, B− to J1 pin 3, GND to J1 pin 1

### Oscilloscope connection
- **CH1:** Probe tip → J1 pin 2 (BUS A), clip → J1 pin 1 (GND)
- **CH2:** Probe tip → J1 pin 3 (BUS B), clip → J1 pin 1 (GND) — same point as CH1 clip
- **Math:** CH1 − CH2 = true differential RS-485 signal (use this for triggering and decoding)
- **Settings:** DC coupling, 1V/div, 1ms/div start, trigger CH1 rising ~1V, BW limit 20 MHz on
- **Safety:** All clips to J1 pin 1 only — never to chassis ground

---

## Software

### Capture tool
- **File:** `vmux_capture.py`
- **Dependency:** `pyserial` (`pip install -r requirements.txt`)
- **Key classes:**
  - `PacketAssembler` — idle-gap framing, byte-by-byte feed, emits `VmuxPacket`
  - `SyncDetector` — detects SYNC period to confirm baud rate
  - `CaptureLogger` — writes CSV + binary log per session
  - `Display` — colour-coded terminal output (magenta=SYNC, green=known, yellow=unknown)
- **Output:** `vmux_capture_YYYYMMDD_HHMMSS.csv` + `.bin` per session
- **Baud detection:** `--detect` flag tries 9600 / 19200 / 38400 / 57600 / 115200

### Common commands
```bash
python vmux_capture.py --scan                          # find port
python vmux_capture.py --port COM3 --detect            # auto baud
python vmux_capture.py --port COM3 --baud 19200        # capture
python vmux_capture.py --map vmux_capture_*.csv        # build message map
```

---

## Firmware architecture (planned)

### RTOS: FreeRTOS on STM32F405RGT6

| Layer | Task | Priority | Description |
|-------|------|----------|-------------|
| 4 | HMI renderer | 1 | LVGL on SPI display, config loaded from SD `layout.json` |
| 3 | Macro engine | 3 | Sequenced multi-message macros triggered by HMI or GPIO |
| 2 | Bus driver | 5 | USART2 DMA RX/TX, hardware DE pin, SYNC watchdog |
| 1 | HAL/peripherals | — | USART3, SPI1, I2C1, SDMMC, GPIO, TIM, IWDG |
| 0 | V-MUX bus | — | Physical RS-485 network |

### Peripheral allocation (STM32F405RGT6, LQFP64)
| Function | Peripheral | Pins |
|----------|-----------|------|
| V-MUX RS-485 | USART2 | PA2 TX, PA3 RX, PA1 DE (hardware) |
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
- **Hardware DE pin** must be used for RS-485 direction control — no GPIO toggling in ISR
- **DIR default Low** (receive-only) at all power-on and reset states
- **Passive tap only** during Phase 1 — the adapter must never drive the bus
- **SYNC monitoring** required in firmware — `VM_OUT_OF_NETWORK` from any node = fault
- **No termination** — confirmed twice because it is the most common mistake on this bus

---

## Files in this project

| File | Description |
|------|-------------|
| `vmux_capture.py` | Python RS-485 bus capture and protocol analysis tool |
| `requirements.txt` | Python dependencies (pyserial) |
| `vmux_capture_README.md` | Usage guide for the capture tool |
| `vmux_bus_tap_schematic.html` | Interactive bus tap and isolation circuit schematic |
| `vmux_replacement_architecture.html` | System architecture overview diagram |
| `agents.md` | This file — agent context and project state |

---

## Reference documents

- Weldon V-MUX Diagnostics User Manual (September 2012) — bus protocol, SYNC behaviour, BC counter, load management
- Weldon V-MUX Parts, Connectors, and Accessories Specification (March 2020) — physical layer, cable part numbers, connector kits, Port F pinout
- STM32F405 Reference Manual RM0090 — peripheral configuration
- WeAct STM32F4 64-pin Core Board schematic — board-level pin assignments
- FTDI FT232RN datasheet — USB-RS485 reference circuit (Fig 7.2, TXDEN CBUS option)
- DSD TECH SH-U11F user guide — jumper configuration, termination disable
