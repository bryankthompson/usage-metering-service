"""Every `app.*` log line must reach the container's log. uvicorn configures only its own loggers, and Python's default
level (WARNING) hid "webhook processed/stale/duplicate/ignored" and "charges computed": a replayed webhook left no trace.

Each case runs in a fresh interpreter, as `uvicorn app.main:app` would, so pytest's own log handlers cannot hide a bug.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(code: str, **env) -> str:
    """The stderr of `python -c code` with uvicorn's own logging already configured, as uvicorn does before importing."""
    prelude = ("import logging.config, uvicorn.config\n"
               "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)\n"
               "import logging\n")
    environment = {**{k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}, **env}
    result = subprocess.run([sys.executable, "-I", "-c", prelude + code], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stderr


EMIT = ("import sys; sys.path.insert(0, '.')\n"
        "import app.main\n"
        "logging.getLogger('app.webhooks').info('INFO-LINE')\n"
        "logging.getLogger('app.webhooks').warning('WARNING-LINE')\n"
        "logging.getLogger('app.webhooks').debug('DEBUG-LINE')\n")


def test_an_info_line_from_an_app_module_reaches_the_container_log_once_with_its_name():
    err = run(EMIT)
    assert err.count("INFO-LINE") == 1 and err.count("WARNING-LINE") == 1
    assert "DEBUG-LINE" not in err
    assert " INFO app.webhooks INFO-LINE" in err and " WARNING app.webhooks WARNING-LINE" in err     # time, level, logger, message


def test_uvicorns_own_lines_still_appear_exactly_once_and_stay_off_the_root_logger():
    err = run(EMIT + "logging.getLogger('uvicorn.error').info('UVICORN-ERROR-LINE')\n"
                     "print('propagate:', logging.getLogger('uvicorn').propagate, logging.getLogger('uvicorn.access').propagate,"
                     " file=__import__('sys').stderr)\n")
    assert err.count("UVICORN-ERROR-LINE") == 1
    assert "propagate: False False" in err                     # so the root handler added for app.* cannot print them twice


def test_creating_the_app_twice_does_not_print_a_line_twice():
    err = run(EMIT + "app.main.create_app(); app.main.create_app()\n"
                     "logging.getLogger('app.pricing').info('AGAIN')\n")
    assert err.count("AGAIN") == 1 and err.count("INFO-LINE") == 1


@pytest.mark.parametrize("value, shown, hidden", [
    ("WARNING", ["WARNING-LINE"], ["INFO-LINE", "DEBUG-LINE"]),
    ("debug", ["DEBUG-LINE", "INFO-LINE", "WARNING-LINE"], []),
    (" Error ", [], ["INFO-LINE", "WARNING-LINE", "DEBUG-LINE"]),
])
def test_log_level_picks_what_is_shown_and_ignores_case_and_spaces(value, shown, hidden):
    err = run(EMIT, LOG_LEVEL=value)
    assert all(word in err for word in shown) and not any(word in err for word in hidden)


@pytest.mark.parametrize("value", ["banana", "", "15", "NOTSET", "INFO;DROP"])
def test_an_invalid_log_level_falls_back_to_info_and_says_so_without_crashing(value):
    err = run(EMIT, LOG_LEVEL=value)
    assert "INFO-LINE" in err and "DEBUG-LINE" not in err
    assert "is not one of DEBUG, INFO, WARNING, ERROR, CRITICAL; using INFO" in err and repr(value) in err


def test_the_level_holds_when_something_else_already_configured_the_root_logger():
    # basicConfig does nothing if the root logger has a handler (a test runner, a wrapper): the app logger's own level must
    # still let INFO through to that handler.
    err = run("logging.getLogger().addHandler(logging.StreamHandler())\n" + EMIT)
    assert err.count("INFO-LINE") == 1 and "DEBUG-LINE" not in err
