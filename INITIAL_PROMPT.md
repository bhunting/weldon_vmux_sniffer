Read CLAUDE.md, HANDOFF.md and agents.md in this directory. We are continuing the V-MUX replacement controller project, moving from a Chat workflow to Code.

Start with items 1–3 of the work queue in HANDOFF.md:
1. Sync the repo: replace vmux_generator.py with the fixed copy in the package, remove the duplicate vmux_capture_README.md, add .gitignore, and show me the diff/commit before committing.
2. Add a pytest harness for PacketAssembler, build_packet/checksum, SyncDetector, using loop:// or fakes. Verify with py_compile and the tests.
3. Define expected results for the burst scenario and add a just-above-threshold gap scenario.

Then inspect docs/reference/ for the wiring diagrams and SH-U11F manual and verify (or flag) the J1 pinout and adapter details in agents.md. Do not assert unverified hardware facts; list what you could and could not confirm. Report back with a short summary and your recommendation on the DE strategy for the STM32F405 (options with tradeoffs).
