# V-MUX Bus Capture Tool

RS-485 bus sniffer and protocol analyser for Braun ambulance V-MUX systems.
Designed for use with the **DSD TECH SH-U11F** isolated USB-RS485 adapter.

## Setup

```bash
pip install -r requirements.txt
```

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

Open the SH-U11F enclosure (two screws on the underside). On the PCB, locate
the 2-pin header labelled **R120** or **120R** near the terminal block end. This
jumper enables a 120Ω resistor across A+ and B−. It ships with the jumper
absent (disabled) by default — confirm it is absent before connecting.
If a jumper block is present, remove it and store it safely.

Connection checklist:

1. Confirm R120 jumper is **absent** (120Ω OFF).
2. Connect adapter **A+** to J1 pin 2 (BUS A).
3. Connect adapter **B−** to J1 pin 3 (BUS B).
4. Connect adapter **GND** to J1 pin 1 (bus GND) — NOT to chassis ground.
5. Keep RTS/DTR low — the tool does this automatically on open.

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
