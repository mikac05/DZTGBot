"""Unit tests for optional Basic Auth (username:password) compatibility switch.
"""

from __future__ import annotations

import unittest
from dztgbot.domain.policy import normalize_pat_input, credential_policy_decision
from dztgbot.infrastructure.jira_gateway import JiraGateway


class TestBasicAuthCompatibility(unittest.TestCase):
    """Test suite for Basic Auth vs PAT authentication modes."""

    def test_pat_only_mode_rejects_username_password(self) -> None:
        raw_user_pass = "john_doe:secret123"
        # When pat_only is True (default), username:password must be rejected
        norm = normalize_pat_input(raw_user_pass, pat_only=True)
        self.assertIsNone(norm)

        decision = credential_policy_decision(raw_user_pass, pat_only=True)
        self.assertFalse(decision.allowed)

    def test_basic_auth_enabled_mode_accepts_username_password(self) -> None:
        raw_user_pass = "john_doe:secret123"
        # When pat_only is False, username:password must be converted to Basic base64
        norm = normalize_pat_input(raw_user_pass, pat_only=False)
        self.assertIsNotNone(norm)
        self.assertTrue(norm.startswith("Basic "))

        decision = credential_policy_decision(raw_user_pass, pat_only=False)
        self.assertTrue(decision.allowed)

    def test_basic_auth_header_support_in_jira_gateway(self) -> None:
        # Verify JiraGateway._headers formats Basic header properly
        basic_token = "Basic am9obl9kb2U6c2VjcmV0MTIz"
        headers = JiraGateway._headers(basic_token)
        self.assertEqual(headers["Authorization"], basic_token)

        pat_token = "raw_pat_token_abc"
        headers_pat = JiraGateway._headers(pat_token)
        self.assertEqual(headers_pat["Authorization"], "Bearer raw_pat_token_abc")


if __name__ == "__main__":
    unittest.main()
