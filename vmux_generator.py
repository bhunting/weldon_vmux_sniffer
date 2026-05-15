#!/usr/bin/env python3
"""
vmux_generator.py — V-MUX RS-485 test message generator  v1
DSD TECH SH-U11F adapter · Braun ambulance 2013 · Phase 1 capture tool validation

PURPOSE
  Simulates a realistic V-MUX bus on a bench RS-485 link so that vmux_capture.py
  can be validated without connecting to the vehicle.  Runs on a separate laptop
  connected via a second SH-U11F (or any USB-RS485 adapter) to the same RS-485
  twisted pair as the capture laptop.

PHYSICAL CONNECTION — see connection diagram at bottom of this file.

SCENARIOS
  idle        Continuous SYNC only (Node 1 broadcast, ~4s period)
  basic       SYNC + common commands ON/OFF with realistic timing
  burst       Rapid back-to-back packets to stress gap-framing detection
  multinode   Traffic from multiple simulated nodes (1, 2, 3)
  unknown     Mix of known and undocumented message codes
  full        Full automated test suite — runs all scenarios in sequence
  interactive Command-line driven single-packet injection

Usage:
    python vmux_generator.py --port COM4 --baud 19200 --scenario basic
    python vmux_generator.py --port COM4 --baud 19200 --scenario full
    python vmux_generator.py --port COM4 --baud 19200 --scenario interactive
    python vmux_generator.py --scan
"""

import serial
import serial.tools.list_ports
import argparse
import time
import sys
import random
import struct
from dataclasses import dataclass
from typing import Optional, List, Tuple


# ─────────────────────────────────────────────
#  Protocol constants — must match vmux_capture.py
# ─────────────────────────────────────────────

VMUX_SYNC_INTERVAL_S  = 4.0    # Node 1 SYNC period (seconds)
VMUX_SYNC_JITTER_S    = 0.050  # ±50ms timing jitter on SYNC (simulates real HW)
VMUX_IDLE_GAP_MS      = 10     # capture tool gap threshold — generator must exceed this
VMUX_MIN_GAP_MS       = 20     # minimum enforced gap between packets (2× threshold)
VMUX_INTER_MSG_MS     = 5      # gap between messages within the same burst (< gap threshold)
                                # NOTE: keep < VMUX_IDLE_GAP_MS to test burst detection

# Packet field positions
IDX_MSG_CODE  = 0
IDX_STATE     = 1
IDX_NODE      = 2
IDX_CHECKSUM  = 3
PACKET_LEN    = 4               # [msg_code][state][node][xor_checksum]

# State values
STATE_OFF = 0x00
STATE_ON  = 0x01

# Known message codes — subset matching vmux_capture.py VMUX_KNOWN_COMMANDS
MSG_FORWARD          = 0x01
MSG_REVERSE          = 0x04
MSG_PARK             = 0x02
MSG_EMERGENCY_MASTER = 0x10
MSG_FRONT_LIGHT_BAR  = 0x11
MSG_GRILL_LIGHTS     = 0x12
MSG_WARNING_FRONT    = 0x13
MSG_WARNING_REAR     = 0x14
MSG_SCENE_LIGHTS     = 0x20
MSG_COMPARTMENT_1    = 0x21
MSG_COMPARTMENT_2    = 0x22
MSG_SIREN            = 0x30
MSG_DOOR_LEFT        = 0x40
MSG_DOOR_RIGHT       = 0x41
MSG_SYNC             = 0x50
MSG_PING             = 0xFF

# Unknown codes for testing capture tool's yellow-highlight path
UNKNOWN_CODES = [0x60, 0x61, 0x70, 0x80, 0xA0, 0xB5]

NODE_MASTER  = 0x01
NODE_SCENE   = 0x02
NODE_LIGHTS  = 0x03

ANSI_GREEN   = "\033[92m"
ANSI_YELLOW  = "\033[93m"
ANSI_RED     = "\033[91m"
ANSI_CYAN    = "\033[96m"
ANSI_MAGENTA = "\033[95m"
ANSI_RESET   = "\033[0m"
ANSI_BOLD    = "\033[1m"
ANSI_DIM     = "\033[2m"

MSG_NAMES = {
    MSG_FORWARD:          "Forward",
    MSG_REVERSE:          "Reverse",
    MSG_PARK:             "Park",
    MSG_EMERGENCY_MASTER: "Emergency Master",
    MSG_FRONT_LIGHT_BAR:  "Front Light Bar",
    MSG_GRILL_LIGHTS:     "Grill Lights",
    MSG_WARNING_FRONT:    "Warning Lights Front",
    MSG_WARNING_REAR:     "Warning Lights Rear",
    MSG_SCENE_LIGHTS:     "Scene Lights",
    MSG_COMPARTMENT_1:    "Compartment Light 1",
    MSG_COMPARTMENT_2:    "Compartment Light 2",
    MSG_SIREN:            "Siren",
    MSG_DOOR_LEFT:        "Door Cab Left",
    MSG_DOOR_RIGHT:       "Door Cab Right",
    MSG_SYNC:             "Sync",
    MSG_PING:             "Ping/Reply",
}


# ─────────────────────────────────────────────
#  Packet construction
# ─────────────────────────────────────────────

def checksum(msg_code: int, state: int, node: int) -> int:
    """XOR checksum over msg_code, state, node bytes."""
    return msg_code ^ state ^ node


def build_packet(msg_code: int, state: int, node: int) -> bytes:
    """
    Build a 4-byte V-MUX packet.
    Format: [msg_code][state][node][xor_checksum]
    This matches the capture tool's decode_attempt() byte-position assumptions.
    """
    cs = checksum(msg_code, state, node)
    return bytes([msg_code, state, node, cs])


def sync_packet(node: int = NODE_MASTER) -> bytes:
    return build_packet(MSG_SYNC, STATE_OFF, node)


# ─────────────────────────────────────────────
#  Port discovery
# ─────────────────────────────────────────────

def scan_ports() -> list:
    ports = serial.tools.list_ports.comports()
    results = []
    for p in sorted(ports, key=lambda x: x.device):
        is_ftdi = (
            "FTDI" in (p.manufacturer or "") or
            "FT232" in (p.description or "") or
            (hasattr(p, "vid") and p.vid == 0x0403)
        )
        results.append((p.device, p.description or "Unknown", is_ftdi))
    return results


def print_ports():
    ports = scan_ports()
    if not ports:
        print(f"{ANSI_RED}No serial ports found.{ANSI_RESET}")
        return
    print(f"\n{ANSI_BOLD}Available serial ports:{ANSI_RESET}")
    print(f"{'Port':<16} {'Description':<40} {'Note'}")
    print("─" * 72)
    for device, desc, is_ftdi in ports:
        marker = f"{ANSI_GREEN}← FTDI (generator candidate){ANSI_RESET}" if is_ftdi else ""
        print(f"{device:<16} {desc:<40} {marker}")
    print()


# ─────────────────────────────────────────────
#  Bus transmitter
# ─────────────────────────────────────────────

class BusTx:
    """
    Wraps a serial port with RS-485 direction control and transmission logging.

    Direction control strategy for SH-U11F:
      The SH-U11F FT232RL uses CBUS TXDEN to drive DE automatically — the chip
      asserts DE on TX start and deasserts after the last stop bit, in hardware.
      No manual RTS/DTR manipulation is needed or correct for this adapter.
      rts and dtr are left False to avoid contention.

    For adapters without hardware auto-DE (e.g. generic CH340-based adapters):
      Set rts=True before write(), rts=False after write() completes. The
      --manual-de flag enables this mode.
    """

    def __init__(self, port: str, baud: int, manual_de: bool = False,
                 quiet: bool = False):
        self.baud      = baud
        self.manual_de = manual_de
        self.quiet     = quiet
        self._pkt_count = 0
        self._byte_time_s = 10.0 / baud   # 8N1

        try:
            self._ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
                timeout=1,
                write_timeout=1,
            )
        except serial.SerialException as e:
            print(f"\n{ANSI_RED}Failed to open {port}: {e}{ANSI_RESET}")
            sys.exit(1)

        # Settle control lines
        self._ser.rts = False
        self._ser.dtr = False
        time.sleep(0.10)
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def send(self, pkt: bytes, label: str = "",
             gap_before_ms: float = VMUX_MIN_GAP_MS) -> None:
        """
        Transmit one packet onto the RS-485 bus.
        Enforces a minimum idle gap before transmission.
        Waits for the full packet to clear the UART shift register before returning,
        so the caller can immediately enforce the next gap without a race.
        """
        # Enforce minimum inter-packet gap
        if gap_before_ms > 0:
            time.sleep(gap_before_ms / 1000.0)

        if self.manual_de:
            self._ser.rts = True
            time.sleep(0.001)   # brief propagation delay for DE assert

        try:
            self._ser.write(pkt)
            self._ser.flush()
        except serial.SerialTimeoutException:
            print(f"{ANSI_RED}Write timeout — check cable and adapter{ANSI_RESET}")
            if self.manual_de:
                self._ser.rts = False
            return

        # Wait for all bytes to leave the shift register before releasing DE.
        # Physical TX time = PACKET_LEN bytes × byte_time_s
        tx_duration_s = len(pkt) * self._byte_time_s
        time.sleep(tx_duration_s + 0.001)   # +1ms margin

        if self.manual_de:
            self._ser.rts = False

        self._pkt_count += 1

        if not self.quiet:
            self._log(pkt, label)

    def _log(self, pkt: bytes, label: str) -> None:
        ts = time.strftime("%H:%M:%S")
        hex_str = " ".join(f"{b:02X}" for b in pkt)
        code    = pkt[0] if pkt else 0
        colour  = ANSI_MAGENTA if code == MSG_SYNC else ANSI_GREEN
        name    = MSG_NAMES.get(code, f"UNKNOWN_0x{code:02X}")
        if not label:
            label = name
        print(f"{colour}{ts}  TX  [{hex_str}]  {label}{ANSI_RESET}")

    def close(self):
        self._ser.rts = False
        self._ser.close()

    @property
    def packet_count(self) -> int:
        return self._pkt_count


# ─────────────────────────────────────────────
#  SYNC scheduler
# ─────────────────────────────────────────────

class SyncScheduler:
    """
    Fires a SYNC packet from Node 1 every VMUX_SYNC_INTERVAL_S seconds
    with optional ±jitter, matching real V-MUX master behaviour.
    """

    def __init__(self, tx: BusTx, interval_s: float = VMUX_SYNC_INTERVAL_S,
                 jitter_s: float = VMUX_SYNC_JITTER_S):
        self._tx       = tx
        self._interval = interval_s
        self._jitter   = jitter_s
        self._next_t   = time.time() + interval_s  # first SYNC after one full interval

    def tick(self) -> bool:
        """
        Call frequently in the main loop. Sends SYNC if due.
        Returns True if a SYNC was sent this tick.
        """
        now = time.time()
        if now >= self._next_t:
            pkt = sync_packet(NODE_MASTER)
            self._tx.send(pkt, label="SYNC (Node 1)", gap_before_ms=VMUX_MIN_GAP_MS)
            jitter = random.uniform(-self._jitter, self._jitter)
            self._next_t = now + self._interval + jitter
            return True
        return False

    def time_to_next(self) -> float:
        return max(0.0, self._next_t - time.time())


# ─────────────────────────────────────────────
#  Scenario: idle — SYNC only
# ─────────────────────────────────────────────

def scenario_idle(tx: BusTx, duration: float) -> None:
    """
    Emit SYNC every ~4s, nothing else.
    Tests: SyncDetector confirmation, baud rate detection scoring.
    """
    print(f"\n{ANSI_CYAN}Scenario: IDLE — SYNC only for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Tests: SyncDetector interval confirmation, detect_baud scoring{ANSI_RESET}\n")

    sync = SyncScheduler(tx)
    t_end = time.time() + duration

    # Send first SYNC immediately so capture tool sees it quickly
    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    while time.time() < t_end:
        sync.tick()
        time.sleep(0.050)


# ─────────────────────────────────────────────
#  Scenario: basic — realistic mixed traffic
# ─────────────────────────────────────────────

def scenario_basic(tx: BusTx, duration: float) -> None:
    """
    SYNC every 4s plus common command ON/OFF cycles with realistic spacing.
    Tests: packet framing, known message decoding, idle gap correctness.
    """
    print(f"\n{ANSI_CYAN}Scenario: BASIC — realistic traffic for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Tests: packet framing, known message decode, idle gap boundary{ANSI_RESET}\n")

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    # Initial SYNC
    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    # Command sequence — each command ON, pause, OFF, pause
    commands = [
        (MSG_EMERGENCY_MASTER, NODE_MASTER, "Emergency Master"),
        (MSG_SCENE_LIGHTS,     NODE_SCENE,  "Scene Lights"),
        (MSG_FRONT_LIGHT_BAR,  NODE_LIGHTS, "Front Light Bar"),
        (MSG_GRILL_LIGHTS,     NODE_LIGHTS, "Grill Lights"),
        (MSG_WARNING_FRONT,    NODE_LIGHTS, "Warning Lights Front"),
        (MSG_WARNING_REAR,     NODE_LIGHTS, "Warning Lights Rear"),
        (MSG_COMPARTMENT_1,    NODE_SCENE,  "Compartment Light 1"),
        (MSG_COMPARTMENT_2,    NODE_SCENE,  "Compartment Light 2"),
        (MSG_SIREN,            NODE_MASTER, "Siren"),
        (MSG_DOOR_LEFT,        NODE_SCENE,  "Door Cab Left"),
        (MSG_DOOR_RIGHT,       NODE_SCENE,  "Door Cab Right"),
        (MSG_FORWARD,          NODE_MASTER, "Forward"),
        (MSG_REVERSE,          NODE_MASTER, "Reverse"),
        (MSG_PARK,             NODE_MASTER, "Park"),
    ]

    cmd_idx = 0

    while time.time() < t_end:
        if sync.tick():
            continue

        if time.time() < t_end and cmd_idx < len(commands):
            code, node, name = commands[cmd_idx]

            # ON
            tx.send(build_packet(code, STATE_ON, node),
                    label=f"{name} ON", gap_before_ms=VMUX_MIN_GAP_MS)
            if sync.tick(): continue

            time.sleep(random.uniform(0.5, 2.0))   # hold time
            if sync.tick(): continue

            # OFF
            tx.send(build_packet(code, STATE_OFF, node),
                    label=f"{name} OFF", gap_before_ms=VMUX_MIN_GAP_MS)
            if sync.tick(): continue

            cmd_idx = (cmd_idx + 1) % len(commands)
            time.sleep(random.uniform(0.2, 0.8))
        else:
            time.sleep(0.050)


# ─────────────────────────────────────────────
#  Scenario: burst — rapid back-to-back packets
# ─────────────────────────────────────────────

def scenario_burst(tx: BusTx, duration: float) -> None:
    """
    Send packets with gaps shorter than VMUX_IDLE_GAP_MS to test burst
    handling, then normal-gap packets to confirm boundary detection.

    Tests:
      - F1: timestamp interpolation must correctly assign distinct times to
            each byte in a buffered chunk — all bytes same timestamp would
            collapse a burst into one giant packet
      - F2: idle flush must trigger correctly after the burst ends
      - PacketAssembler must NOT split intra-burst bytes into separate packets
    """
    print(f"\n{ANSI_CYAN}Scenario: BURST — stress gap-framing for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Tests: F1 timestamp interpolation, intra-burst framing, idle flush (F2){ANSI_RESET}")
    print(f"{ANSI_DIM}Intra-burst gap = {VMUX_INTER_MSG_MS}ms  (< {VMUX_IDLE_GAP_MS}ms threshold){ANSI_RESET}")
    print(f"{ANSI_DIM}Inter-burst gap = {VMUX_MIN_GAP_MS}ms  (> {VMUX_IDLE_GAP_MS}ms threshold){ANSI_RESET}\n")

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    burst_commands = [
        (MSG_SCENE_LIGHTS,    STATE_ON,  NODE_SCENE,  "Scene Lights ON"),
        (MSG_FRONT_LIGHT_BAR, STATE_ON,  NODE_LIGHTS, "Front Light Bar ON"),
        (MSG_GRILL_LIGHTS,    STATE_ON,  NODE_LIGHTS, "Grill Lights ON"),
        (MSG_WARNING_FRONT,   STATE_ON,  NODE_LIGHTS, "Warning Front ON"),
    ]
    burst_off = [
        (MSG_SCENE_LIGHTS,    STATE_OFF, NODE_SCENE,  "Scene Lights OFF"),
        (MSG_FRONT_LIGHT_BAR, STATE_OFF, NODE_LIGHTS, "Front Light Bar OFF"),
        (MSG_GRILL_LIGHTS,    STATE_OFF, NODE_LIGHTS, "Grill Lights OFF"),
        (MSG_WARNING_FRONT,   STATE_OFF, NODE_LIGHTS, "Warning Front OFF"),
    ]

    burst_n = 0
    while time.time() < t_end:
        if sync.tick():
            continue

        burst_n += 1
        print(f"{ANSI_DIM}  — burst #{burst_n} start —{ANSI_RESET}")

        # Fire burst ON packets with sub-threshold gap (intra-burst)
        for i, (code, state, node, label) in enumerate(burst_commands):
            gap = VMUX_MIN_GAP_MS if i == 0 else VMUX_INTER_MSG_MS
            tx.send(build_packet(code, state, node), label=label,
                    gap_before_ms=gap)
            if sync.tick(): break

        # Deliberate long pause — triggers idle flush in capture tool (F2)
        time.sleep(random.uniform(0.8, 1.5))
        if sync.tick(): pass

        # Burst OFF
        for i, (code, state, node, label) in enumerate(burst_off):
            gap = VMUX_MIN_GAP_MS if i == 0 else VMUX_INTER_MSG_MS
            tx.send(build_packet(code, state, node), label=label,
                    gap_before_ms=gap)

        print(f"{ANSI_DIM}  — burst #{burst_n} end —{ANSI_RESET}")
        time.sleep(random.uniform(0.5, 1.2))


# ─────────────────────────────────────────────
#  Scenario: multinode — multiple simulated nodes
# ─────────────────────────────────────────────

def scenario_multinode(tx: BusTx, duration: float) -> None:
    """
    Simulate traffic from three nodes interleaved realistically.
    Tests: node byte parsing in capture tool, message_counts per-code.
    """
    print(f"\n{ANSI_CYAN}Scenario: MULTINODE — 3 simulated nodes for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Tests: node byte field parsing, per-code message frequency counts{ANSI_RESET}\n")

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    tx.send(sync_packet(NODE_MASTER), label="SYNC (Node 1)", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    node_sequences = {
        NODE_MASTER: [
            (MSG_EMERGENCY_MASTER, STATE_ON),
            (MSG_EMERGENCY_MASTER, STATE_OFF),
            (MSG_SIREN,            STATE_ON),
            (MSG_SIREN,            STATE_OFF),
        ],
        NODE_SCENE: [
            (MSG_SCENE_LIGHTS,    STATE_ON),
            (MSG_COMPARTMENT_1,   STATE_ON),
            (MSG_COMPARTMENT_2,   STATE_ON),
            (MSG_SCENE_LIGHTS,    STATE_OFF),
            (MSG_COMPARTMENT_1,   STATE_OFF),
            (MSG_COMPARTMENT_2,   STATE_OFF),
        ],
        NODE_LIGHTS: [
            (MSG_FRONT_LIGHT_BAR, STATE_ON),
            (MSG_GRILL_LIGHTS,    STATE_ON),
            (MSG_WARNING_FRONT,   STATE_ON),
            (MSG_WARNING_REAR,    STATE_ON),
            (MSG_FRONT_LIGHT_BAR, STATE_OFF),
            (MSG_GRILL_LIGHTS,    STATE_OFF),
            (MSG_WARNING_FRONT,   STATE_OFF),
            (MSG_WARNING_REAR,    STATE_OFF),
        ],
    }

    indices = {n: 0 for n in node_sequences}

    while time.time() < t_end:
        if sync.tick():
            continue

        # Round-robin across nodes with variable delay
        for node, seq in node_sequences.items():
            if time.time() >= t_end:
                break
            if sync.tick():
                break

            idx  = indices[node]
            code, state = seq[idx]
            name = MSG_NAMES.get(code, f"0x{code:02X}")
            state_str = "ON" if state else "OFF"

            tx.send(build_packet(code, state, node),
                    label=f"{name} {state_str} (node {node})",
                    gap_before_ms=VMUX_MIN_GAP_MS)

            indices[node] = (idx + 1) % len(seq)
            time.sleep(random.uniform(0.15, 0.40))


# ─────────────────────────────────────────────
#  Scenario: unknown — undocumented message codes
# ─────────────────────────────────────────────

def scenario_unknown(tx: BusTx, duration: float) -> None:
    """
    Mix known and unknown message codes.
    Tests: capture tool yellow-highlight path for undocumented codes,
           message_counts accumulation for codes not in VMUX_KNOWN_COMMANDS.
    """
    print(f"\n{ANSI_CYAN}Scenario: UNKNOWN codes — mixed known/unknown for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Tests: yellow unknown highlight, VMUX_KNOWN_COMMANDS miss path{ANSI_RESET}")
    print(f"{ANSI_DIM}Unknown codes used: {[hex(c) for c in UNKNOWN_CODES]}{ANSI_RESET}\n")

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    all_codes = [
        (MSG_SCENE_LIGHTS,    NODE_SCENE),
        (UNKNOWN_CODES[0],    NODE_MASTER),
        (MSG_FRONT_LIGHT_BAR, NODE_LIGHTS),
        (UNKNOWN_CODES[1],    NODE_SCENE),
        (MSG_SIREN,           NODE_MASTER),
        (UNKNOWN_CODES[2],    NODE_LIGHTS),
        (MSG_COMPARTMENT_1,   NODE_SCENE),
        (UNKNOWN_CODES[3],    NODE_MASTER),
        (MSG_GRILL_LIGHTS,    NODE_LIGHTS),
        (UNKNOWN_CODES[4],    NODE_SCENE),
        (MSG_EMERGENCY_MASTER,NODE_MASTER),
        (UNKNOWN_CODES[5],    NODE_LIGHTS),
    ]

    idx = 0
    while time.time() < t_end:
        if sync.tick():
            continue

        code, node = all_codes[idx % len(all_codes)]
        state = random.choice([STATE_ON, STATE_OFF])
        name  = MSG_NAMES.get(code, f"UNKNOWN_0x{code:02X}")
        state_str = "ON" if state else "OFF"

        tx.send(build_packet(code, state, node),
                label=f"{name} {state_str}",
                gap_before_ms=VMUX_MIN_GAP_MS)
        idx += 1
        time.sleep(random.uniform(0.2, 0.6))


# ─────────────────────────────────────────────
#  Scenario: full — automated test suite
# ─────────────────────────────────────────────

def scenario_full(tx: BusTx) -> None:
    """
    Run all scenarios in sequence with inter-scenario breaks.
    Provides a complete end-to-end validation of vmux_capture.py.
    """
    print(f"\n{ANSI_BOLD}{'═' * 60}{ANSI_RESET}")
    print(f"{ANSI_BOLD}  Full automated test suite{ANSI_RESET}")
    print(f"{ANSI_BOLD}{'═' * 60}{ANSI_RESET}")
    print(f"\nExpected capture tool output after full run:")
    print(f"  ≥3 SYNC packets (baud rate confirmed)")
    print(f"  All known message codes captured")
    print(f"  ≥3 unknown codes captured (yellow in terminal)")
    print(f"  Node 1, 2, 3 all appear in node field")
    print(f"  No merged packets in burst scenario\n")

    steps = [
        ("IDLE",      20,  scenario_idle),
        ("BASIC",     60,  scenario_basic),
        ("BURST",     30,  scenario_burst),
        ("MULTINODE", 40,  scenario_multinode),
        ("UNKNOWN",   30,  scenario_unknown),
    ]

    total_s = sum(d for _, d, _ in steps)
    print(f"Total run time: ~{total_s}s ({total_s//60}m {total_s%60}s)\n")

    for name, dur, fn in steps:
        print(f"\n{'─' * 60}")
        print(f"{ANSI_CYAN}Starting: {name}  ({dur}s){ANSI_RESET}")
        print(f"{'─' * 60}")
        fn(tx, float(dur))
        print(f"\n{ANSI_DIM}  Pausing 3s between scenarios...{ANSI_RESET}")
        time.sleep(3.0)

    print(f"\n{ANSI_GREEN}{ANSI_BOLD}Full test suite complete.{ANSI_RESET}")
    print(f"Total packets sent: {tx.packet_count}")


# ─────────────────────────────────────────────
#  Scenario: interactive — manual packet injection
# ─────────────────────────────────────────────

def scenario_interactive(tx: BusTx) -> None:
    """
    Manual command-line packet injection. Runs a background SYNC scheduler
    while accepting commands from stdin.
    """
    print(f"\n{ANSI_CYAN}Interactive mode{ANSI_RESET} — SYNC running in background (~4s)")
    print("Commands:")
    print("  sync              — send SYNC manually")
    print("  <code> <on|off> [node]  — e.g.  0x20 on  or  0x10 off 3")
    print("  list              — show all known codes")
    print("  quit / q          — exit\n")

    sync = SyncScheduler(tx)

    # Send initial SYNC
    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    while True:
        # Background SYNC
        sync.tick()

        try:
            raw = input(f"{ANSI_DIM}vmux> {ANSI_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            continue

        parts = raw.split()
        cmd   = parts[0].lower()

        if cmd in ("quit", "q", "exit"):
            break

        elif cmd == "sync":
            tx.send(sync_packet(), label="SYNC (manual)", gap_before_ms=VMUX_MIN_GAP_MS)

        elif cmd == "list":
            print(f"\n{'Code':<8} {'Label'}")
            print("─" * 36)
            for code, name in sorted(MSG_NAMES.items()):
                print(f"0x{code:02X}    {name}")
            print()

        else:
            # <code> <on|off> [node]
            try:
                code_s  = parts[0]
                code    = int(code_s, 16) if code_s.startswith("0x") else int(code_s)
                state_s = parts[1].lower() if len(parts) > 1 else "on"
                state   = STATE_ON if state_s in ("on", "1") else STATE_OFF
                node    = int(parts[2]) if len(parts) > 2 else NODE_MASTER

                pkt  = build_packet(code, state, node)
                name = MSG_NAMES.get(code, f"UNKNOWN_0x{code:02X}")
                tx.send(pkt, label=f"{name} {'ON' if state else 'OFF'} (node {node})",
                        gap_before_ms=VMUX_MIN_GAP_MS)

            except (ValueError, IndexError):
                print(f"{ANSI_RED}Syntax: <hex_code> <on|off> [node]{ANSI_RESET}")
                print(f"  Example: 0x20 on 2   or   0x50 off")

        sync.tick()


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="V-MUX RS-485 test message generator — vmux_capture.py validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios:
  idle         SYNC only — validates baud detection and SyncDetector
  basic        Realistic command traffic — validates packet framing and decode
  burst        Rapid packets — validates F1 timestamp interpolation and F2 idle flush
  multinode    Multi-node traffic — validates node field parsing
  unknown      Mixed known/unknown codes — validates yellow-highlight path
  full         All scenarios in sequence (~3 min total)
  interactive  Manual packet injection with background SYNC

Examples:
  python vmux_generator.py --scan
  python vmux_generator.py --port COM4 --baud 19200 --scenario idle
  python vmux_generator.py --port COM4 --baud 19200 --scenario full
  python vmux_generator.py --port COM4 --baud 19200 --scenario interactive
  python vmux_generator.py --port COM4 --baud 19200 --scenario basic --duration 120
  python vmux_generator.py --port COM4 --baud 19200 --scenario burst --manual-de

Physical connection:
  Generator laptop SH-U11F  →  Capture laptop SH-U11F
  A+ (terminal)              →  A+ (terminal)
  B- (terminal)              →  B- (terminal)
  GND (terminal)             →  GND (terminal)
  Both adapters: R120 termination jumper ABSENT
        """
    )
    parser.add_argument("--port",      help="Serial port for generator adapter")
    parser.add_argument("--baud",      type=int, default=19200,
                        help="Baud rate — must match vmux_capture.py (default: 19200)")
    parser.add_argument("--scenario",  default="basic",
                        choices=["idle","basic","burst","multinode",
                                 "unknown","full","interactive"],
                        help="Test scenario to run (default: basic)")
    parser.add_argument("--duration",  type=float, default=None,
                        help="Override scenario duration in seconds")
    parser.add_argument("--scan",      action="store_true",
                        help="List available ports and exit")
    parser.add_argument("--quiet",     action="store_true",
                        help="Suppress per-packet terminal output")
    parser.add_argument("--manual-de", action="store_true", dest="manual_de",
                        help="Use RTS for manual DE control (non-SH-U11F adapters)")
    parser.add_argument("--gap",       type=float, default=VMUX_MIN_GAP_MS,
                        help=f"Minimum inter-packet gap in ms (default: {VMUX_MIN_GAP_MS})")

    args = parser.parse_args()

    if args.scan:
        print_ports()
        return

    if not args.port:
        parser.print_help()
        print(f"\n{ANSI_RED}Error: --port required. Use --scan to list ports.{ANSI_RESET}\n")
        sys.exit(1)

    global VMUX_MIN_GAP_MS
    VMUX_MIN_GAP_MS = args.gap

    print(f"\n{ANSI_BOLD}V-MUX Generator{ANSI_RESET}")
    print(f"  Port:     {args.port}")
    print(f"  Baud:     {args.baud:,}")
    print(f"  Scenario: {args.scenario}")
    print(f"  Gap:      {VMUX_MIN_GAP_MS}ms min inter-packet")
    print(f"  Manual DE:{args.manual_de}")

    tx = BusTx(port=args.port, baud=args.baud,
               manual_de=args.manual_de, quiet=args.quiet)

    default_durations = {
        "idle":      30.0,
        "basic":     90.0,
        "burst":     45.0,
        "multinode": 60.0,
        "unknown":   45.0,
    }

    try:
        if args.scenario == "full":
            scenario_full(tx)

        elif args.scenario == "interactive":
            scenario_interactive(tx)

        else:
            dur = args.duration or default_durations[args.scenario]
            scenarios = {
                "idle":      scenario_idle,
                "basic":     scenario_basic,
                "burst":     scenario_burst,
                "multinode": scenario_multinode,
                "unknown":   scenario_unknown,
            }
            scenarios[args.scenario](tx, dur)

    except KeyboardInterrupt:
        print(f"\n\n{ANSI_YELLOW}Generator stopped by user.{ANSI_RESET}")

    finally:
        tx.close()
        print(f"\n{ANSI_GREEN}Done. Total packets sent: {tx.packet_count}{ANSI_RESET}\n")


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
#  PHYSICAL CONNECTION DIAGRAM
# ═══════════════════════════════════════════════════════════════════════════
#
#   GENERATOR LAPTOP                        CAPTURE LAPTOP
#   ─────────────────                       ─────────────────
#   vmux_generator.py                       vmux_capture.py
#        │                                       │
#   SH-U11F (USB-RS485)                    SH-U11F (USB-RS485)
#   R120 jumper: ABSENT                    R120 jumper: ABSENT
#        │                                       │
#   ┌────┴────────────────────────────────────┴────┐
#   │  A+  ──────────────────────────────────  A+  │
#   │  B-  ──────────────────────────────────  B-  │  RS-485 twisted pair
#   │ GND  ─────────────────────────────────  GND  │  (any length up to ~1m bench)
#   └──────────────────────────────────────────────┘
#
#   WIRING NOTES:
#   • Use twisted pair cable (Cat5/6 or dedicated RS-485 cable)
#   • Keep bench cable short (<1m); no termination resistors at either end
#   • Both adapters share GND — connect GND terminals together
#   • Both R120 (120Ω) jumpers must be ABSENT — adding termination on a
#     2-node bench link with short cable will cause signal reflection but
#     is NOT catastrophic; still, match vehicle configuration: no termination
#   • Verify A+/B- polarity — if capture shows only garbage, swap A+/B-
#     on one adapter
#
#   RUNTIME CONFIGURATION:
#   • Baud rate MUST match on both tools:
#       generator:  --baud 19200
#       capture:    --baud 19200   (or --detect)
#   • Run capture tool FIRST, then start generator
#   • Capture tool port:    the port NOT used by the generator
#   • Generator port:       the port NOT used by the capture tool
#   • On Windows: generator on COM4, capture on COM3 (example)
#   • On Linux:   generator on /dev/ttyUSB1, capture on /dev/ttyUSB0
#
#   EXPECTED CAPTURE TOOL BEHAVIOUR:
#   • Within 5s: first SYNC packet appears (magenta)
#   • Within 10s: "SYNC detected — verifying baud rate..." message
#   • Within 14s: "Baud rate CONFIRMED OK (SYNC avg=4.xx s)" message
#   • Known commands: green in terminal
#   • Unknown codes (scenario 'unknown'): yellow in terminal
#   • --map output: all transmitted codes appear in frequency table
#
# ═══════════════════════════════════════════════════════════════════════════
