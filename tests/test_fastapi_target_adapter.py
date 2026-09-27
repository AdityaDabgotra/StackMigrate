from __future__ import annotations

from app.adapters.fast_api_target.test_adapter import FastAPITargetAdapter
from app.graph.state import TestOutcome

ADAPTER = FastAPITargetAdapter()

ALL_PASSED_OUTPUT = """
....                                                                    [100%]
4 passed in 0.08s
"""

ONE_FAILED_OUTPUT = """
..F.                                                                    [100%]
=================================== FAILURES ===================================
_________________________ test_create_order_missing_field _________________________
    def test_create_order_missing_field():
>       assert response.status_code == 422
E       assert 500 == 422

tests/test_orders.py:12: AssertionError
FAILED tests/test_orders.py::test_create_order_missing_field - assert 500 == 422
1 failed, 3 passed in 0.11s
"""

MULTIPLE_FAILED_OUTPUT = """
FF..
FAILED tests/test_orders.py::test_a - AssertionError
FAILED tests/test_orders.py::test_b - ValueError
2 failed, 2 passed in 0.10s
"""

NO_TESTS_COLLECTED_OUTPUT = """
ERROR tests/test_orders.py - ImportError: cannot import name 'OrderRouter' from 'app.routers.orders'
no tests ran in 0.02s
"""


def test_install_and_test_commands_are_argv_lists_not_shell_strings():
    # Argv form (not a shell string) is the actual security property being
    # tested here — it means there is no shell to inject into.
    assert isinstance(ADAPTER.install_command(), list)
    assert isinstance(ADAPTER.test_command(), list)
    assert "pytest" in ADAPTER.test_command()


def test_parse_all_passed():
    outcome, failing = ADAPTER.parse_test_output(ALL_PASSED_OUTPUT, exit_code=0)
    assert outcome == TestOutcome.PASSED
    assert failing == []


def test_parse_one_failed():
    outcome, failing = ADAPTER.parse_test_output(ONE_FAILED_OUTPUT, exit_code=1)
    assert outcome == TestOutcome.FAILED
    assert failing == ["tests/test_orders.py::test_create_order_missing_field"]


def test_parse_multiple_failed():
    outcome, failing = ADAPTER.parse_test_output(MULTIPLE_FAILED_OUTPUT, exit_code=1)
    assert outcome == TestOutcome.FAILED
    assert failing == ["tests/test_orders.py::test_a", "tests/test_orders.py::test_b"]


def test_parse_no_tests_collected_is_error_not_failure():
    outcome, failing = ADAPTER.parse_test_output(NO_TESTS_COLLECTED_OUTPUT, exit_code=5)
    assert outcome == TestOutcome.ERROR


def test_parse_internal_pytest_error_is_error():
    outcome, _ = ADAPTER.parse_test_output("INTERNALERROR> something broke", exit_code=3)
    assert outcome == TestOutcome.ERROR
