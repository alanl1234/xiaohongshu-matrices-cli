"""
Tests for camoufox.server.

Regression cover for #656: `python -m camoufox server` broke on Playwright
1.60, which bundled away the private `lib/browserServerImpl.js` that
launchServer.js reached into. The two tests below pin the invariants the fix
relies on, so the next time Playwright reshuffles its internals this fails in
CI rather than in a user's terminal.

These need Playwright's driver (a dependency) but never download or launch a
browser, so they stay fast enough to run anywhere.

Run with:
    cd pythonlib && python -m pytest tests/test_server.py -v
"""

import base64
import os
import subprocess
import sys
from pathlib import Path

import orjson
import pytest

# Make `import camoufox` resolve to the in-tree pythonlib without an install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from camoufox import server  # noqa: E402
from camoufox.server import get_nodejs  # noqa: E402

# Anything on the driver's private lib/ path is fair game for Playwright to
# move between releases; only the package entrypoint is a supported contract.
MODULE_ERRORS = ("Cannot find module", "MODULE_NOT_FOUND")


def _driver_package() -> Path:
    return Path(get_nodejs()).parent / "package"


def test_driver_entrypoint_exposes_launch_server():
    # launchServer.js calls playwright.firefox.launchServer() through the
    # driver's entrypoint. The driver is a bundled copy of playwright-core, so
    # this is public API -- but assert it rather than assume it, since the whole
    # bug was an assumption about driver layout going stale.
    nodejs = get_nodejs()
    result = subprocess.run(
        [
            nodejs,
            "-e",
            "const pw = require(process.argv[1]);"
            "console.log(typeof pw.firefox.launchServer)",
            str(_driver_package() / "index.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "function", result.stdout


def test_launch_script_resolves_driver_against_installed_playwright():
    # The #656 symptom exactly: launchServer.js died at require() time with
    # MODULE_NOT_FOUND before it ever read its config. Drive the real script
    # with a config pointing at a binary that does not exist -- reaching a
    # browser-launch failure proves require() and config parsing both worked.
    nodejs = get_nodejs()
    package = _driver_package()
    result = subprocess.run(
        [nodejs, str(server.LAUNCH_SCRIPT), str(package)],
        input=base64.b64encode(
            orjson.dumps({"executablePath": "/nonexistent/camoufox-bin"})
        ).decode(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    for error in MODULE_ERRORS:
        assert error not in combined, f"driver failed to resolve:\n{combined}"
    assert "Launching server..." in combined, combined
    assert "executable doesn't exist" in combined, combined


def test_launch_server_surfaces_child_exit_instead_of_pipe_error(monkeypatch, tmp_path):
    # The traceback in #656 was masked twice over: node died, then writing the
    # config to its dead stdin raised BrokenPipeError (EINVAL on Windows),
    # burying the real cause. launch_server() must report the child's exit.
    script = tmp_path / "dies_immediately.js"
    script.write_text("process.exit(3);\n")

    # Oversized so the write cannot fit in the pipe buffer and must hit the
    # closed pipe -- otherwise a small config lands in the buffer and the
    # regression stays invisible.
    monkeypatch.setattr(
        server, "launch_options", lambda **kwargs: {"pad": "x" * 500_000}
    )
    monkeypatch.setattr(server, "LAUNCH_SCRIPT", script)

    with pytest.raises(RuntimeError) as excinfo:
        server.launch_server()

    assert "3" in str(excinfo.value), str(excinfo.value)
