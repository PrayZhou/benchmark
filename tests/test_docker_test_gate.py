"""Exercise the CI gate in isolated pytest sessions, without Docker."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.parametrize(
    ("body", "options", "success"),
    [
        ("pass", [], True),
        ("pytest.skip('daemon unavailable')", [], False),
        ("pytest.skip('image pull failed')", [], False),
        ("pytest.xfail('not implemented')", [], False),
        ("assert False", [], False),
        ("pass", ["--collect-only"], False),
        ("pass", ["-m", "not docker_integration"], False),
    ],
)
def test_required_docker_gate(pytester, body, options, success):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text())
    pytester.makepyfile(f"import pytest\n@pytest.mark.docker_integration\ndef test_real():\n    {body}\n")
    result = pytester.runpytest("--require-docker", *options)
    assert (result.ret == 0) is success


@pytest.mark.parametrize("strict", [False, True])
def test_setup_skip_gate(pytester, strict):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text())
    pytester.makepyfile('''
import pytest
@pytest.fixture
def docker_image():
    pytest.skip("daemon unavailable")
@pytest.mark.docker_integration
def test_real(docker_image):
    pass
''')
    result = pytester.runpytest(*(["--require-docker"] if strict else []))
    if strict:
        result.assert_outcomes(errors=1)
        assert result.ret != 0
    else:
        result.assert_outcomes(skipped=1)
        assert result.ret == 0
