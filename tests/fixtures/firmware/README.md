# Firmware fixtures

Response bodies reconstructed byte for byte from the Buddy firmware source at
hyiger/Prusa-Firmware-Buddy release `v6.9.2-hy.3` (`d54d239fd` on
`release/6.9.2`). They are raw bodies with no trailing newline:
don't reformat them.

Serial numbers, temperatures and timestamps are placeholders. The scenarios
`indx-hy8`, `indx-gaps` and `indx-printing` carry the same values, so the mock
reproduces these files exactly (tests/test_render.py).

| File | Renderer in the firmware |
| --- | --- |
| api_version.json | lib/WUI/link_content/basic_gets.cpp:200-226 |
| api_v1_info.json | basic_gets.cpp:228-253 |
| api_v1_storage.json | basic_gets.cpp:473-490 |
| api_v1_tools_indx.json | lib/WUI/nhttp/tools_renderer.cpp:34-65 |
| api_v1_status_idle_indx_no_tool.json | lib/WUI/nhttp/status_renderer.cpp:19-82 (no tool picked) |
| api_v1_status_printing.json | status_renderer.cpp:19-82 |
| api_v1_job_printing.json | basic_gets.cpp:388-459 |
| api_v1_files_usb_file.json | lib/WUI/nhttp/file_info.cpp:232-264 |
| api_settings.json | lib/WUI/link_content/prusa_link_api_octo.cpp:50-52 |
| error_404_accept_json.json | lib/WUI/nhttp/status_page.cpp:57-62 |
