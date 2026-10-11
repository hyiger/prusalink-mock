# prusalink-mock

A mock PrusaLink server that answers the way the Prusa Buddy firmware does, so code that talks to a printer can be tested without one. I built it for PrusaSlicer's "sync filament presets from the printer" work, which reads `GET /api/v1/tools` from a CORE One INDX, but it also handles the calls PrusaSlicer already makes (`/api/version`, `/api/v1/storage` and the PUT upload).

It copies the firmware, not Prusa's OpenAPI spec: the same RFC 2069 Digest, the same headers in the same order, the same JSON bytes, the same quirks. Fixtures and captures are pinned to hyiger/Prusa-Firmware-Buddy release `v6.9.2-hy.3` (`d54d239fd`).

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

Every shipped scenario uses the test key `mockKeyTest2345`, as both the API key and the Digest password. Use `--password-file FILE` (or `--password`) to set your own; the mock never prints it. The file may start with a UTF-8 byte order mark and end with a line break. An empty file is an error: for login disabled, use `--scenario login-disabled`.

Options: `--scenario NAME|FILE`, `--host` (default `127.0.0.1`), `--port` (default 8088), `--control-port` (default 8089), `--log-file requests.jsonl`, `--seed N` for repeatable nonces, `--frozen-clock` so time moves only through the control API.

### Point PrusaSlicer at it

1. Add a Physical Printer, host type **PrusaLink**, hostname `http://127.0.0.1:8088`.
2. Authorization: API key `mockKeyTest2345`, or user and password with user `maker` and the same password (that's Digest).
3. Test, then Send to printer. See what arrived with `curl http://127.0.0.1:8089/_mock/files`.

Use `127.0.0.1` rather than `localhost`: by default the mock listens on IPv4 only, and `localhost` can resolve to `::1` first. `--host ::` listens on IPv6 only and prints an `http://[::1]:8088` URL.

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

A scenario is one flat JSON file. Outside `tools` and `job`, every field has a default:

* `name`, `description`
* `firmware`: `version` (`6.9.2+0`), `has_filament_slots` (false)
* `printer`:
  * `model`: `COREONEINDX` (default), `COREONELINDX`, `COREONE`, `COREONEL`, `MK4`, `MK4S`, `MINI` or `XL`
  * `printer_version`, `tool_count` and `hostname`: from the model, and they follow it when it changes. Set one to override it, or to `null` in a patch to make it follow the model again. `GET /_mock/state` shows `null` while they do. A CORE One build with three tools, like `mini404-3tool`, needs `tool_count`.
  * `nozzle_diameter`, what `/api/version` and `/api/v1/info` report: tool 1's while tool 1 is listed, as on the printer, otherwise the last value (0.4)
  * `serial`, `mmu` (false), `min_extrusion_temp` (170)
  * `state` (`IDLE`), `active_tool` (0 for no tool picked, the default on a toolchanger; always 1 on a single-tool printer)
  * `job_id` (0): the current or last job's id, which the firmware keeps after the job ends
  * `bed` and `nozzle` (`actual`, `target`), `axis` (`x`, `y`, `z`), all 0.0; `flow` and `speed` (100); `fans` (`hotend`, `print`, in RPM, 0)
* `auth.password` (`mockKeyTest2345`), `storage.available` (true)
* `tools`, below
* `job`, required while `PRINTING` or `PAUSED`, allowed in `ATTENTION`: `progress` and `time_printing` (required), `time_remaining`, `filament_change_in`, and `file` with `sfn_path`, `display_name`, and `size` with `m_timestamp` (both or neither)

`src/prusalink_mock/scenarios/indx-printing.json` shows most of them.

`tools` is the `/api/v1/tools` body, so a captured body pastes straight in. A missing key is a dock that isn't calibrated (the firmware leaves it out). Tool keys are the strings `"1"` to `"8"` on an INDX, and an unloaded tool reports empty strings. Names follow the firmware's rules: material is up to 7 bytes of `A-Za-z0-9_-` and not `---`; vendor and color are up to 15 bytes of `A-Za-z0-9 _-.+&` with no space at either end. A preset vendor or color is spelled exactly as the firmware spells it. A preset color always has its own `color_hex`, so it can be left out for the 16 presets; a user color needs one, as uppercase `#RRGGBB`. Tools that name the same user vendor or color agree on its spelling and its `color_hex`.

Everything else is checked against the type the firmware keeps it in, so the printer port never sends something the firmware couldn't:

* Strings are valid Unicode without control characters. `hostname` is up to 20 bytes, `serial` 24, `job.file.display_name` 166 and `job.file.sfn_path` 102. `printer_version` is three numbers from 0 to 255, like `7.10.0`.
* Decimal numbers fit a 32-bit float. `bed.target` and `nozzle.target` are whole numbers from -32768 to 32767 (`60` or `60.0`).
* `job_id`, `flow`, `speed` and the fans are 0 to 65535, and `time_printing` 0 to 4294967295. `time_remaining` and `filament_change_in` stop at 4294967294: the firmware leaves the field out at 4294967295, so leave it out instead. `size` is 0 to 2147483647 (a 32-bit `off_t`), and `m_timestamp` a FAT date from 1980 to 2107 (315532800 to 4354819199).
* A scenario or patch is a JSON object nested 32 levels deep at most.

## Control API

JSON over HTTP on `127.0.0.1:8089` (`$PRUSALINK_MOCK_CONTROL` under `run`). Control traffic never shows up in the request log. Unlike the printer port, it answers `Expect: 100-continue`, so libcurl doesn't wait a second before each PUT.

| Request | What it does |
| --- | --- |
| `GET /_mock/health` | URLs, scenario name, pid |
| `GET /_mock/state` | Current state (without the password) |
| `PUT /_mock/state` | Replace the state; 422 with a list of errors if it's invalid |
| `PATCH /_mock/state` | Merge patch; `tools` is replaced as a whole, `null` deletes a key |
| `PUT /_mock/tools/{n}`, `DELETE /_mock/tools/{n}` | Set one tool; delete means its dock isn't calibrated |
| `GET /_mock/scenarios`, `POST /_mock/scenario {"name": ...}` | List or switch scenarios (drops printer connections, clears files) |
| `POST /_mock/reset` | Reload the scenario; clear faults, log and files; reboot, which drops printer connections and requests in flight |
| `GET /_mock/requests?since=N&limit=M`, `DELETE /_mock/requests` | Request log after sequence N (the first M entries); clear it |
| `GET /_mock/files`, `GET /_mock/files/{path}`, `DELETE ...` | Uploaded files; the raw bytes of one |
| `GET`, `POST`, `DELETE /_mock/faults`, `DELETE /_mock/faults/{id}` | Fault rules |
| `POST /_mock/clock/advance {"seconds": N}` | Move uptime on (301 s expires every nonce). The wall clock stops at the end of 2107, the last FAT date; a step past it is 400. |
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

A state write keeps the current password, `--password` included, unless it sets `auth.password`.

Log entries carry the method, the raw target, the normalized path, headers with the API key, the Digest response and other credentials redacted (and the password scrubbed from any other header), the auth outcome, status, whether the connection closed, the fault that fired, upload details (`stored_as`, `sfn_path`, `overwrite`, `print_after_upload`), `print_requested` for a print start, and `error` for timeouts, early closes and bugs in the mock. An entry is in the log before its response goes out. `--log-file` also appends them to a JSON lines file.

## Fault rules

```sh
curl -X POST $C/_mock/faults -d '{"match":{"method":"GET","path":"/api/v1/tools","auth":"ok"},
  "skip":0,"times":1,"action":{"type":"status","status":500,"message":"boom"}}'
```

`match` takes `method`, `path` (exact), `path_glob` and `auth` (`any`, `none`, `ok`, `fail`). Paths are compared without the query and with duplicate slashes collapsed, still percent-encoded. `skip` lets the first N matches through; `times` limits how often the rule fires (`null` is forever). Rules are tried in the order they were added.

| Action | Effect |
| --- | --- |
| `{"type":"status","status":503,"message":"..."}` | A firmware-style status page with any code; `message` is one line of text. `body` and `content_type` (printable ASCII) send your own body instead; `close` forces Connection: close. |
| `{"type":"malformed","mode":"truncate","keep_bytes":40}` | The real JSON cut short, in a clean chunked stream |
| `{"type":"malformed","mode":"wrong_shape"}` | Valid JSON of the wrong shape (the body wrapped in an array, or your `body`, which has to be valid JSON) |
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

This starts the mock on free `127.0.0.1` ports, exports `PRUSALINK_MOCK_URL`, `PRUSALINK_MOCK_CONTROL` and `PRUSALINK_MOCK_KEY` to the command, returns the command's exit code, and then shuts the mock down, hung requests included. There's no daemon to clean up. SIGTERM and SIGHUP sent to `plmock` are passed on to the command. After a SIGINT the command gets a second to end on its own, since Ctrl-C in a terminal reaches it too; then it gets the SIGINT, and after 10 seconds SIGTERM. For CTest:

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

On Mini404, `--allow-writes` needs the emulated USB drive to be a FAT image (`-drive id=usbstick,format=raw,file=usb.img`). QEMU's `fat:rw:` folder drive crashes the emulator as soon as the firmware creates a folder. The full suite, writes included, passes against Mini404 running `v6.9.2-hy.3` with a FAT32 image.

## Fidelity

Firmware paths below are in hyiger/Prusa-Firmware-Buddy at `v6.9.2-hy.3` (`d54d239fd`).

Modeled:

* Request parsing (`lib/WUI/nhttp/req_parser.cpp`, `utils/gen-automata/http.py` and `parts.py`), byte by byte like the firmware's automaton: the method runs to the first space or tab (a stray CRLF before it makes an unknown method, so 405), the URL runs to the next one, a line ends at LF, CRLF or a bare CR, unknown header names run to the next colon, and a malformed request line is 400 with close. The raw target is kept as sent, with a 168-byte limit then 414 (`src/common/http/types.h:154`). Also prefix matching of `Connection`, `Accept`, `Overwrite` and `Print-After-Upload`, keep-alive rules (`req_parser.cpp:265-267`), Content-Length only (411 otherwise) and summed in 32 bits like the printer's `size_t` (`req_parser.cpp:175-180`), multipart boundaries counted in the URL buffer they share (`req_parser.cpp:107-114`), no `100 Continue`.
* Timeouts and closes (`server.cpp:481-560`): a connection is idle until a byte of a request arrives, and an idle one closes without a word after 60 s or when the client closes. A request or upload body that stalls for 10 s gets the firmware's bare `HTTP/1.1 408 Request Timeout` and its trailing NUL, then a close. A client that closes partway gets 400: a cut-off head, "Truncated body" before any body byte, "Connection closed by client" after some.
* Auth (`req_parser.cpp:115-174, 309-429`, `status_page.cpp:91-118`): X-Api-Key exact match, RFC 2069 Digest with HA2 over the raw target, nonce `random32 << 32 | uptime` valid for 300 s, the `stale=true` rule, last auth header wins, Basic gets the Digest challenge, empty password locks everything, auth before routing on `/api/`, `/usb/` and `/thumb/`. Both unit-test vectors from `tests/unit/lib/WUI/nhttp/server_tests.cpp` pass.
* Responses (`lib/WUI/nhttp/headers.cpp:64-103`, `status_page.cpp:42-85`, `send_json.cpp`, `src/common/http/chunked.h`): header order, no Date or Server, chunked JSON with `%04X` sizes in 1024-byte packets on keep-alive, close-delimited otherwise, status pages as text or JSON cut at 127 bytes, and the lingering half-close on error paths (`handler.h:129-131`, `handler.cpp:15-24`) so clients see the status instead of a reset.
* Bodies (`basic_gets.cpp`, `tools_renderer.cpp`, `status_renderer.cpp`, `file_info.cpp`, `src/common/json_encode.cpp`): `/api/version`, `/api/v1/info`, `/api/v1/storage`, `/api/v1/tools` (404 without filament slots), `/api/v1/status`, `/api/v1/job` (204 without a job, for any method), `/api/settings`. With no tool picked, status reports zero nozzle temperatures and flow; the INDX still reports the head's fan RPMs, other toolchangers zero (`marlin_vars.hpp:402-404`, `marlin_server.cpp:3653-3660`). That rests on source reading; no capture covers it yet.
* Job commands (`prusa_link_api_v1.cpp:42-155`): the id is read like `strtol` (white space and a sign first, 33 bytes at most, `str_utils.cpp:102-128`) and compared with the printer's `job_id`, which outlives the job, so a command for a job that just ended gets 409, not 404.
* Uploads (`gcode_upload.cpp:116-413`, `server.cpp:931-941`, `prusa_api_helpers.cpp:32-144`): percent-decoding with `+` as space and `%XX` read like `strtol` (`%+4` is 0x04, `url_decode.cpp:16-29`), slash dedup, 411, 501 for 0 bytes, 415, 409 without `Overwrite: ?1`, one transfer at a time, folders created as needed, FAT short names as FatFS makes them, 201 with the file info and Connection: close. A name FatFS refuses (`"*:<>?|`, DEL or bad UTF-8, `ff.c:2822-2828`) gets 404 "Destination directory does not exist", or 500 "Failed to create a folder" when it's a folder. Paths are followed the way FatFS follows them (`ff.c:2806-2860, 3009-3077`): trailing dots and spaces are dropped, so `dir.` is `dir`, `.` and `..` are the folder and its parent, a name with nothing left is refused, and names are compared one character at a time, so `straße` and `strasse` are two files. With all 99 numbered short names taken (`ff.c:2483-2490`), an upload gets 422 "Filename contains invalid characters" and a new folder 500. Also Create-Folder (with the same name checks), GET/HEAD file info, DELETE (which a PUT in progress doesn't block, since it writes a temporary file) and POST (print). `POST /api/files/...` with a multipart body gets 410.

Not modeled, or simplified:

* Folder listings, ETags and 304; `/usb/` downloads; `/thumb/` previews; legacy `/api/printer`, `/api/job` and `/api/files`. These answer 501 "Not modeled by prusalink-mock" (after auth).
* `GET /` and `/index.html` serve a mock page with the scenario and its tools. The firmware serves its gzipped PrusaLink web UI there (`static_file.cpp:21-63`), with `Content-Encoding: gzip`, an ETag and 304; the web UI's other files are 404 on the mock.
* Upload names with a control character or a backslash, which FatFS reads as the end of the name or a folder separator: 501.
* Transfers: `/api/v1/transfer` is always 204 and status has no `transfer` block. Connect's partial transfers, and with them DELETE's "File is being transferred", aren't modeled.
* Printing: Print-After-Upload and `POST /api/v1/files/...` are recorded but start nothing. Job pause, resume and stop change state at once, without the in-between states.
* The 3-connection limit (`server.h:155-168`). Timeouts run from the last byte to the second; the firmware polls every 0.5 s.
* Inside Authorization the parser is simpler than the firmware's: the scheme and quoted values may run across line ends, otherwise the header ends at the line end.
* Short names for non-ASCII file names use `_` rather than the OEM code page.
* Chunk boundaries follow the packet size, not the firmware's JSON segment boundaries.
* Digest through Windows SSPI curl hasn't been checked; those tests are allowed to fail there.
