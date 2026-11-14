import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def byte_time_s(baud: int) -> float:
    """8N1: 10 bit times per byte."""
    return 10.0 / baud


def simulate_wire(schedule, baud=19200, t0=1000.0):
    """
    Convert [(packet_bytes, idle_gap_ms_before), ...] into
    [(byte, arrival_time_s), ...] the way a UART receiver sees it:
    each byte 'arrives' at the end of its stop bit, bytes within a packet
    are back-to-back, and idle_gap_ms is line-idle time before the
    packet's first start bit.
    """
    bt = byte_time_s(baud)
    t = t0
    out = []
    for pkt, gap_ms in schedule:
        t += gap_ms / 1000.0
        for b in pkt:
            t += bt
            out.append((b, t))
    return out


def assemble(stream, gap_ms=10, baud=19200):
    """Run a byte stream through PacketAssembler; return all packets incl. final flush."""
    from vmux_capture import PacketAssembler
    pa = PacketAssembler(gap_threshold_ms=gap_ms, baud=baud)
    pkts = []
    for b, t in stream:
        p = pa.feed(b, t)
        if p is not None:
            pkts.append(p)
    last = pa.flush()
    if last is not None:
        pkts.append(last)
    return pkts


@pytest.fixture
def no_sleep(monkeypatch):
    """Record time.sleep calls in both tools instead of sleeping."""
    calls = []
    import vmux_generator
    monkeypatch.setattr(vmux_generator.time, "sleep", lambda s: calls.append(s))
    return calls
