import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_benchmark_logging():
    """Keep a configured ``benchmark`` logger from leaking across tests.

    CLI tests call ``configure_logging``, which attaches a handler bound to the
    captured stderr stream. Resetting here stops later log calls from writing to
    a closed stream.
    """
    yield
    logger = logging.getLogger("benchmark")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def pytest_addoption(parser):
    parser.addoption(
        "--require-docker", action="store_true",
        help="Fail unless every selected docker_integration test passes without skips/xfails.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker_integration: requires a real Docker daemon and image")
    if config.getoption("--require-docker"):
        config.pluginmanager.register(_RequiredDockerTests(), "required-docker-tests")


class _RequiredDockerTests:
    def __init__(self):
        self.selected = set()
        self.passed = set()

    def pytest_collection_finish(self, session):
        self.selected = {
            item.nodeid for item in session.items
            if item.get_closest_marker("docker_integration") is not None
        }
        if not self.selected:
            raise pytest.UsageError("--require-docker requires selected docker_integration tests")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if item.nodeid not in self.selected:
            return
        if report.skipped or hasattr(report, "wasxfail"):
            report.outcome = "failed"
            report.longrepr = (
                f"{item.nodeid}: Docker integration must pass; skip/xfail is forbidden\n"
                f"{report.longrepr}"
            )
        if report.when == "call" and report.passed:
            self.passed.add(item.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        # Also reject --collect-only and tests that never reached their call phase.
        if exitstatus == 0 and (not self.selected or self.passed != self.selected):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
