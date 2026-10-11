"""Real curl against the mock: the client PrusaSlicer is built on (skipped without curl)."""

from __future__ import annotations

import subprocess

import pytest


def run_curl(curl, *args, timeout=60):
    return subprocess.run([curl, "-sS", *args], capture_output=True, timeout=timeout)


@pytest.fixture
def digest_curl(curl, request):
    """curl for Digest tests. A Windows curl may do Digest through SSPI, which is not verified yet."""
    if "SSPI" in subprocess.run([curl, "-V"], capture_output=True, text=True).stdout:
        request.applymarker(pytest.mark.xfail(reason="Digest through Windows SSPI is not verified", strict=False))
    return curl


def test_digest_get_reuses_the_connection(digest_curl, printer, tmp_path):
    curl = digest_curl
    out1, out2 = tmp_path / "version.json", tmp_path / "tools.json"
    r = run_curl(curl, "--digest", "-u", f"maker:{printer.password}", "-o", str(out1), printer.url + "/api/version",
                 "-o", str(out2), printer.url + "/api/v1/tools")
    assert r.returncode == 0, r.stderr
    assert out1.read_bytes().startswith(b'{"api":"2.0.0"') and out2.read_bytes().startswith(b'{"tools":{"1"')
    entries = printer.requests()
    assert [(e["target"], e["status"]) for e in entries] == [
        ("/api/version", 401), ("/api/version", 200), ("/api/v1/tools", 401), ("/api/v1/tools", 200)]
    assert len({e["conn"] for e in entries}) == 1
    assert entries[1]["auth"]["username"] == "maker"


def test_digest_put_two_legs(digest_curl, printer, tmp_path):
    curl = digest_curl
    src = tmp_path / "a b.gcode"
    src.write_bytes(b"G28\nG1 Z5\n" * 100)
    r = run_curl(curl, "--digest", "-u", f"maker:{printer.password}", "-X", "PUT", "-H", "Overwrite: ?1",
                 "-H", "Content-Type: text/x.gcode", "--data-binary", f"@{src}", "-o", str(tmp_path / "out"),
                 "-w", "%{http_code}", printer.url + "/api/v1/files/usb//a%20b.gcode")
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"201"
    assert printer.file_bytes("a b.gcode") == src.read_bytes()
    puts = [e for e in printer.requests() if e["method"] == "PUT"]
    assert [(e["status"], e["body_len"]) for e in puts] == [(401, 0), (201, src.stat().st_size)]
    assert puts[0]["conn"] != puts[1]["conn"]  # the 401 to a PUT closes the connection
    assert puts[1]["upload"]["stored_as"] == "/usb/a b.gcode"


@pytest.mark.parametrize("expect", [True, False], ids=["expect-100", "no-expect"])
def test_rejected_big_put_reports_401_not_a_reset(curl, printer, tmp_path, expect):
    src = tmp_path / "big.gcode"
    src.write_bytes(b"G1 X1\n" * (2 * 1024 * 1024 // 6))
    extra = [] if expect else ["-H", "Expect:"]
    r = run_curl(curl, "-X", "PUT", "-H", "X-Api-Key: wrong-key", *extra, "--data-binary", f"@{src}",
                 "-o", str(tmp_path / "out"), "-w", "%{http_code}", printer.url + "/api/v1/files/usb//big.gcode")
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"401"
    assert (tmp_path / "out").read_bytes() == b"401: Unauthorized\n\n\n"
    assert printer.files() == []


def test_api_key_get(curl, printer):
    r = run_curl(curl, "-H", f"X-Api-Key: {printer.password}", printer.url + "/api/v1/storage")
    assert r.returncode == 0
    assert r.stdout == b'{"storage_list":[{"path":"/usb/","name":"usb","type":"USB","read_only":false,"available":true}]}'


def test_http10_digest(digest_curl, printer):
    curl = digest_curl
    r = run_curl(curl, "--http1.0", "--digest", "-u", f"maker:{printer.password}", printer.url + "/api/v1/info")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(b'{"nozzle_diameter":0.40')


def test_upload_and_print_header(curl, printer, tmp_path):
    src = tmp_path / "p.bgcode"
    src.write_bytes(b"GCDE" + bytes(100))
    r = run_curl(curl, "-X", "PUT", "-H", f"X-Api-Key: {printer.password}", "-H", "Print-After-Upload: ?1",
                 "--data-binary", f"@{src}", "-o", str(tmp_path / "out"), "-w", "%{http_code}",
                 printer.url + "/api/v1/files/usb//p.bgcode")
    assert r.stdout == b"201"
    assert printer.files()[0]["print_after_upload"] is True
