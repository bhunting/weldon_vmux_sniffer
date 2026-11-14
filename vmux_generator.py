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
  threshold   Packet pairs straddling the capture gap threshold (split/merge check)
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
VMUX_THRESHOLD_MARGIN_MS = 3.0  # threshold scenario: request threshold ± this margin.
                                # Must exceed host-side timing jitter (sleep resolution,
                                # USB frame, FTDI latency timer) or results are noisy.

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

    Direction control strategy for SH-U11F — UNVERIFIED:
      The SH-U11F user guide does not document how DE is driven. FT232R CBUS
      TXDEN (hardware auto-DE) is a plausible implementation but is NOT
      confirmed for this adapter. Default mode assumes automatic direction
      control and leaves rts/dtr False. Confirm on a scope (A/B differential
      while transmitting) before relying on it.

    If packets do not appear on the bus (adapter without auto-DE):
      --manual-de sets rts=True before write() and rts=False after the
      shift-register wait. RTS polarity/wiring to DE is also unverified.
    """

    def __init__(self, port: str, baud: int, manual_de: bool = False,
                 quiet: bool = False, min_gap_ms: float = VMUX_MIN_GAP_MS):
        self.baud       = baud
        self.manual_de  = manual_de
        self.quiet      = quiet
        self.min_gap_ms = min_gap_ms   # default idle gap before each packet (--gap)
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
             gap_before_ms: Optional[float] = None) -> None:
        """
        Transmit one packet onto the RS-485 bus.
        gap_before_ms=None uses self.min_gap_ms (the --gap setting). Scenarios
        that deliberately need a shorter gap (burst test) pass an explicit value.
        Waits for the full packet to clear the UART shift register before returning,
        so the caller can immediately enforce the next gap without a race.
        """
        if gap_before_ms is None:
            gap_before_ms = self.min_gap_ms
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
            self._tx.send(pkt, label="SYNC (Node 1)")
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
                    label=f"{name} ON")
            if sync.tick(): continue

            time.sleep(random.uniform(0.5, 2.0))   # hold time
            if sync.tick(): continue

            # OFF
            tx.send(build_packet(code, STATE_OFF, node),
                    label=f"{name} OFF")
            if sync.tick(): continue

            cmd_idx = (cmd_idx + 1) % len(commands)
            time.sleep(random.uniform(0.2, 0.8))
        else:
            time.sleep(0.050)


# ─────────────────────────────────────────────
#  Scenario: burst — rapid back-to-back packets
# ─────────────────────────────────────────────

BURST_ON = [
    (MSG_SCENE_LIGHTS,    STATE_ON,  NODE_SCENE,  "Scene Lights ON"),
    (MSG_FRONT_LIGHT_BAR, STATE_ON,  NODE_LIGHTS, "Front Light Bar ON"),
    (MSG_GRILL_LIGHTS,    STATE_ON,  NODE_LIGHTS, "Grill Lights ON"),
    (MSG_WARNING_FRONT,   STATE_ON,  NODE_LIGHTS, "Warning Front ON"),
]
BURST_OFF = [
    (MSG_SCENE_LIGHTS,    STATE_OFF, NODE_SCENE,  "Scene Lights OFF"),
    (MSG_FRONT_LIGHT_BAR, STATE_OFF, NODE_LIGHTS, "Front Light Bar OFF"),
    (MSG_GRILL_LIGHTS,    STATE_OFF, NODE_LIGHTS, "Grill Lights OFF"),
    (MSG_WARNING_FRONT,   STATE_OFF, NODE_LIGHTS, "Warning Front OFF"),
]


def burst_schedule(commands, lead_gap_ms: Optional[float] = None,
                   inter_msg_ms: float = VMUX_INTER_MSG_MS
                   ) -> List[Tuple[bytes, Optional[float], str]]:
    """
    Return [(packet, gap_before_ms, label), ...] for one burst.
    The first packet uses lead_gap_ms (None -> tx.min_gap_ms); the rest use
    inter_msg_ms, which is deliberately below the capture gap threshold.
    """
    return [(build_packet(code, state, node),
             lead_gap_ms if i == 0 else inter_msg_ms,
             label)
            for i, (code, state, node, label) in enumerate(commands)]


def scenario_burst(tx: BusTx, duration: float) -> None:
    """
    Back-to-back packets with intra-burst gaps BELOW the capture threshold.

    EXPECTED RESULT (capture --gap 10, generator defaults):
      Idle-gap framing cannot see message boundaries inside a burst, so each
      burst of 4 x 4-byte packets is captured as ONE 16-byte packet. The
      merge is correct behaviour of the framing, not a fault:
        ON  burst -> 20 01 02 23 11 01 03 13 12 01 03 10 13 01 03 11
        OFF burst -> 20 00 02 22 11 00 03 12 12 00 03 11 13 00 03 10
        gap_before of each merged packet >= tx.min_gap_ms (default 20 ms)
        SYNC is never sent inside a burst, so it never merges into one.
      FAIL if a burst is split (intra-burst gap exceeded the threshold: host
      jitter or receiver latency timer) or two bursts / a burst and a SYNC
      merge (idle flush or timestamp interpolation broken).
      Precondition: receiver FTDI latency timer = 1 ms. At the 16 ms default
      the result depends on USB batching and the run is not valid.

    Use the 'threshold' scenario to test behaviour near the gap boundary.
    """
    print(f"\n{ANSI_CYAN}Scenario: BURST — stress gap-framing for {duration:.0f}s{ANSI_RESET}")
    print(f"{ANSI_DIM}Intra-burst gap = {VMUX_INTER_MSG_MS}ms  (< {VMUX_IDLE_GAP_MS}ms threshold){ANSI_RESET}")
    print(f"{ANSI_DIM}Inter-burst gap = {tx.min_gap_ms}ms  (> {VMUX_IDLE_GAP_MS}ms threshold){ANSI_RESET}")
    print(f"{ANSI_DIM}Expected: each burst captured as ONE 16-byte packet (merge is correct){ANSI_RESET}\n")

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    burst_n = 0
    while time.time() < t_end:
        sync.tick()            # SYNC only between bursts, never inside one

        burst_n += 1
        print(f"{ANSI_DIM}  — burst #{burst_n} ON  (expect 1 x 16-byte packet) —{ANSI_RESET}")
        for pkt, gap, label in burst_schedule(BURST_ON):
            tx.send(pkt, label=label, gap_before_ms=gap)

        time.sleep(random.uniform(0.8, 1.5))   # idle flush in capture tool (F2)
        sync.tick()

        print(f"{ANSI_DIM}  — burst #{burst_n} OFF (expect 1 x 16-byte packet) —{ANSI_RESET}")
        for pkt, gap, label in burst_schedule(BURST_OFF):
            tx.send(pkt, label=label, gap_before_ms=gap)

        time.sleep(random.uniform(0.5, 1.2))


# ─────────────────────────────────────────────
#  Scenario: threshold — pairs straddling the gap threshold
# ─────────────────────────────────────────────

THRESHOLD_PAIR_SEPARATION_MS = 200.0   # between pairs; far above any threshold


def threshold_cases(threshold_ms: float = VMUX_IDLE_GAP_MS,
                    margin_ms: float = VMUX_THRESHOLD_MARGIN_MS) -> List[dict]:
    """
    Two packet pairs. 'above': second packet follows after threshold+margin,
    must be captured as 2 x 4-byte packets. 'below': threshold-margin, must
    merge into 1 x 8-byte packet. State 0x01/0x00 tags above/below so the
    cases are distinguishable in the capture log.
    """
    return [
        {"name": "above", "gap_ms": threshold_ms + margin_ms, "expect_packets": 2,
         "pkts": [build_packet(MSG_SCENE_LIGHTS, STATE_ON, NODE_SCENE),
                  build_packet(MSG_GRILL_LIGHTS, STATE_ON, NODE_LIGHTS)]},
        {"name": "below", "gap_ms": max(0.0, threshold_ms - margin_ms), "expect_packets": 1,
         "pkts": [build_packet(MSG_SCENE_LIGHTS, STATE_OFF, NODE_SCENE),
                  build_packet(MSG_GRILL_LIGHTS, STATE_OFF, NODE_LIGHTS)]},
    ]


def scenario_threshold(tx: BusTx, duration: float,
                       threshold_ms: float = VMUX_IDLE_GAP_MS,
                       margin_ms: float = VMUX_THRESHOLD_MARGIN_MS) -> None:
    """
    Alternate packet pairs separated by threshold+margin and threshold-margin.

    EXPECTED RESULT (capture tool --gap must equal threshold_ms):
      'above' pair (state 01): 2 packets  20 01 02 23 | 12 01 03 10
                               second packet gap_before ~ threshold+margin
      'below' pair (state 00): 1 packet   20 00 02 22 12 00 03 11
      Pairs are >= 200 ms apart and never merge with each other.
    On hardware this is statistical: the real wire gap is the requested gap
    plus ~0-2 ms of host/USB jitter, and the capture tool measures
    arrival-to-arrival (includes one byte time). Report split rate per case.
    Margins below ~3 ms are not meaningful. Receiver latency timer = 1 ms.
    """
    cases = threshold_cases(threshold_ms, margin_ms)
    print(f"\n{ANSI_CYAN}Scenario: THRESHOLD — gap {threshold_ms}±{margin_ms}ms "
          f"for {duration:.0f}s{ANSI_RESET}")
    for c in cases:
        print(f"{ANSI_DIM}  {c['name']:5s}: gap {c['gap_ms']:.1f}ms -> expect "
              f"{c['expect_packets']} packet(s){ANSI_RESET}")
    print()

    sync  = SyncScheduler(tx)
    t_end = time.time() + duration

    tx.send(sync_packet(), label="SYNC (Node 1) — initial", gap_before_ms=200)
    sync._next_t = time.time() + VMUX_SYNC_INTERVAL_S

    counts = {c["name"]: 0 for c in cases}
    while time.time() < t_end:
        for c in cases:
            sync.tick()
            first, second = c["pkts"]
            tx.send(first, label=f"{c['name']} #1",
                    gap_before_ms=THRESHOLD_PAIR_SEPARATION_MS)
            tx.send(second, label=f"{c['name']} #2 (gap {c['gap_ms']:.1f}ms)",
                    gap_before_ms=c["gap_ms"])
            counts[c["name"]] += 1

    print(f"\n{ANSI_DIM}  Pairs sent: above={counts['above']} below={counts['below']} "
          f"(expect {2 * counts['above']} x 4-byte + {counts['below']} x 8-byte, "
          f"plus SYNC){ANSI_RESET}")


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
                    label=f"{name} {state_str} (node {node})")

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
                label=f"{name} {state_str}")
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
    print(f"  BURST: each 4-packet burst captured as ONE 16-byte packet")
    print(f"  THRESHOLD: 'above' pairs split (2 x 4B), 'below' pairs merge (1 x 8B)\n")

    steps = [
        ("IDLE",      20,  scenario_idle),
        ("BASIC",     60,  scenario_basic),
        ("BURST",     30,  scenario_burst),
        ("THRESHOLD", 20,  scenario_threshold),
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
            tx.send(sync_packet(), label="SYNC (manual)")

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
                tx.send(pkt, label=f"{name} {'ON' if state else 'OFF'} (node {node})")

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
  burst        Rapid packets — each burst must merge into one 16-byte packet
  threshold    Pairs at threshold±margin — 'above' must split, 'below' must merge
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
  python vmux_generator.py --port COM4 --baud 19200 --scenario threshold --threshold 10 --margin 3

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
                        choices=["idle","basic","burst","threshold","multinode",
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
    parser.add_argument("--threshold", type=float, default=VMUX_IDLE_GAP_MS,
                        help=f"threshold scenario: capture tool --gap value in ms "
                             f"(default: {VMUX_IDLE_GAP_MS})")
    parser.add_argument("--margin",    type=float, default=VMUX_THRESHOLD_MARGIN_MS,
                        help=f"threshold scenario: ± margin around threshold in ms "
                             f"(default: {VMUX_THRESHOLD_MARGIN_MS})")

    args = parser.parse_args()

    if args.scan:
        print_ports()
        return

    if not args.port:
        parser.print_help()
        print(f"\n{ANSI_RED}Error: --port required. Use --scan to list ports.{ANSI_RESET}\n")
        sys.exit(1)

    print(f"\n{ANSI_BOLD}V-MUX Generator{ANSI_RESET}")
    print(f"  Port:     {args.port}")
    print(f"  Baud:     {args.baud:,}")
    print(f"  Scenario: {args.scenario}")
    print(f"  Gap:      {args.gap}ms min inter-packet")
    print(f"  Manual DE:{args.manual_de}")

    tx = BusTx(port=args.port, baud=args.baud,
               manual_de=args.manual_de, quiet=args.quiet,
               min_gap_ms=args.gap)

    default_durations = {
        "idle":      30.0,
        "basic":     90.0,
        "burst":     45.0,
        "threshold": 45.0,
        "multinode": 60.0,
        "unknown":   45.0,
    }

    try:
        if args.scenario == "full":
            scenario_full(tx)

        elif args.scenario == "interactive":
            scenario_interactive(tx)

        elif args.scenario == "threshold":
            scenario_threshold(tx, args.duration or default_durations["threshold"],
                               threshold_ms=args.threshold, margin_ms=args.margin)

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
