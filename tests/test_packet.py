"""build_packet / checksum (generator) and PacketAssembler (capture)."""
import pytest

import vmux_generator as gen
from vmux_capture import PacketAssembler
from conftest import simulate_wire, assemble, byte_time_s


# ── checksum / build_packet ───────────────────────────────────────────────

@pytest.mark.parametrize("code,state,node,expected", [
    (0x20, 0x01, 0x02, bytes([0x20, 0x01, 0x02, 0x23])),   # Scene Lights ON node 2
    (0x50, 0x00, 0x01, bytes([0x50, 0x00, 0x01, 0x51])),   # SYNC node 1
    (0xFF, 0xFF, 0xFF, bytes([0xFF, 0xFF, 0xFF, 0xFF])),
    (0x00, 0x00, 0x00, bytes([0x00, 0x00, 0x00, 0x00])),
])
def test_build_packet_known_vectors(code, state, node, expected):
    assert gen.build_packet(code, state, node) == expected


def test_checksum_xor_of_all_bytes_is_zero():
    for code in range(256):
        pkt = gen.build_packet(code, 0x01, 0x03)
        assert len(pkt) == gen.PACKET_LEN
        assert pkt[0] ^ pkt[1] ^ pkt[2] ^ pkt[3] == 0
        assert pkt[gen.IDX_CHECKSUM] == gen.checksum(code, 0x01, 0x03)


def test_sync_packet():
    assert gen.sync_packet() == bytes([0x50, 0x00, 0x01, 0x51])


# ── PacketAssembler basics ───────────────────────────────────────────────

def test_single_packet_needs_flush():
    pa = PacketAssembler(gap_threshold_ms=10, baud=19200)
    for b, t in simulate_wire([(b"\x20\x01\x02\x23", 100)]):
        assert pa.feed(b, t) is None
    p = pa.flush()
    assert p.raw_bytes == b"\x20\x01\x02\x23"
    assert p.baud_rate == 19200
    assert pa.flush() is None


def test_flush_empty_returns_none():
    assert PacketAssembler().flush() is None


def test_two_packets_split_on_gap():
    a, b = gen.build_packet(0x20, 1, 2), gen.sync_packet()
    pkts = assemble(simulate_wire([(a, 100), (b, 40)]))
    assert [p.raw_bytes for p in pkts] == [a, b]
    # gap_before is arrival-to-arrival: idle gap + one byte time
    assert pkts[1].gap_before_ms == pytest.approx(40 + byte_time_s(19200) * 1000, abs=1e-6)


def test_packet_timestamp_is_first_byte_arrival():
    stream = simulate_wire([(b"\x01\x02\x03", 50), (b"\x04\x05", 30)])
    pkts = assemble(stream)
    assert pkts[0].timestamp == stream[0][1]
    assert pkts[1].timestamp == stream[3][1]


def test_gap_below_threshold_merges():
    a, b = gen.build_packet(0x20, 1, 2), gen.build_packet(0x12, 1, 3)
    pkts = assemble(simulate_wire([(a, 100), (b, 5)]))
    assert [p.raw_bytes for p in pkts] == [a + b]


def test_threshold_is_inclusive_on_measured_gap():
    """Split when arrival-to-arrival gap >= threshold (not idle time)."""
    pa = PacketAssembler(gap_threshold_ms=10, baud=19200)
    pa.feed(0xAA, 1.000)
    assert pa.feed(0xBB, 1.010) is not None          # exactly 10 ms -> split
    assert pa.feed(0xCC, 1.010 + 0.00999) is None    # 9.99 ms -> merge


def test_idle_gap_just_below_threshold_can_still_split_at_low_baud():
    """
    Documented quirk: measured gap = idle + 1 byte time. At 9600 baud
    (1.04 ms/byte) an idle gap of 9.0 ms measures 10.04 ms and splits.
    """
    a, b = b"\x01\x02", b"\x03\x04"
    pkts = assemble(simulate_wire([(a, 100), (b, 9.0)], baud=9600), gap_ms=10, baud=9600)
    assert len(pkts) == 2


def test_max_length_not_enforced():
    """PacketAssembler does not cap packet length (VMUX_MAX_PACKET_BYTES unused)."""
    big = bytes(range(100))
    pkts = assemble(simulate_wire([(big, 100)]))
    assert len(pkts) == 1 and pkts[0].length == 100


# ── Generator schedules through the assembler (burst / threshold) ────────

def _bursts_on_wire(min_gap_ms):
    sched = []
    for cmds in (gen.BURST_ON, gen.BURST_OFF):
        for pkt, gap, _ in gen.burst_schedule(cmds):
            sched.append((pkt, min_gap_ms if gap is None else gap))
    return sched


def test_burst_expected_result_each_burst_is_one_16_byte_packet():
    pkts = assemble(simulate_wire(_bursts_on_wire(gen.VMUX_MIN_GAP_MS)),
                    gap_ms=gen.VMUX_IDLE_GAP_MS)
    assert len(pkts) == 2
    assert pkts[0].raw_bytes == bytes.fromhex("20010223 11010313 12010310 13010311")
    assert pkts[1].raw_bytes == bytes.fromhex("20000222 11000312 12000311 13000310")
    assert pkts[1].gap_before_ms >= gen.VMUX_MIN_GAP_MS


def test_burst_inter_msg_gap_is_below_threshold():
    assert gen.VMUX_INTER_MSG_MS + 1000 * byte_time_s(9600) < gen.VMUX_IDLE_GAP_MS
    assert gen.VMUX_MIN_GAP_MS > gen.VMUX_IDLE_GAP_MS


@pytest.mark.parametrize("baud", [9600, 19200, 38400, 115200])
@pytest.mark.parametrize("threshold,margin", [(10, 3), (10, 2), (20, 5)])
def test_threshold_cases_split_and_merge(baud, threshold, margin):
    for case in gen.threshold_cases(threshold, margin):
        sched = [(case["pkts"][0], gen.THRESHOLD_PAIR_SEPARATION_MS),
                 (case["pkts"][1], case["gap_ms"])]
        pkts = assemble(simulate_wire(sched, baud=baud), gap_ms=threshold, baud=baud)
        assert len(pkts) == case["expect_packets"], case["name"]
        assert b"".join(p.raw_bytes for p in pkts) == b"".join(case["pkts"])


def test_threshold_cases_values():
    above, below = gen.threshold_cases(10, 3)
    assert (above["name"], above["gap_ms"], above["expect_packets"]) == ("above", 13, 2)
    assert (below["name"], below["gap_ms"], below["expect_packets"]) == ("below", 7, 1)
    assert gen.threshold_cases(2, 5)[1]["gap_ms"] == 0.0
