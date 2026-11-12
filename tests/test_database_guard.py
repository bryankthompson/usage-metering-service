import os
import subprocess
import sys

from conftest import database_url_problem

URL = "postgresql://metering:metering@localhost:5455/metering_x"


def test_guard_refuses_a_missing_or_empty_url():
    assert "DATABASE_URL is not set" in database_url_problem({})
    assert "DATABASE_URL is not set" in database_url_problem({"DATABASE_URL": ""})


def test_guard_accepts_a_url():
    assert database_url_problem({"DATABASE_URL": URL}) is None


def _collect(env):
    # --collect-only: no test or fixture runs, so nothing can be truncated even if the guard is broken.
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_scaffold.py"],
        env=env, capture_output=True, text=True, timeout=120)


def test_pytest_exits_2_without_database_url():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = _collect(env)
    assert result.returncode == 2
    assert "DATABASE_URL is not set" in result.stdout + result.stderr


def test_pytest_collects_with_database_url():
    result = _collect({**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"]})
    assert result.returncode == 0, result.stdout + result.stderr
