"""
Scenario smoke tests on a fake clock: run burst/threshold scenarios through a
recording BusTx stand-in, replay the (packet, gap) stream through
PacketAssembler, and check the documented expected results.
"""
import pytest

import vmux_generator as gen
from conftest import simulate_wire, assemble


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class RecordingTx:
    """Duck-typed BusTx: records (packet, effective_gap_ms) and advances the clock."""
    def __init__(self, clock, min_gap_ms=gen.VMUX_MIN_GAP_MS, baud=19200):
        self.clock, self.min_gap_ms, self.baud = clock, min_gap_ms, baud
        self.sent = []
        self._last_end = None

    def send(self, pkt, label="", gap_before_ms=None):
        if gap_before_ms is None:
            gap_before_ms = self.min_gap_ms
        self.clock.sleep(gap_before_ms / 1000.0)
        # effective idle = time since the previous packet finished
        idle_ms = (gap_before_ms if self._last_end is None
                   else (self.clock.t - self._last_end) * 1000.0)
        self.sent.append((pkt, idle_ms))
        self.clock.sleep(len(pkt) * 10.0 / self.baud)
        self._last_end = self.clock.t

    @property
    def packet_count(self):
        return len(self.sent)


@pytest.fixture
def fake_env(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gen.time, "time", clock.time)
    monkeypatch.setattr(gen.time, "sleep", clock.sleep)
    monkeypatch.setattr(gen.random, "uniform", lambda a, b: a)
    return clock


def _framed(tx):
    return assemble(simulate_wire(tx.sent), gap_ms=gen.VMUX_IDLE_GAP_MS)


def test_burst_scenario_expected_framing(fake_env):
    tx = RecordingTx(fake_env)
    gen.scenario_burst(tx, duration=10.0)
    pkts = _framed(tx)
    sync = gen.sync_packet()
    bursts = [p for p in pkts if p.raw_bytes != sync]
    assert bursts, "no bursts sent"
    assert all(p.length == 16 for p in bursts)
    assert all(sync not in p.raw_bytes or p.raw_bytes == sync for p in pkts)
    assert any(p.raw_bytes == sync for p in pkts[1:]), "no periodic SYNC"


def test_threshold_scenario_expected_framing(fake_env):
    tx = RecordingTx(fake_env)
    gen.scenario_threshold(tx, duration=10.0, threshold_ms=10, margin_ms=3)
    pkts = [p.raw_bytes for p in _framed(tx) if p.raw_bytes != gen.sync_packet()]
    above, below = gen.threshold_cases(10, 3)
    n_above = sum(1 for p in pkts if p == above["pkts"][0])
    assert n_above > 0
    assert pkts.count(above["pkts"][1]) == n_above                # split
    assert pkts.count(b"".join(below["pkts"])) == n_above         # merged
    assert len(pkts) == 3 * n_above
