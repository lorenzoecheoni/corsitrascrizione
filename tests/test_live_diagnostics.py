"""Exercise pytest's real failure renderer without credentials or network."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("phase,message", [
    ("configuration", "Invalid live configuration (details withheld)"),
    ("analysis", "Live analysis failed; inspect only sanitized application diagnostics"),
])
def test_live_failure_output_does_not_disclose_private_input(tmp_path, phase, message):
    # Reintroducing exception chaining in either live boundary leaks this value.
    sentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC_72fada"
    env = dict(os.environ)
    env.update({
        "RUN_LIVE_BUNNY": "1",
        "BUNNY_LIBRARY_ID": sentinel if phase == "configuration" else "123",
        "BUNNY_STREAM_API_KEY": "TEST_ONLY_BUNNY",
        "BUNNY_CDN_HOSTNAME": "cdn.example.invalid",
        "BUNNY_TOKEN_AUTH_KEY": "",
        "OPENAI_API_KEY": "TEST_ONLY_OPENAI",
        "APP_PASSWORD": "TEST_ONLY_PASSWORD",
        "BUNNY_SAMPLE_VIDEO_URL": "https://example.invalid/authorized-sample",
        "DIAGNOSTIC_SENTINEL": sentinel,
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    live_test = Path(__file__).with_name("test_live_bunny.py").resolve()
    script = """
import os
from pathlib import Path
import socket
import sys
import pytest

sys.path.insert(0, str(Path(sys.argv[1]).parents[1]))

def deny_network(*args, **kwargs):
    raise AssertionError("Offline diagnostic test attempted a network connection")

socket.socket.connect = deny_network

class OfflineFailure:
    def pytest_collection_modifyitems(self, items):
        def fail_services(settings):
            raise RuntimeError(os.environ["DIAGNOSTIC_SENTINEL"])
        for item in items:
            item.module.build_services = fail_services

raise SystemExit(pytest.main([sys.argv[1], "-q", "--tb=long"], plugins=[OfflineFailure()]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(live_test)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert message in output
    assert sentinel not in output
