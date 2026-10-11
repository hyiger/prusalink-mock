# Mini404 captures

Real responses captured on 2026-10-11 with curl 8.7.1 (macOS) from the
Mini404 emulator (utils/mini404 in the firmware repo, PrusaLink on port 8080)
running hyiger/Prusa-Firmware-Buddy commit `312ad0dc7`, built for the CORE One
with filament slots and a test-only patch that enables three tools. The
`mini404-3tool` scenario mirrors that printer.

* `<name>.headers`: every response head curl saw, in order (for Digest, the
  401 challenge first).
* `<name>.body`: the body of the last response.
* `capture.log`: name, status and time of each capture.

Only nonce values may differ from the mock (tests/test_golden.py).

| Name | Request |
| --- | --- |
| version, v1-info, v1-storage, v1-status, v1-tools, v1-job | `curl --digest -u maker:PW` GET of /api/version, /api/v1/info, ... |
| v1-tools-http10 | the same with `--http1.0` |
| unauth, unauth-json | GET /api/v1/tools without credentials (`-H 'Accept: application/json'`) |
| wrong-password, wrong-user | Digest with a wrong password, or user `admin` |
| apikey, apikey-wrong | GET /api/v1/tools with the right or a wrong X-Api-Key |
| notfound-v1, notfound-v1-json | Digest GET /api/v1/nope (text, JSON) |
| notfound-root | GET /nope, no credentials |
| files-missing | Digest GET /api/v1/files/usb/does-not-exist.gcode |
| files-no-storage | Digest GET /api/v1/files |
| head-tools | Digest HEAD /api/v1/tools (`curl -I`) |
