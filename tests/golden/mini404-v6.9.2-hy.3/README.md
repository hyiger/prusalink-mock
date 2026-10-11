# Mini404 captures

Real responses captured on 2026-10-11 with curl 8.7.1 (macOS) from the
Mini404 emulator (utils/mini404 in the firmware repo, PrusaLink on port 8080)
running hyiger/Prusa-Firmware-Buddy release `v6.9.2-hy.3` (`d54d239fd`), built for the CORE One
with filament slots and a test-only patch that enables three tools. The
`mini404-3tool` scenario mirrors that printer.

## Capturing again

`kit/` holds what it takes:

* `three-tools.patch`: the test-only firmware patch. Three tools without an
  MMU, tool mapping and the Load All, Unload All and Change All menus on that
  build, and a purge that fails when the extruder sensor loses the filament.
* `slots.gcode`: sets the filament slots to the captured state: Polymaker PLA
  in Red, Prusament PETG in Orange and 3D-Fuel ASA in Green. The capture was
  taken after printing it, with the printer back on the home screen.
* `capture.sh`: the requests below, with curl.

In the firmware repo, on a clean checkout of the pinned commit:

```sh
git apply PATH/TO/kit/three-tools.patch
utils/mini404/stage.sh -DHAS_FILAMENT_SLOTS:BOOL=YES
cp PATH/TO/kit/slots.gcode build/mini404/usb/
utils/mini404/start.sh
```

Print `slots.gcode` from the printer's screen, write the PrusaLink password
(Settings > Network > PrusaLink) to a file, and capture into a new folder:

```sh
PATH/TO/kit/capture.sh PASSWORD_FILE tests/golden/mini404-<commit>
```

Then point `GOLDEN` in tests/conftest.py at it. The scenario must keep
matching: if the serial, firmware version or temperatures differ, update
`mini404-3tool`.

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
