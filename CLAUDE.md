# CLAUDE.md: prusalink-mock

## Purpose

A mock PrusaLink server that behaves like the Prusa Buddy firmware, for
testing slicer integrations without a printer. The first consumer is the
hyiger PrusaSlicer fork's "sync filament presets from the printer" feature,
which reads `GET /api/v1/tools` from a CORE One INDX (tool N maps to extruder
N), plus PrusaSlicer's existing calls: `/api/version`, `/api/v1/storage` and
`PUT /api/v1/files/usb//<name>`.

Runtime is Python >= 3.10, standard library only. Tests need only pytest.

## The fidelity rule

Match the firmware, not Prusa's OpenAPI spec. Every behavior must trace to
firmware source: cite `file:line` (paths relative to the
hyiger/Prusa-Firmware-Buddy repo) in the docstring or comment next to the
code. When the source reading and a capture from the emulator disagree, the
capture wins and the code gets fixed. Behavior that isn't modeled answers
501 "Not modeled by prusalink-mock" rather than inventing a response, and is
listed under Fidelity in README.md.

## Pinned firmware

Fixtures (`tests/fixtures/firmware`) and captures
(`tests/golden/mini404-v6.9.2-hy.3`) come from hyiger/Prusa-Firmware-Buddy
release `v6.9.2-hy.3` (`d54d239fd` on `release/6.9.2`). `/api/v1/tools`
arrived in `08968e64f`, which is on `release/6.9.2` but not on `master`.

When the firmware's PrusaLink code changes (`lib/WUI/`,
`src/common/json_encode.cpp`, `src/common/http/`, `utils/gen-automata/`),
mirror it here:

1. Update the renderer or route and its source citation.
2. Rebuild the affected fixtures from the source.
3. Recapture goldens from the Mini404 emulator (`utils/mini404` in the
   firmware repo, PrusaLink on `http://127.0.0.1:8080`) into
   `tests/golden/mini404-<commit>/`, with the patch, slot G-code and capture
   script in `tests/golden/mini404-v6.9.2-hy.3/kit` (its README has the steps).
   Plain `utils/mini404` has one tool. Then run the contract suite against it:
   `pytest -m contract --target http://127.0.0.1:8080 --password-file=FILE`.
4. Update the pinned commit here, in README.md and in the fixture READMEs.

The emulator can't run the INDX (8 tools, dock gaps, printer `7.10.0`, no tool
picked); those cases rest on source reading until a printer running a build
with `/api/v1/tools` can be captured read-only.

## Layout

```
src/prusalink_mock/
  core.py       listeners, connection loop, faults, lingering close
  wire.py       request parsing, header writer, chunked framing
  auth.py       X-Api-Key, RFC 2069 Digest, nonces, log redaction
  routes.py     printer routing in firmware selector order, uploads
  render.py     firmware-exact JSON bodies and status pages
  state.py      scenario loading, merge patch, validation
  storage.py    in-memory USB drive; sfn.py makes FAT short names
  faults.py     fault rules; control.py the control API
  api.py        MockPrinter (in-process, background thread)
  cli.py        plmock serve | run | scenarios
  scenarios/    shipped scenario JSON (package data)
tests/          pytest; httpraw.py is a raw-socket client
```

## Running tests

```sh
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

No install needed: the pytest config puts `src/` and `tests/` on `sys.path`.
The CLI runs from a checkout with `PYTHONPATH=src python -m prusalink_mock`.
CI (`.github/workflows/tests.yml`) runs Linux, macOS and Windows with Python
3.10 and 3.13.

## Conventions

* Responses are written by hand in `render.py`, never with `json.dumps`, to
  keep the firmware's key order, `%.Nf` floats and escaping.
* Never print, log or store the password. The request log redacts X-Api-Key,
  the Digest `response` and other Authorization credentials, and scrubs the
  password from the other headers and the Digest username; the state API
  leaves out `auth`.
* Validate at the boundary. Scenarios, the control API and `MockPrinter`
  share one validator (`state.normalize`) that knows each field's firmware
  type and range. Input the firmware couldn't hold is a 400, a 422 or a
  `ScenarioError` up front, never accepted and then a 500, a dropped
  connection or invalid JSON on the printer port later.
* Signals in code, tests and probes: SIGTERM, SIGHUP and SIGINT only. Never
  SIGQUIT, SIGABRT, SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGTRAP or SIGSYS: on
  macOS each process that dies from one gets a crash report and a dialog.
  `plmock run` never re-raises a signal on itself.
* Docs are in the user's voice: American spelling, no em-dashes, concise.
* Commits: imperative, capitalized subject, no trailing period. Never push to
  `main` directly; work on a branch.
