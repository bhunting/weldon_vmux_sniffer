# V-MUX Bus Capture Tool

RS-485 bus sniffer and protocol analyser for Braun ambulance V-MUX systems.
Designed for use with the **DSD TECH SH-U11F** isolated USB-RS485 adapter.

## Files

| File | Description |
|------|-------------|
| `vmux_capture.py` | RS-485 bus capture, packet framing, protocol analysis |
| `vmux_generator.py` | Test message generator for bench validation without the vehicle |
| `requirements.txt` | Shared dependency: `pyserial` |

## Setup

```bash
pip install -r requirements.txt
```

---

## Bench validation (no vehicle required)

Use `vmux_generator.py` on a second laptop to simulate V-MUX bus traffic and
validate the capture tool before the first vehicle connection.

**Physical connection:**
```
GENERATOR LAPTOP          RS-485 twisted pair        CAPTURE LAPTOP
vmux_generator.py    A+ ──────────────────── A+    vmux_capture.py
SH-U11F              B- ──────────────────── B-    SH-U11F
R120 jumper: ABSENT  GND─────────────────── GND    R120 jumper: ABSENT
```
Both R120 (120Ω) termination jumpers must be absent. Keep bench cable under 1m.

**Run order — start capture first, then generator:**
```bash
# Capture laptop (COM3):
python vmux_capture.py --port COM3 --baud 19200

# Generator laptop (COM4) — separate terminal:
python vmux_generator.py --port COM4 --baud 19200 --scenario full
```

**Scenarios:**

| Scenario | Duration | Tests |
|----------|----------|-------|
| `idle` | 30s | SYNC timing, baud detection |
| `basic` | 90s | Packet framing, known message decode |
| `burst` | 45s | Sub-threshold gaps: each 4-packet burst must capture as ONE 16-byte packet |
| `threshold` | 45s | Pairs at threshold ± margin (`--threshold`, `--margin`): above must split, below must merge |
| `multinode` | 60s | Node byte parsing |
| `unknown` | 45s | Unknown code yellow-highlight |
| `full` | ~3 min | All of the above in sequence |
| `interactive` | ∞ | Manual single-packet injection |

**Expected output on capture laptop during `full`:**
- Within 5s: SYNC packet in magenta
- Within 14s: `SYNC CONFIRMED OK (avg=4.xx s)`
- Known commands: green; unknown codes: yellow
- `--map` shows all generator codes with correct counts
- Burst: 16-byte merged packets only; never a SYNC merged into a burst
- Threshold: state-01 pairs as two 4-byte packets, state-00 pairs as one 8-byte packet.
  On hardware this is statistical (host/USB jitter ~0-2 ms); report the split rate.

**Precondition for any timing result:** set the receiving adapter's FTDI latency
timer to 1 ms (driver default 16 ms batches reads and defeats gap framing).
Windows: Device Manager → port → Advanced. Linux: `/sys/bus/usb-serial/devices/ttyUSBx/latency_timer`.

**If capture shows only garbage:** swap A+/B− on one adapter (polarity inversion).

---

## Quick start

**Step 1 — Find your adapter's port:**
```bash
python vmux_capture.py --scan
```
Look for the port marked `← FTDI / likely SH-U11F`.

**Step 2 — Auto-detect baud rate** (vehicle must be running, V-MUX active):
```bash
python vmux_capture.py --port COM3 --detect
```

**Step 3 — Capture with confirmed baud rate:**
```bash
python vmux_capture.py --port COM3 --baud 19200
```

**Step 4 — Analyse a previous capture to build your message map:**
```bash
python vmux_capture.py --map vmux_capture_20240101_120000.csv
```

## Before connecting to the vehicle

**Disable the SH-U11F termination resistor first.** This is the most important
pre-connection step — V-MUX explicitly forbids termination resistors anywhere on
the network.

The SH-U11F user guide states the 120Ω termination is a jumper and is
disabled by default. The jumper's location and silkscreen label are **not
documented** — open the shell, identify it, and confirm termination is off
(measure A–B resistance with the adapter unpowered and disconnected: it should
not read ~120Ω) before connecting.

Connection checklist:

J1 is the V-MUX network tap, a Deutsch DT04-4P. Pinout per Weldon Connector
Spec (2024-05) p.8 and Braun dwg 49384 Detail C ("DL" connector, behind the
ACP or in the PDQ): **pin 1 = comms A, pin 2 = comms B, pin 3 = ground
(18-BK-GND), pin 4 = power (circuit 312)**. Meter it on the vehicle first.

1. Confirm 120Ω termination is **off**.
2. Set the FTDI latency timer to 1 ms.
3. Meter J1: pin 3 to battery negative ≈ 0 Ω; note pin 4 voltage.
4. Connect adapter **A+** to J1 pin 1 (comms A).
5. Connect adapter **B−** to J1 pin 2 (comms B).
6. Connect adapter **GND** to J1 pin 3 (ground). Never put a scope ground clip on pin 1 or 2.
7. Keep RTS/DTR low — the tool does this automatically on open.

## Tests

```bash
pip install -r requirements-dev.txt
python -m py_compile vmux_capture.py vmux_generator.py
python -m pytest
git config core.hooksPath .githooks   # once per clone: runs both on every commit
```

The suite covers `build_packet`/checksum, `PacketAssembler` framing (including
the burst and threshold expectations), `SyncDetector`, `BusTx` over pyserial
`loop://`, and the burst/threshold scenarios on a fake clock. No hardware needed.

## Output files

Each capture session produces two files:

| File | Format | Use |
|------|--------|-----|
| `vmux_capture_YYYYMMDD_HHMMSS.csv` | CSV | Human review, import to Excel |
| `vmux_capture_YYYYMMDD_HHMMSS.bin` | Binary | Replay, further parsing |

### CSV columns
`timestamp_s`, `timestamp_str`, `pkt_num`, `baud`, `length`,
`gap_before_ms`, `raw_hex`, `decoded`, `msg_code_hex`, `state_byte_hex`, `node_byte`

### Binary format
Each packet record (13 + N bytes):

| Field | Type | Size | Description |
|-------|------|------|-------------|
| `epoch_ms` | int64 big-endian | 8 bytes | Full Unix timestamp in milliseconds — no wrap |
| `ts_ms` | uint32 big-endian | 4 bytes | Session-relative ms (wraps at ~49.7 days, kept for compat) |
| `length` | uint8 | 1 byte | Payload byte count |
| payload | bytes | N bytes | Raw packet bytes |

Parse with: `struct.unpack_from('>qIB', data, offset)` → `(epoch_ms, ts_ms, length)`

## Options

| Flag | Description |
|------|-------------|
| `--port PORT` | Serial port |
| `--baud N` | Baud rate (9600 / 19200 / 38400 / 57600 / 115200) |
| `--scan` | List ports and exit |
| `--detect` | Auto-detect baud rate before capturing |
| `--duration N` | Capture for N seconds then stop |
| `--output DIR` | Directory for log files (default: current dir) |
| `--verbose` | Per-byte breakdown for each packet |
| `--quiet` | Log only — no terminal display |
| `--gap N` | Idle gap threshold in ms for packet boundary (default: 10) |
| `--map CSV` | Analyse CSV and print message map |

## Baud rate detection

The tool tries each candidate baud rate (9600, 19200, 38400, 57600, 115200)
for ~6 seconds each and scores based on:

- Byte distribution entropy (real UART is non-uniform)
- Repeated byte sequence detection (SYNC packet repeats every ~4 s)

The SYNC packet from Node 1 is the anchor — it fires every ~4 seconds
and is the most regular, identifiable packet on the V-MUX bus.
Once detected, the tool confirms whether the measured interval matches
the expected 4-second period, verifying the baud rate.

## Building your message map

After a capture session, run `--map` against the CSV. You will see a table like:

```
Code       Count    States seen              Known label              Example hex
──────────────────────────────────────────────────────────────────────────────────
0x50       47       0x00                     Sync                     50 00 01
0x10       12       0x00, 0x01               Emergency Master         10 01 01 00
0x20       8        0x00, 0x01               Scene Lights             20 01 01 00
0x??       3        0x01                     — annotate me —          ?? 01 02 00
```

Annotate unknown codes in the `VMUX_KNOWN_COMMANDS` dict in `vmux_capture.py`
by operating each vehicle function one at a time during a capture session.

## Protocol hypothesis

Based on Weldon V-MUX documentation, each packet is expected to contain:

```
[msg_code: 1 byte] [state: 1 byte (0x00=OFF, 0x01=ON)] [node: 1 byte] [...] [checksum?]
```

Packet boundaries are identified by inter-message idle gaps (default 10 ms).
This hypothesis will be refined as real capture data is analysed.
The `--gap` parameter can be tuned if packets are being split incorrectly.
