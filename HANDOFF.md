# HANDOFF — V-MUX replacement controller (Chat → Code)

Date: 2026-11-14. Owner: Brad (bhunting). Repo: https://github.com/bhunting/weldon_vmux_sniffer

Read `agents.md` for full project context. This file is the state-of-play and the work queue.

## State
- Phase 1 (passive capture / protocol RE). SH-U11F adapter ordered, **not yet arrived**. No real bus data exists yet.
- `vmux_capture.py` v3: compiles; bench-untested against hardware.
- `vmux_generator.py` v1: **the repo copy is broken** (SyntaxError: `global VMUX_MIN_GAP_MS` after use; `--gap` never took effect). The fixed version is in this handoff package and must replace the repo copy. Verified with `py_compile` and a `loop://` loopback test (wire bytes `20 01 02 23 50 00 01 51`, checksums correct, 40 ms default gap honoured, `PacketAssembler` splits two packets).
- Design HTML (`vmux_bus_tap_schematic.html`, `vmux_replacement_architecture.html`) are drafts with unverified values.

## Known corrections to earlier chat output (treat earlier claims as withdrawn)
1. STM32F405 has no hardware RS-485 DE (DEM/DEAT/DEDT) — that arrived in F0/F3+. Verify in RM0090. DE strategy is open (GPIO + TC interrupt, auto-direction transceiver, or MCU change).
2. SH-U11F: direction control (TXDEN), jumper location, isolation voltage, isolator part were asserted without support. The user guide only says 120Ω is a jumper, default disabled.
3. J1 pinout and Weldon part number (0K90-3111-00) are unverified against Brad's wiring diagrams.
4. Message code table is placeholders, not from Weldon docs.
5. FTDI latency timer (16 ms default) was omitted; set to 1 ms.
6. Capture timestamps are interpolated per byte; inter-packet gaps inside one batched read are lost.
7. Burst scenario expected result was self-contradictory; undefined.
8. Scope voltages in schematic/block diagram are illustrative; SM712 wording needs checking.

## Work queue (priority order)
1. Replace repo `vmux_generator.py` with the fixed copy; delete `vmux_capture_README.md` (duplicate of README); add `.gitignore`; commit.
2. Add a test harness (pytest) around `PacketAssembler`, `build_packet`, checksum, `SyncDetector`, using `loop://` or fakes. Make `py_compile` + tests a pre-commit check. (`ast.parse` misses `global`-after-use.)
3. Define burst-scenario expectations; add a gap-just-above-threshold scenario.
4. Verify J1 pinout against `docs/reference/` wiring diagrams; correct agents.md and schematic.
5. Verify SH-U11F details against the manual in `docs/reference/` once placed; confirm direction control on a scope when hardware arrives.
6. Decide DE strategy (see correction 1); update schematic and firmware plan.
7. Phase 1 vehicle capture when adapter arrives; build message map; replace placeholder codes.
8. Validate schematic values (SM712 choice, common-mode ranges, scope amplitudes).

## Open design decisions (need Brad)
- Replacement display selection (SPI vs RGB/parallel; size; touch controller).
- Physical switch-panel I/O count (drives GPIO expander strategy).
- Protocol: baud, framing, checksum, 8N1 vs 9-bit — all unknown until capture.

## Hard safety rules
- NO termination resistors on the V-MUX bus. Ever.
- Phase 1 is passive: never drive the bus. RTS/DTR low. Generator is for bench only, never connected to the vehicle.
- Bus GND (J1 pin 1, if verified) only for references; never chassis.
- Any incrementing BC (bus collision) count on the vehicle is a serious fault.

## Environment notes
- GitHub push from the Chat session was impossible (invalid token); Brad commits manually.
- Windows 15.6 ms timer floor is why capture uses blocking `read(1)` with `timeout = gap_ms/2000`.
