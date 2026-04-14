#!/usr/bin/env python3
"""
vmux_capture.py — V-MUX RS-485 bus capture and analysis tool  v2
DSD TECH SH-U11F adapter · Braun ambulance 2013 · Phase 1 bus analysis

Changes from v1:
  - scan_ports(): fixed FTDI VID detection operator-precedence bug
  - detect_baud(): removed dead framing_errors variable; added Phase 3 note
  - capture(): SyncDetector intervals now propagated to CaptureStats on every
               SYNC event so Display.summary() and status_bar() show live data
  - Display.status_bar(): uses ANSI cursor save/restore to avoid overwriting
               packet output lines
  - CaptureLogger.log(): binary format extended to 64-bit epoch_ms (no wrap);
               32-bit session-relative ts_ms retained for backward compat
  - README: added SH-U11F termination jumper location note

Usage:
    python vmux_capture.py --port COM3              # Windows
    python vmux_capture.py --port /dev/ttyUSB0      # Linux/Mac
    python vmux_capture.py --scan                   # List available ports
    python vmux_capture.py --port COM3 --baud 19200 # Force specific baud
    python vmux_capture.py --port COM3 --detect     # Auto-detect baud rate
"""

import serial
import serial.tools.list_ports
import argparse
import time
import sys
import os
import csv
import struct
from datetime import datetime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
#  V-MUX protocol constants (from Weldon docs)
# ─────────────────────────────────────────────

VMUX_SYNC_INTERVAL_S   = 4.0    # Node 1 SYNC fires every ~4 seconds
VMUX_IDLE_GAP_MS       = 10     # Treat gap >10ms between bytes as packet boundary
VMUX_CANDIDATE_BAUDS   = [9600, 19200, 38400, 57600, 115200]
VMUX_MAX_PACKET_BYTES  = 64     # Safety ceiling; real packets are typically 4-20 bytes
VMUX_KNOWN_COMMANDS    = {
    # Populated from Weldon .dav database references where known.
    # Format:  code (int or bytes) : human label
    # These are illustrative placeholders — update from your captured .dav file.
    0x04: "Reverse",
    0x01: "Forward",
    0x02: "Park",
    0x10: "Emergency Master",
    0x11: "Front Light Bar",
    0x12: "Grill Lights",
    0x13: "Warning Lights Front",
    0x14: "Warning Lights Rear",
    0x20: "Scene Lights",
    0x21: "Compartment Light 1",
    0x22: "Compartment Light 2",
    0x30: "Siren",
    0x40: "Door Cab Left",
    0x41: "Door Cab Right",
    0x50: "Sync",          # Node 1 synchronisation broadcast
    0xFF: "Ping/Reply",
}

ANSI_GREEN  = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED    = "\033[91m"
ANSI_CYAN   = "\033[96m"
ANSI_MAGENTA= "\033[95m"
ANSI_RESET  = "\033[0m"
ANSI_BOLD   = "\033[1m"
ANSI_DIM    = "\033[2m"


# ─────────────────────────────────────────────
#  Data structures
# ─────────────────────────────────────────────

@dataclass
class VmuxPacket:
    timestamp: float
    raw_bytes: bytes
    gap_before_ms: float        # idle gap before first byte of this packet
    baud_rate: int

    @property
    def hex_str(self) -> str:
        return " ".join(f"{b:02X}" for b in self.raw_bytes)

    @property
    def length(self) -> int:
        return len(self.raw_bytes)

    @property
    def timestamp_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S.%f")[:-3]

    def decode_attempt(self) -> str:
        """
        Best-effort human decode of a V-MUX packet.
        V-MUX packets appear to carry: [msg_code][state_byte][node_data][...][checksum?]
        Exact framing is confirmed during Phase 1 capture — this is a starting hypothesis.
        """
        if not self.raw_bytes:
            return "(empty)"
        if len(self.raw_bytes) == 1:
            return f"single byte: 0x{self.raw_bytes[0]:02X}"

        msg_code = self.raw_bytes[0]
        label = VMUX_KNOWN_COMMANDS.get(msg_code, f"UNKNOWN_0x{msg_code:02X}")

        state_str = ""
        if len(self.raw_bytes) >= 2:
            state_byte = self.raw_bytes[1]
            if state_byte == 0x01:
                state_str = " ON"
            elif state_byte == 0x00:
                state_str = " OFF"
            else:
                state_str = f" STATE=0x{state_byte:02X}"

        node_str = ""
        if len(self.raw_bytes) >= 3:
            node_str = f" NODE={self.raw_bytes[2]}"

        tail = ""
        if len(self.raw_bytes) > 3:
            tail = " [" + " ".join(f"{b:02X}" for b in self.raw_bytes[3:]) + "]"

        return f"{label}{state_str}{node_str}{tail}"


@dataclass
class CaptureStats:
    start_time: float = field(default_factory=time.time)
    packet_count: int = 0
    byte_count: int = 0
    error_count: int = 0
    last_sync_time: Optional[float] = None
    sync_intervals: list = field(default_factory=list)
    message_counts: dict = field(default_factory=lambda: defaultdict(int))
    unique_messages: set = field(default_factory=set)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.start_time

    @property
    def avg_sync_interval(self) -> Optional[float]:
        if len(self.sync_intervals) < 2:
            return None
        return sum(self.sync_intervals) / len(self.sync_intervals)


# ─────────────────────────────────────────────
#  Port discovery
# ─────────────────────────────────────────────

def scan_ports() -> list:
    """List all serial ports, highlighting FTDI devices (DSD TECH SH-U11F)."""
    ports = serial.tools.list_ports.comports()
    results = []
    for p in sorted(ports, key=lambda x: x.device):
        # USB VID 0x0403 = FTDI; also match by manufacturer / description strings
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
        marker = f"{ANSI_GREEN}← FTDI / likely SH-U11F{ANSI_RESET}" if is_ftdi else ""
        print(f"{device:<16} {desc:<40} {marker}")
    print()


# ─────────────────────────────────────────────
#  Baud rate auto-detection
# ─────────────────────────────────────────────

def detect_baud(port: str, timeout_per_baud: float = 6.0) -> Optional[int]:
    """
    Try each candidate baud rate and score based on:
      - Framing errors (fewer = better)
      - Byte variance (random noise has flat distribution; real UART is non-uniform)
      - SYNC packet detection (~4s period of repeated identical packet)

    Returns the best candidate baud rate or None if inconclusive.
    """
    print(f"\n{ANSI_CYAN}{ANSI_BOLD}Baud rate auto-detection{ANSI_RESET}")
    print(f"Testing {len(VMUX_CANDIDATE_BAUDS)} baud rates, {timeout_per_baud}s each...\n")

    scores = {}

    for baud in VMUX_CANDIDATE_BAUDS:
        print(f"  Testing {baud:>7,} baud ... ", end="", flush=True)
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            ser.reset_input_buffer()

            raw = bytearray()
            # NOTE: pyserial does not expose per-byte framing errors via read().
            # Framing quality is inferred from byte distribution and sequence
            # scoring below. For Phase 3 active injection a dedicated read thread
            # with select()-based I/O will be required to meet <1µs DE turnaround.
            t_end = time.time() + timeout_per_baud

            while time.time() < t_end:
                chunk = ser.read(64)
                if chunk:
                    raw.extend(chunk)

            ser.close()

            if len(raw) < 4:
                print(f"{ANSI_DIM}no data{ANSI_RESET}")
                scores[baud] = -1
                continue

            # Score 1: byte value distribution (real RS-485 UART is non-uniform)
            counts = [0] * 256
            for b in raw:
                counts[b] += 1
            non_zero = sum(1 for c in counts if c > 0)
            uniformity_penalty = abs(non_zero - 128) / 128.0  # 0=uniform(noise), 1=sparse(real)

            # Score 2: look for repeated byte sequences (SYNC packet pattern)
            repeated = _find_repeated_sequences(bytes(raw))

            score = (repeated * 2.0) + (1.0 - uniformity_penalty) * 0.5
            scores[baud] = score

            verdict = (f"{ANSI_GREEN}candidate{ANSI_RESET}" if score > 1.0
                       else f"{ANSI_DIM}unlikely{ANSI_RESET}")
            print(f"{len(raw):4d} bytes  score={score:.2f}  {verdict}")

        except serial.SerialException as e:
            print(f"{ANSI_RED}error: {e}{ANSI_RESET}")
            scores[baud] = -1

    best = max(scores, key=lambda b: scores[b])
    best_score = scores[best]

    if best_score <= 0:
        print(f"\n{ANSI_RED}Could not determine baud rate automatically.{ANSI_RESET}")
        print("Ensure the vehicle is powered and the V-MUX system is active.")
        return None

    print(f"\n{ANSI_GREEN}{ANSI_BOLD}Best candidate: {best:,} baud (score={best_score:.2f}){ANSI_RESET}")
    print("Verify by checking SYNC packet period (~4 seconds) in capture mode.\n")
    return best


def _find_repeated_sequences(data: bytes, min_len: int = 3, min_count: int = 2) -> int:
    """Count how many short byte sequences repeat — a heuristic for structured UART."""
    found = 0
    for length in range(min_len, min(8, len(data) // 4)):
        seen = defaultdict(int)
        for i in range(len(data) - length):
            seq = data[i:i+length]
            seen[seq] += 1
        found += sum(1 for c in seen.values() if c >= min_count)
    return found


# ─────────────────────────────────────────────
#  Packet reassembly
# ─────────────────────────────────────────────

class PacketAssembler:
    """
    Reassembles raw bytes into V-MUX packets using idle-gap framing.
    V-MUX does not use length-prefix framing or explicit delimiters in the
    known documentation — packet boundaries are defined by inter-message gaps.
    The gap threshold (VMUX_IDLE_GAP_MS) may need tuning after observing real traffic.
    """

    def __init__(self, gap_threshold_ms: float = VMUX_IDLE_GAP_MS, baud: int = 19200):
        self.gap_threshold_ms = gap_threshold_ms
        self.baud = baud
        self._buffer: bytearray = bytearray()
        self._last_byte_time: Optional[float] = None
        self._packet_start_time: Optional[float] = None
        self._gap_before: float = 0.0

    def feed(self, byte: int, arrival_time: float) -> Optional[VmuxPacket]:
        """
        Feed one byte. Returns a completed VmuxPacket if a gap was detected
        before this byte (i.e., the previous packet just ended).
        """
        completed = None

        if self._last_byte_time is not None:
            gap_ms = (arrival_time - self._last_byte_time) * 1000.0

            if gap_ms >= self.gap_threshold_ms and self._buffer:
                # Gap detected — flush current buffer as a completed packet
                completed = VmuxPacket(
                    timestamp=self._packet_start_time,
                    raw_bytes=bytes(self._buffer),
                    gap_before_ms=self._gap_before,
                    baud_rate=self.baud,
                )
                self._buffer = bytearray()
                self._gap_before = gap_ms
                self._packet_start_time = arrival_time

        if not self._buffer:
            self._packet_start_time = arrival_time

        self._buffer.append(byte)
        self._last_byte_time = arrival_time

        return completed

    def flush(self) -> Optional[VmuxPacket]:
        """Force-flush any buffered bytes as a final packet (call on exit)."""
        if self._buffer:
            pkt = VmuxPacket(
                timestamp=self._packet_start_time or time.time(),
                raw_bytes=bytes(self._buffer),
                gap_before_ms=self._gap_before,
                baud_rate=self.baud,
            )
            self._buffer = bytearray()
            return pkt
        return None


# ─────────────────────────────────────────────
#  Display
# ─────────────────────────────────────────────

class Display:
    """Terminal output with colour coding by packet type."""

    def __init__(self, verbose: bool = False, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self._line_count = 0

    def header(self, port: str, baud: int):
        print(f"\n{ANSI_BOLD}{'═' * 72}{ANSI_RESET}")
        print(f"{ANSI_BOLD}  V-MUX Bus Capture{ANSI_RESET}  "
              f"port={ANSI_CYAN}{port}{ANSI_RESET}  "
              f"baud={ANSI_CYAN}{baud:,}{ANSI_RESET}  "
              f"gap={VMUX_IDLE_GAP_MS}ms")
        print(f"{ANSI_BOLD}{'═' * 72}{ANSI_RESET}")
        print(f"{'Timestamp':<14} {'#':<5} {'Bytes':<5} {'Raw hex':<30} Decoded")
        print("─" * 100)

    def packet(self, pkt: VmuxPacket, pkt_num: int, stats: CaptureStats):
        if self.quiet:
            return

        decoded = pkt.decode_attempt()
        raw = pkt.hex_str

        # Colour by first byte / type
        if not pkt.raw_bytes:
            return
        b0 = pkt.raw_bytes[0]

        if b0 == 0x50 or (len(pkt.raw_bytes) >= 2 and pkt.raw_bytes[0:2] == b'\x50\x00'):
            colour = ANSI_MAGENTA   # SYNC — magenta
        elif b0 in VMUX_KNOWN_COMMANDS:
            colour = ANSI_GREEN     # Known command — green
        elif b0 == 0xFF:
            colour = ANSI_DIM       # Ping/reply — dim
        else:
            colour = ANSI_YELLOW    # Unknown — yellow (interesting!)

        gap_str = f"(+{pkt.gap_before_ms:.0f}ms)" if pkt.gap_before_ms > 50 else ""

        print(f"{colour}{pkt.timestamp_str:<14} "
              f"{pkt_num:<5} "
              f"{pkt.length:<5} "
              f"{raw:<30} "
              f"{decoded}{ANSI_RESET} "
              f"{ANSI_DIM}{gap_str}{ANSI_RESET}")

        self._line_count += 1

        if self.verbose and pkt.length > 1:
            # Print byte-by-byte breakdown
            for i, b in enumerate(pkt.raw_bytes):
                role = _byte_role(i, pkt.length)
                print(f"  {ANSI_DIM}  [{i}] 0x{b:02X} = {b:3d}  {role}{ANSI_RESET}")

    def status_bar(self, stats: CaptureStats, baud: int):
        """
        Print a persistent status line without overwriting packet output.
        Uses ANSI save/restore cursor: ESC[s saves position, ESC[u restores.
        The status line is printed at the current cursor position then the
        cursor is restored, so subsequent packet lines appear above it.
        """
        sync_str = (f"SYNC_avg={stats.avg_sync_interval:.1f}s"
                    if stats.avg_sync_interval else "awaiting SYNC...")

        line = (f"\033[s"                           # save cursor position
                f"\r{ANSI_DIM}"
                f"  elapsed={stats.elapsed_s:.0f}s  "
                f"pkts={stats.packet_count}  "
                f"bytes={stats.byte_count}  "
                f"unique_msgs={len(stats.unique_messages)}  "
                f"errs={stats.error_count}  "
                f"{sync_str}        "
                f"{ANSI_RESET}"
                f"\033[u")                          # restore cursor position
        print(line, end="", flush=True)

    def summary(self, stats: CaptureStats):
        print(f"\n\n{ANSI_BOLD}{'─' * 72}{ANSI_RESET}")
        print(f"{ANSI_BOLD}Capture summary{ANSI_RESET}")
        print(f"  Duration:         {stats.elapsed_s:.1f} s")
        print(f"  Total packets:    {stats.packet_count}")
        print(f"  Total bytes:      {stats.byte_count}")
        print(f"  Unique msg codes: {len(stats.unique_messages)}")
        print(f"  Errors:           {stats.error_count}")
        if stats.avg_sync_interval:
            print(f"  SYNC interval:    {stats.avg_sync_interval:.2f} s  "
                  f"(target ~4.0 s — {'OK' if 3.0 < stats.avg_sync_interval < 5.0 else 'CHECK'})")
        print(f"\n{ANSI_BOLD}Message frequency table:{ANSI_RESET}")
        print(f"  {'Code':<8} {'Count':<8} {'Label'}")
        print("  " + "─" * 50)
        for code, count in sorted(stats.message_counts.items(),
                                   key=lambda x: -x[1]):
            label = VMUX_KNOWN_COMMANDS.get(code, "unknown")
            print(f"  0x{code:02X}    {count:<8} {label}")


def _byte_role(index: int, total: int) -> str:
    """Return a hypothesis about the role of byte at position index."""
    if index == 0:
        return "→ message code"
    if index == 1:
        return "→ state (0x00=OFF, 0x01=ON?)"
    if index == 2:
        return "→ node number?"
    if index == total - 1:
        return "→ checksum / stop?"
    return "→ data"


# ─────────────────────────────────────────────
#  CSV / log file output
# ─────────────────────────────────────────────

class CaptureLogger:
    """Writes all packets to a timestamped CSV and raw binary log."""

    def __init__(self, output_dir: str = "."):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path  = os.path.join(output_dir, f"vmux_capture_{ts}.csv")
        self.raw_path  = os.path.join(output_dir, f"vmux_capture_{ts}.bin")
        self._csv_file  = open(self.csv_path,  "w", newline="")
        self._raw_file  = open(self.raw_path,  "wb")
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow([
            "timestamp_s", "timestamp_str", "pkt_num", "baud",
            "length", "gap_before_ms", "raw_hex", "decoded",
            "msg_code_hex", "state_byte_hex", "node_byte"
        ])
        self._pkt_count = 0

    def log(self, pkt: VmuxPacket, pkt_num: int):
        self._pkt_count += 1

        # CSV row
        msg_code = f"0x{pkt.raw_bytes[0]:02X}" if pkt.raw_bytes else ""
        state_b  = f"0x{pkt.raw_bytes[1]:02X}" if len(pkt.raw_bytes) > 1 else ""
        node_b   = pkt.raw_bytes[2] if len(pkt.raw_bytes) > 2 else ""

        self._writer.writerow([
            f"{pkt.timestamp:.6f}",
            pkt.timestamp_str,
            pkt_num,
            pkt.baud_rate,
            pkt.length,
            f"{pkt.gap_before_ms:.2f}",
            pkt.hex_str,
            pkt.decode_attempt(),
            msg_code,
            state_b,
            node_b,
        ])
        self._csv_file.flush()

        # Binary record format (12 + N bytes per packet):
        #   [8-byte epoch_ms  int64  big-endian]  — full Unix ms, no wrap
        #   [4-byte ts_ms     uint32 big-endian]  — session-relative ms (wraps at ~49.7 days)
        #   [1-byte length    uint8             ]  — payload byte count
        #   [N-byte payload                     ]
        # The 32-bit ts_ms is kept for backward compatibility. Use epoch_ms for
        # cross-session analysis. struct '>qIB' = big-endian int64 + uint32 + uint8.
        epoch_ms = int(pkt.timestamp * 1000)
        ts_ms    = epoch_ms & 0xFFFFFFFF
        self._raw_file.write(struct.pack(">qIB", epoch_ms, ts_ms, pkt.length))
        self._raw_file.write(pkt.raw_bytes)
        self._raw_file.flush()

    def close(self):
        self._csv_file.close()
        self._raw_file.close()

    def paths(self) -> tuple:
        return self.csv_path, self.raw_path


# ─────────────────────────────────────────────
#  SYNC detection / baud verification
# ─────────────────────────────────────────────

class SyncDetector:
    """
    Watches for the V-MUX SYNC message from Node 1.
    SYNC fires every ~4 seconds and is the most regular, identifiable
    packet on the bus. Detecting it confirms baud rate and bus health.
    """

    def __init__(self, window: int = 10):
        self._recent: deque = deque(maxlen=window)
        self._intervals: list = []
        self._last_sync_time: Optional[float] = None
        self.confirmed: bool = False
        self.sync_packet: Optional[bytes] = None

    def feed(self, pkt: VmuxPacket) -> bool:
        """
        Returns True if this packet looks like a SYNC.
        Heuristics: short packet (2-6 bytes), repeating at ~4s,
        first byte 0x50 or consistent unknown code.
        """
        is_sync = False

        # Hypothesis 1: known SYNC code
        if pkt.raw_bytes and pkt.raw_bytes[0] == 0x50:
            is_sync = True

        # Hypothesis 2: short packet repeating at ~4s interval
        if not is_sync and len(pkt.raw_bytes) in (2, 3, 4):
            self._recent.append((pkt.timestamp, bytes(pkt.raw_bytes)))
            # Look for the same byte pattern appearing repeatedly
            if len(self._recent) >= 3:
                patterns = [r[1] for r in self._recent]
                most_common = max(set(patterns), key=patterns.count)
                if patterns.count(most_common) >= 3:
                    is_sync = True
                    self.sync_packet = most_common

        if is_sync:
            now = pkt.timestamp
            if self._last_sync_time is not None:
                interval = now - self._last_sync_time
                if 2.0 < interval < 8.0:  # Reasonable SYNC window
                    self._intervals.append(interval)
                    if len(self._intervals) >= 2:
                        self.confirmed = True
            self._last_sync_time = now

        return is_sync

    @property
    def avg_interval(self) -> Optional[float]:
        if self._intervals:
            return sum(self._intervals) / len(self._intervals)
        return None

    def baud_verdict(self) -> str:
        if not self.confirmed:
            return "unconfirmed"
        avg = self.avg_interval
        if avg and 3.5 < avg < 4.5:
            return f"CONFIRMED OK (SYNC avg={avg:.2f}s)"
        elif avg:
            return f"WARNING: SYNC interval {avg:.2f}s (expected ~4.0s) — check baud rate"
        return "unconfirmed"


# ─────────────────────────────────────────────
#  Main capture loop
# ─────────────────────────────────────────────

def capture(port: str, baud: int, duration: Optional[float],
            output_dir: str, verbose: bool, quiet: bool):

    display = Display(verbose=verbose, quiet=quiet)
    logger  = CaptureLogger(output_dir=output_dir)
    stats   = CaptureStats()
    assembler = PacketAssembler(gap_threshold_ms=VMUX_IDLE_GAP_MS, baud=baud)
    sync_detector = SyncDetector()

    print(f"\n{ANSI_CYAN}Logging to:{ANSI_RESET}")
    csv_path, raw_path = logger.paths()
    print(f"  CSV: {csv_path}")
    print(f"  BIN: {raw_path}")

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,             # Short timeout for responsive gap detection
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
    except serial.SerialException as e:
        print(f"\n{ANSI_RED}Failed to open {port}: {e}{ANSI_RESET}")
        print("Check the port name and that no other application is using it.")
        sys.exit(1)

    # SH-U11F: disable RTS (don't accidentally drive DE high)
    ser.rts = False
    ser.dtr = False
    ser.reset_input_buffer()

    display.header(port, baud)

    pkt_num = 0
    t_start = time.time()
    t_last_status = t_start
    status_interval = 1.0  # seconds between status bar refresh

    print(f"\n{ANSI_YELLOW}Waiting for bus activity...  (Ctrl+C to stop){ANSI_RESET}\n")

    try:
        while True:
            # Check duration
            elapsed = time.time() - t_start
            if duration and elapsed >= duration:
                break

            # Read available bytes
            waiting = ser.in_waiting
            if waiting:
                raw = ser.read(min(waiting, 256))
                t_rx = time.time()

                for b in raw:
                    stats.byte_count += 1
                    completed = assembler.feed(b, t_rx)

                    if completed:
                        pkt_num += 1
                        stats.packet_count += 1

                        # Update stats
                        if completed.raw_bytes:
                            code = completed.raw_bytes[0]
                            stats.message_counts[code] += 1
                            stats.unique_messages.add(code)

                        # SYNC detection
                        is_sync = sync_detector.feed(completed)
                        if is_sync:
                            # Keep CaptureStats in sync with SyncDetector so
                            # Display.summary() and status_bar() reflect live data.
                            stats.sync_intervals = list(sync_detector._intervals)
                            stats.last_sync_time = sync_detector._last_sync_time
                        if is_sync and not sync_detector.confirmed:
                            print(f"\n{ANSI_MAGENTA}  SYNC detected — verifying baud rate...{ANSI_RESET}")
                        elif is_sync and sync_detector.confirmed:
                            verdict = sync_detector.baud_verdict()
                            if "OK" in verdict and pkt_num == 2:
                                print(f"\n{ANSI_GREEN}  Baud rate {verdict}{ANSI_RESET}\n")

                        # Display and log
                        display.packet(completed, pkt_num, stats)
                        logger.log(completed, pkt_num)

            else:
                time.sleep(0.001)  # Yield CPU when idle

            # Status bar refresh
            if time.time() - t_last_status >= status_interval:
                if not quiet:
                    display.status_bar(stats, baud)
                t_last_status = time.time()

    except KeyboardInterrupt:
        print(f"\n\n{ANSI_YELLOW}Capture stopped by user.{ANSI_RESET}")

    finally:
        # Flush any partial packet
        final_pkt = assembler.flush()
        if final_pkt and final_pkt.raw_bytes:
            pkt_num += 1
            stats.packet_count += 1
            display.packet(final_pkt, pkt_num, stats)
            logger.log(final_pkt, pkt_num)

        ser.close()
        logger.close()
        display.summary(stats)

        # Final baud verdict
        verdict = sync_detector.baud_verdict()
        print(f"\n{ANSI_CYAN}Baud rate verdict:{ANSI_RESET} {verdict}")
        print(f"\n{ANSI_GREEN}Files saved:{ANSI_RESET}")
        print(f"  {csv_path}")
        print(f"  {raw_path}\n")


# ─────────────────────────────────────────────
#  Message map builder (post-capture analysis)
# ─────────────────────────────────────────────

def build_message_map(csv_path: str):
    """
    Read a capture CSV and produce a deduplicated message map:
    msg_code → {count, states_seen, example_packets}.
    Prints a table for manual annotation.
    """
    if not os.path.exists(csv_path):
        print(f"{ANSI_RED}File not found: {csv_path}{ANSI_RESET}")
        return

    messages = defaultdict(lambda: {"count": 0, "states": set(), "examples": []})

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code_str = row.get("msg_code_hex", "")
            if not code_str:
                continue
            state = row.get("state_byte_hex", "")
            raw   = row.get("raw_hex", "")
            messages[code_str]["count"] += 1
            messages[code_str]["states"].add(state)
            if len(messages[code_str]["examples"]) < 3:
                messages[code_str]["examples"].append(raw)

    print(f"\n{ANSI_BOLD}Message map from: {csv_path}{ANSI_RESET}")
    print(f"{'Code':<10} {'Count':<8} {'States seen':<24} {'Known label':<24} Example hex")
    print("─" * 100)

    for code_str, data in sorted(messages.items(), key=lambda x: -x[1]["count"]):
        try:
            code_int = int(code_str, 16)
        except ValueError:
            continue
        label  = VMUX_KNOWN_COMMANDS.get(code_int, "— annotate me —")
        states = ", ".join(sorted(data["states"]))
        ex     = data["examples"][0] if data["examples"] else ""
        print(f"{code_str:<10} {data['count']:<8} {states:<24} {label:<24} {ex}")

    print(f"\nTotal unique message codes: {len(messages)}")
    print("Update VMUX_KNOWN_COMMANDS in vmux_capture.py with your annotations.\n")


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main():
    global VMUX_IDLE_GAP_MS  # noqa: PLW0603 — may be overridden by --gap

    parser = argparse.ArgumentParser(
        description="V-MUX RS-485 bus capture tool — DSD TECH SH-U11F",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vmux_capture.py --scan
  python vmux_capture.py --port COM3 --detect
  python vmux_capture.py --port /dev/ttyUSB0 --baud 19200
  python vmux_capture.py --port COM3 --baud 19200 --duration 60
  python vmux_capture.py --port COM3 --baud 19200 --verbose
  python vmux_capture.py --map vmux_capture_20240101_120000.csv
        """
    )
    parser.add_argument("--port",     help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--baud",     type=int, default=None,
                        help="Baud rate (omit to use --detect)")
    parser.add_argument("--scan",     action="store_true",
                        help="List available serial ports and exit")
    parser.add_argument("--detect",   action="store_true",
                        help="Auto-detect baud rate before capturing")
    parser.add_argument("--duration", type=float, default=None,
                        help="Capture duration in seconds (default: run until Ctrl+C)")
    parser.add_argument("--output",   default=".",
                        help="Output directory for log files (default: current dir)")
    parser.add_argument("--verbose",  action="store_true",
                        help="Show per-byte breakdown of each packet")
    parser.add_argument("--quiet",    action="store_true",
                        help="Suppress per-packet display (log only)")
    parser.add_argument("--gap",      type=float, default=VMUX_IDLE_GAP_MS,
                        help=f"Idle gap (ms) for packet boundary detection "
                             f"(default: {VMUX_IDLE_GAP_MS})")
    parser.add_argument("--map",      metavar="CSV",
                        help="Analyse a previous capture CSV and print message map")

    args = parser.parse_args()

    # Override gap threshold if user specified --gap
    VMUX_IDLE_GAP_MS = args.gap

    # Message map analysis mode
    if args.map:
        build_message_map(args.map)
        return

    # Port scan mode
    if args.scan:
        print_ports()
        return

    # Require port for capture
    if not args.port:
        parser.print_help()
        print(f"\n{ANSI_RED}Error: --port is required for capture. "
              f"Use --scan to list ports.{ANSI_RESET}\n")
        sys.exit(1)

    # Baud rate
    baud = args.baud
    if baud is None and not args.detect:
        print(f"{ANSI_YELLOW}No baud rate specified. Starting auto-detect...{ANSI_RESET}")
        args.detect = True

    if args.detect:
        baud = detect_baud(args.port)
        if baud is None:
            print(f"{ANSI_RED}Auto-detect failed. Specify --baud manually.{ANSI_RESET}")
            sys.exit(1)

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Run capture
    capture(
        port=args.port,
        baud=baud,
        duration=args.duration,
        output_dir=args.output,
        verbose=args.verbose,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
