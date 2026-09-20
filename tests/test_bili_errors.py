"""错误码分类回归测试。"""

import unittest

from app.bili.errors import ResultKind, classify


class ErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_codes_are_distinct_from_regular_retry(self) -> None:
        self.assertIs(classify(429), ResultKind.RATE_LIMIT)
        self.assertIs(classify(412), ResultKind.RATE_LIMIT)
        self.assertIs(classify(3), ResultKind.RATE_LIMIT)
        self.assertIs(classify(221), ResultKind.RATE_LIMIT)
        self.assertIs(classify(900002), ResultKind.RATE_LIMIT)
        self.assertIs(classify(100001), ResultKind.RETRY)

    def test_congestion_codes_are_retryable(self) -> None:
        self.assertIs(classify(900001), ResultKind.RETRY)
        self.assertIs(classify(100039), ResultKind.RETRY)

    def test_new_risk_and_terminal_codes_are_classified(self) -> None:
        self.assertIs(classify(100044), ResultKind.RISK)
        self.assertIs(classify(100016), ResultKind.FATAL)
