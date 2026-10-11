# prusalink-mock

A mock PrusaLink server that answers the way the Prusa Buddy firmware does, so code that talks to a printer can be tested without one. I built it for PrusaSlicer's "sync filament presets from the printer" work, which reads `GET /api/v1/tools` from a CORE One INDX, but it also handles the calls PrusaSlicer already makes (`/api/version`, `/api/v1/storage` and the PUT upload).

It copies the firmware, not Prusa's OpenAPI spec: the same RFC 2069 Digest, the same headers in the same order, the same JSON bytes, the same quirks. Fixtures and captures are pinned to hyiger/Prusa-Firmware-Buddy commit `312ad0dc7`.

Python 3.10 or newer, standard library only. GPL-3.0.

## Quick start

From a checkout, nothing to install:

```sh
PYTHONPATH=src python -m prusalink_mock serve
```

On Windows, set it first: `$env:PYTHONPATH = "src"` in PowerShell, `set PYTHONPATH=src` in cmd. Or `pip install -e .` and use `plmock serve`.

```
PrusaLink mock, scenario indx-hy8: http://127.0.0.1:8088
User: maker. Password and API key: the scenario's test key (see README)
Control API: http://127.0.0.1:8089
```

Every shipped scenario uses the test key `mockKeyTest2345`, as both the API key and the Digest password. Use `--password-file FILE` (or `--password`) to set your own; the mock never prints it.

Options: `--scenario NAME|FILE`, `--host` (default `127.0.0.1`), `--port` (default 8088), `--control-port` (default 8089), `--log-file requests.jsonl`, `--seed N` for repeatable nonces, `--frozen-clock` so time moves only through the control API.

### Point PrusaSlicer at it

1. Add a Physical Printer, host type **PrusaLink**, hostname `http://127.0.0.1:8088`.
2. Authorization: API key `mockKeyTest2345`, or user and password with user `maker` and the same password (that's Digest).
3. Test, then Send to printer. See what arrived with `curl http://127.0.0.1:8089/_mock/files`.

Use `127.0.0.1` rather than `localhost`: the mock listens on IPv4 only, and `localhost` can resolve to `::1` first.

For manual runs of the fork, start PrusaSlicer with a throwaway `--datadir` and set `filamentdb_url` in its `PrusaSlicer.ini` to a closed port, so the fresh profile doesn't load from or push to your real Filament DB.

## Scenarios

`plmock scenarios` lists them; `--scenario` takes a name or a path to your own file.

| Scenario | Printer |
| --- | --- |
| `indx-hy8` (default) | CORE One INDX 8T, 0.40 high-flow nozzles (not hardened), printer `7.10.0`, idle with no tool picked. Tools 1, 2, 3, 5 and 7 loaded with preset vendors and colors, 4, 6 and 8 empty. |
| `indx-hy8-hf06` | The same with 0.60 nozzles. |
| `indx-gaps` | Docks 1, 2, 3 and 5 calibrated, so the keys have a gap; tool 5 is PA with no vendor or color on a 0.60 hardened nozzle. Byte-identical to the firmware fixture. |
| `indx-no-docks` | No dock calibrated: `{"tools":{}}`. |
| `indx-stock-fw` | `indx-hy8` on firmware without filament slots: `/api/v1/tools` is 404 after auth. |
| `coreone-stock` | Single-tool CORE One (printer `7.1.0`), no tools endpoint. |
| `indx-printing` | Printing job 42 with tool 1 picked. |
| `login-disabled` | Empty password: every `/api` request is 401, whatever you send. |
| `mini404-3tool` | The Mini404 emulator as captured: CORE One build with slots and a test patch for three tools. |

A scenario is one flat JSON file: `firmware` (`version`, `has_filament_slots`), `printer` (model, hostname, serial, state, `active_tool` with 0 for no tool picked, bed, nozzle, axis, flow, speed, fans), `auth.password`, `storage.available`, `tools`, and `job` while printing. See `src/prusalink_mock/scenarios/indx-printing.json` for every field.

`tools` is the `/api/v1/tools` body, so a captured body pastes straight in. A missing key is a dock that isn't calibrated (the firmware leaves it out). Scenarios are checked against the firmware's limits: tool keys 1 to 8 on an INDX, material up to 7 bytes, vendor and color up to 15, `color_hex` as uppercase `#RRGGBB` or empty, and an unloaded tool reports empty strings. `color_hex` can be left out for the 16 preset colors.

## Control API

JSON over HTTP on `127.0.0.1:8089` (`$PRUSALINK_MOCK_CONTROL` under `run`). Control traffic never shows up in the request log.

| Request | What it does |
| --- | --- |
| `GET /_mock/health` | URLs, scenario name, pid |
| `GET /_mock/state` | Current state (without the password) |
| `PUT /_mock/state` | Replace the state; 422 with a list of errors if it's invalid |
| `PATCH /_mock/state` | Merge patch; `tools` is replaced as a whole, `null` deletes a key |
| `PUT /_mock/tools/{n}`, `DELETE /_mock/tools/{n}` | Set one tool; delete means its dock isn't calibrated |
| `GET /_mock/scenarios`, `POST /_mock/scenario {"name": ...}` | List or switch scenarios (clears files) |
| `POST /_mock/reset` | Reload the scenario; clear faults, log and files; reboot |
| `GET /_mock/requests?since=N`, `DELETE /_mock/requests` | Request log after sequence N; clear it |
| `GET /_mock/files`, `GET /_mock/files/{path}`, `DELETE ...` | Uploaded files; the raw bytes of one |
| `GET`, `POST`, `DELETE /_mock/faults`, `DELETE /_mock/faults/{id}` | Fault rules |
| `POST /_mock/clock/advance {"seconds": N}` | Move uptime on (301 s expires every nonce) |
| `POST /_mock/reboot` | New nonce random half: old nonces get `stale=true` |
| `POST /_mock/shutdown` | Stop `serve` |

```sh
C=http://127.0.0.1:8089
curl -X PUT $C/_mock/tools/3 -d '{"loaded":true,"material":"PETG","vendor":"Prusament","color":"Black",
  "nozzle_diameter":0.4,"hardened":false,"high_flow":true}'
curl -X DELETE $C/_mock/tools/8
curl -X PATCH $C/_mock/state -d '{"printer":{"bed":{"actual":60.0,"target":60.0}}}'
curl -X POST $C/_mock/scenario -d '{"name":"indx-stock-fw"}'
curl "$C/_mock/requests?since=0"
curl -o got.gcode "$C/_mock/files/benchy.bgcode"
curl -X POST $C/_mock/clock/advance -d '{"seconds":301}'
```

Log entries carry the method, the raw target, the normalized path, headers with the API key and Digest response redacted, the auth outcome, status, whether the connection closed, the fault that fired, and upload details (`stored_as`, `sfn_path`, `overwrite`, `print_after_upload`). `--log-file` also appends them to a JSON lines file.

## Fault rules

```sh
curl -X POST $C/_mock/faults -d '{"match":{"method":"GET","path":"/api/v1/tools","auth":"ok"},
  "skip":0,"times":1,"action":{"type":"status","status":500,"message":"boom"}}'
```

`match` takes `method`, `path` (exact), `path_glob` and `auth` (`any`, `none`, `ok`, `fail`). Paths are compared without the query and with duplicate slashes collapsed, still percent-encoded. `skip` lets the first N matches through; `times` limits how often the rule fires (`null` is forever). Rules are tried in the order they were added.

| Action | Effect |
| --- | --- |
| `{"type":"status","status":503,"message":"..."}` | A firmware-style status page with any code. `body` and `content_type` send your own body instead; `close` forces Connection: close. |
| `{"type":"malformed","mode":"truncate","keep_bytes":40}` | The real JSON cut short, in a clean chunked stream |
| `{"type":"malformed","mode":"wrong_shape"}` | Valid JSON of the wrong shape (the body wrapped in an array, or your `body`) |
| `{"type":"malformed","mode":"html"}` | A 200 captive-portal HTML page |
| `{"type":"delay","ms":1500}` | Wait, then answer normally |
| `{"type":"hang","seconds":30}` | Never answer; close after `seconds` (30 at most) or at shutdown |
| `{"type":"drop","when":"before_response"}` | Close (FIN) without a response |
| `{"type":"drop","when":"mid_body","after_bytes":120}` | Send the head and `after_bytes` more bytes, then close |

For "connection refused", point the client at a closed port.

## The run wrapper

```sh
python -m prusalink_mock run --scenario indx-gaps --log-file build/prusalink-mock.jsonl -- ./slic3rutils_tests "[PrusaLinkMock]"
```

This starts the mock on free `127.0.0.1` ports, exports `PRUSALINK_MOCK_URL`, `PRUSALINK_MOCK_CONTROL` and `PRUSALINK_MOCK_KEY` to the command, returns the command's exit code, and then shuts the mock down, hung requests included. There's no daemon to clean up. For CTest:

```cmake
add_test(NAME prusalink_mock
  COMMAND ${Python3_EXECUTABLE} -m prusalink_mock run --scenario indx-hy8
          --log-file ${CMAKE_BINARY_DIR}/prusalink-mock.jsonl
          -- $<TARGET_FILE:slic3rutils_tests> "[PrusaLinkMock]")
set_tests_properties(prusalink_mock PROPERTIES
  ENVIRONMENT "PYTHONPATH=${PRUSALINK_MOCK_DIR}/src"
  SKIP_RETURN_CODE 4   # Catch2: every selected test skipped
  TIMEOUT 300)
```

## Python API

```python
from prusalink_mock import MockPrinter

with MockPrinter("indx-gaps") as printer:   # free 127.0.0.1 ports, background thread
    printer.url, printer.control_url, printer.password
    printer.set_tool(4, {"loaded": True, "material": "PA", "nozzle_diameter": 0.6,
                         "hardened": True, "high_flow": True})
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "times": 1,
                       "action": {"type": "status", "status": 500}})
    printer.requests(), printer.file_bytes("a b.gcode")
```

## Tests

```sh
python -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The curl tests skip when curl isn't installed. CI runs on Linux, macOS and Windows with Python 3.10 and 3.13.

### Contract mode

Tests marked `contract` check firmware behavior that has to hold on the mock and on real firmware alike: exact 401, 404 and 405 pages, Digest details, header order, JSON key order and number formats. Without options they run against an in-process mock. Point them at the Mini404 emulator or a printer to check the mock's assumptions:

```sh
.venv/bin/python -m pytest -m contract --target http://127.0.0.1:8080 --password-file=$HOME/.prusalink-password
```

They send only GET and HEAD requests, plus requests that fail authentication. `--allow-writes` adds an upload and delete of `/usb/plmock/contract test.gcode`; on a real printer, an upload opens the print preview on the screen. The password is read from the file and never printed; failures show `<secret>` in its place. Keep the `=` in `--password-file=FILE`, or pytest takes the file's folder for the project root.

## Fidelity

Firmware paths below are in hyiger/Prusa-Firmware-Buddy at `312ad0dc7`.

Modeled:

* Request parsing (`lib/WUI/nhttp/req_parser.cpp`, `utils/gen-automata/http.py`): raw target kept as sent, 168-byte limit then 414 (`src/common/http/types.h:154`), prefix matching of `Connection`, `Accept`, `Overwrite` and `Print-After-Upload`, keep-alive rules (`req_parser.cpp:265-267`), Content-Length only (411 otherwise), no `100 Continue`.
* Auth (`req_parser.cpp:115-174, 309-429`, `status_page.cpp:91-118`): X-Api-Key exact match, RFC 2069 Digest with HA2 over the raw target, nonce `random32 << 32 | uptime` valid for 300 s, the `stale=true` rule, last auth header wins, Basic gets the Digest challenge, empty password locks everything, auth before routing on `/api/`, `/usb/` and `/thumb/`. Both unit-test vectors from `tests/unit/lib/WUI/nhttp/server_tests.cpp` pass.
* Responses (`lib/WUI/nhttp/headers.cpp:64-103`, `status_page.cpp:42-85`, `send_json.cpp`, `src/common/http/chunked.h`): header order, no Date or Server, chunked JSON with `%04X` sizes in 1024-byte packets on keep-alive, close-delimited otherwise, status pages as text or JSON cut at 127 bytes, and the lingering half-close on error paths (`handler.h:129-131`, `handler.cpp:15-24`) so clients see the status instead of a reset.
* Bodies (`basic_gets.cpp`, `tools_renderer.cpp`, `status_renderer.cpp`, `file_info.cpp`, `src/common/json_encode.cpp`): `/api/version`, `/api/v1/info`, `/api/v1/storage`, `/api/v1/tools` (404 without filament slots), `/api/v1/status`, `/api/v1/job` (204 without a job, for any method), `/api/settings`.
* Uploads (`gcode_upload.cpp:116-413`, `server.cpp:931-941`, `prusa_api_helpers.cpp:32-144`): percent-decoding with `+` as space, slash dedup, 411, 501 for 0 bytes, 415, 409 without `Overwrite: ?1`, 422, one transfer at a time, folders created as needed, FAT short names as FatFS makes them, 201 with the file info and Connection: close. Also Create-Folder, GET/HEAD file info, DELETE and POST (print). `POST /api/files/...` with a multipart body gets 410.

Not modeled, or simplified:

* Folder listings, ETags and 304; `/usb/` downloads; `/thumb/` previews; legacy `/api/printer`, `/api/job` and `/api/files`. These answer 501 "Not modeled by prusalink-mock" (after auth).
* Transfers: `/api/v1/transfer` is always 204 and status has no `transfer` block.
* Printing: Print-After-Upload and `POST /api/v1/files/...` are recorded but start nothing. Job pause, resume and stop change state at once, without the in-between states.
* Status with no tool picked reports zero temperatures, flow and fans. That follows `marlin_vars.hpp:402-404` but isn't confirmed on hardware.
* The 3-connection limit (`server.h:155-168`). Timeouts are approximate: 60 s idle, 10 s for a stalled upload body.
* Short names for non-ASCII file names use `_` rather than the OEM code page.
* Chunk boundaries follow the packet size, not the firmware's JSON segment boundaries.
* Digest through Windows SSPI curl hasn't been checked; those tests are allowed to fail there.
