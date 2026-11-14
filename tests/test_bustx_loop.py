"""BusTx over pyserial loop:// — wire bytes, gap handling, manual DE."""
import pytest
import serial

import vmux_generator as gen


@pytest.fixture
def loop_tx(monkeypatch, no_sleep):
    """BusTx whose serial.Serial is a pyserial loop:// port."""
    ports = []

    def fake_serial(port=None, **kw):
        s = serial.serial_for_url("loop://", **kw)
        ports.append(s)
        return s

    monkeypatch.setattr(gen.serial, "Serial", fake_serial)

    def make(**kw):
        tx = gen.BusTx(port="LOOP", baud=19200, quiet=True, **kw)
        return tx, ports[-1]
    return make


def test_wire_bytes_and_count(loop_tx):
    tx, port = loop_tx()
    a, b = gen.build_packet(0x20, 1, 2), gen.sync_packet()
    tx.send(a)
    tx.send(b)
    assert port.read(8) == bytes.fromhex("20010223 50000151")
    assert tx.packet_count == 2


def test_default_gap_is_min_gap_and_override(loop_tx, no_sleep):
    tx, _ = loop_tx(min_gap_ms=40)
    no_sleep.clear()
    tx.send(gen.sync_packet())
    assert no_sleep[0] == pytest.approx(0.040)          # --gap honoured
    no_sleep.clear()
    tx.send(gen.sync_packet(), gap_before_ms=5)
    assert no_sleep[0] == pytest.approx(0.005)
    no_sleep.clear()
    tx.send(gen.sync_packet(), gap_before_ms=0)          # no pre-gap sleep
    assert no_sleep == [pytest.approx(4 * 10 / 19200 + 0.001)]


def test_shift_register_wait(loop_tx, no_sleep):
    tx, _ = loop_tx()
    no_sleep.clear()
    tx.send(gen.build_packet(1, 1, 1), gap_before_ms=0)
    assert no_sleep[-1] == pytest.approx(4 * 10.0 / 19200 + 0.001)


def test_manual_de_toggles_rts(loop_tx, monkeypatch):
    tx, port = loop_tx(manual_de=True)
    states = []
    real_write = port.write

    def spy_write(data):
        states.append(port.rts)
        return real_write(data)

    monkeypatch.setattr(port, "write", spy_write)
    tx.send(gen.sync_packet())
    assert states == [True]           # DE asserted during write
    assert port.rts is False          # released afterwards


def test_rts_dtr_low_after_open(loop_tx):
    _, port = loop_tx()
    assert port.rts is False and port.dtr is False
