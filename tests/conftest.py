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
