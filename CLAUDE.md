# CLAUDE.md

Project: Weldon V-MUX replacement controller (2013 Braun ambulance). Read `HANDOFF.md` first (state + work queue), then `agents.md` (full context).

## Working style
- User is a senior embedded engineer. Be technical, concise, professional. Give options and counterpoints; justify with technical reasoning.
- Do not state unverified hardware facts as fact. Label assumptions. Check docs in `docs/reference/` before asserting pinouts, part numbers, or register features.
- Review-and-iterate on code: show diffs, explain changes.

## Rules
- Validate Python with `python -m py_compile <file>` and tests, never `ast.parse` alone.
- Never add code that drives the V-MUX bus from the capture tool. The generator is bench-only.
- Never add termination resistors/jumpers to any bus-side design.
- Keep `agents.md` and `HANDOFF.md` updated when facts change or are verified.
- Commit messages: concise, imperative.

## Layout
- `vmux_capture.py`, `vmux_generator.py`, `requirements.txt`, `README.md`
- `docs/` design HTML; `docs/reference/` user-supplied manuals and wiring diagrams (read-only source of truth)
