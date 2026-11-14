"""SyncDetector: SYNC recognition, interval tracking, baud verdict."""
import pytest

from vmux_capture import SyncDetector, VmuxPacket

SYNC = bytes([0x50, 0x00, 0x01, 0x51])


def pkt(raw, t):
    return VmuxPacket(timestamp=t, raw_bytes=raw, gap_before_ms=100.0, baud_rate=19200)


def feed_all(det, items):
    return [det.feed(pkt(raw, t)) for raw, t in items]


def test_sync_code_0x50_detected_and_confirmed_after_three():
    det = SyncDetector()
    res = feed_all(det, [(SYNC, 0.0), (SYNC, 4.0)])
    assert res == [True, True]
    assert not det.confirmed                      # one interval only
    det.feed(pkt(SYNC, 8.02))
    assert det.confirmed
    assert det.avg_interval == pytest.approx(4.01)
    assert det.baud_verdict().startswith("CONFIRMED OK")


def test_unconfirmed_verdict():
    assert SyncDetector().baud_verdict() == "unconfirmed"


def test_interval_outside_window_ignored():
    det = SyncDetector()
    feed_all(det, [(SYNC, 0.0), (SYNC, 1.0), (SYNC, 10.0)])   # 1 s and 9 s
    assert det.avg_interval is None and not det.confirmed


def test_off_nominal_interval_warns():
    det = SyncDetector()
    feed_all(det, [(SYNC, 0.0), (SYNC, 6.0), (SYNC, 12.0)])
    assert det.confirmed
    assert det.baud_verdict().startswith("WARNING")


def test_non_sync_code_short_packet_not_sync_until_repeated():
    det = SyncDetector()
    other = bytes([0x60, 0x00, 0x01, 0x61])
    assert det.feed(pkt(other, 0.0)) is False
    assert det.feed(pkt(other, 4.0)) is False
    assert det.feed(pkt(other, 8.0)) is True      # hypothesis 2: repeated pattern
    assert det.sync_packet == other


def test_long_packet_never_hypothesis2_sync():
    det = SyncDetector()
    long_pkt = bytes(range(1, 9))
    assert not any(feed_all(det, [(long_pkt, 4.0 * i) for i in range(5)]))


@pytest.mark.xfail(strict=True, reason=(
    "Known limitation: hypothesis 2 ignores timing. Any short packet seen 3x "
    "in the window (e.g. a repeated command 50 ms apart) is classified as SYNC."))
def test_repeated_command_at_non_sync_period_is_not_sync():
    det = SyncDetector()
    cmd = bytes([0x20, 0x01, 0x02, 0x23])
    res = feed_all(det, [(cmd, 0.00), (cmd, 0.05), (cmd, 0.10)])
    assert res == [False, False, False]
