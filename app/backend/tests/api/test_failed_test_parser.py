"""Failed tests read from a verifier's text: pytest, unittest, jest and plain expected/got lines; nothing parseable = empty list."""
import unittest

from harness_backend.services.failed_test_parser import FAILED_TESTS_LIMIT, parse_failed_tests

PYTEST_TEXT = """=================================== FAILURES ===================================
______________________________ test_wave_order ______________________________

    def test_wave_order():
>       assert compute() == [1, 2]
E       assert [1] == [1, 2]

tests/test_wave.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_wave.py::test_wave_order - assert [1] == [1, 2]
FAILED tests/test_wave.py::test_other - AssertionError: expected 5 but got 3
"""
UNITTEST_TEXT = """======================================================================
FAIL: test_total (tests.test_sum.SumTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_sum.py", line 7, in test_total
AssertionError: 3 != 4
"""
JEST_TEXT = """  ● cart › adds the item

    expect(received).toBe(expected)

    Expected: 2
    Received: 1
"""


class FailedTestParserTests(unittest.TestCase):
    def test_pytest_summary_lines_merge_with_their_section(self):
        tests = parse_failed_tests(PYTEST_TEXT)
        self.assertEqual([test["name"] for test in tests], ["test_wave_order", "tests/test_wave.py::test_other"])
        by_name = {test["name"].split("::")[-1]: test for test in tests}
        self.assertEqual((by_name["test_wave_order"]["got"], by_name["test_wave_order"]["expected"]), ("[1]", "[1, 2]"))
        self.assertEqual((by_name["test_other"]["expected"], by_name["test_other"]["got"]), ("5", "3"))
        self.assertIn("AssertionError", by_name["test_other"]["why"])

    def test_unittest_difference_gives_got_and_expected(self):
        test, = parse_failed_tests(UNITTEST_TEXT)
        self.assertEqual((test["name"], test["got"], test["expected"]), ("tests.test_sum.SumTests.test_total", "3", "4"))
        self.assertEqual(test["why"], "AssertionError: 3 != 4")

    def test_jest_expected_and_received_lines(self):
        test, = parse_failed_tests(JEST_TEXT)
        self.assertEqual((test["name"], test["expected"], test["got"]), ("cart › adds the item", "2", "1"))

    def test_nothing_parseable_is_an_empty_list_and_secrets_are_redacted(self):
        self.assertEqual(parse_failed_tests("all good, 12 passed"), [])
        self.assertEqual(parse_failed_tests(None), [])
        test, = parse_failed_tests("FAILED t.py::test_a - AssertionError: token=abc123 is wrong")
        self.assertNotIn("abc123", test["why"])

    def test_the_list_is_capped(self):
        text = "\n".join("FAILED t.py::test_%d - boom" % number for number in range(FAILED_TESTS_LIMIT + 5))
        self.assertEqual(len(parse_failed_tests(text)), FAILED_TESTS_LIMIT)


if __name__ == "__main__":
    unittest.main()
