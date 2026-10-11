# AGENTS.md: prusalink-mock

A mock of the Prusa Buddy firmware's PrusaLink HTTP API, so slicer
integration tests (PrusaSlicer first) can run without a printer. CLAUDE.md
has the project rules: the fidelity rule, the pinned firmware, the layout and
the conventions.

## Review guidelines

The mock is a test double. It listens on localhost, the test's own client
drives it, and it holds test passwords (the shipped scenarios use
`mockKeyTest2345`). Nobody in that setup is an attacker, so review for what a
test could hit or be misled by.

Report these:

* Fidelity: a difference from the firmware at the pinned commit that a client
  could observe, for requests a real client could send. That covers status
  codes, headers, bodies, Digest and X-Api-Key auth, uploads, FAT names,
  timestamps and timeouts. Cite the firmware source.
* Reliability: crashes, hangs, deadlocks, flaky tests, failures on Linux,
  macOS or Windows with Python 3.10 to 3.13, and request log or control API
  behavior a test would rely on.
* Test author input: a value a test could plausibly pass to the control API,
  a scenario or `MockPrinter` (NaN, infinity, an out-of-range reading) that is
  accepted and then breaks the printer port with invalid JSON or a 500. Such
  input must be rejected up front.

Don't report these:

* Hostile or pathological input on the printer port: request heads or header
  values megabytes long, floods of headers, slow clients, and the memory or
  CPU they cost. Differences from the firmware that only such input reaches
  are out of scope too.
* Keeping the password out of the request log beyond what the code does now.
  Log redaction is a convenience for test logs, not a security boundary.
  Password changes racing a request, partly encoded or mixed encodings,
  overlapping passwords and the password history are out of scope.
* Contrived control API values no test would write, such as integers
  hundreds of digits long.
* Production hardening and network exposure. The mock isn't meant to face
  real traffic.
