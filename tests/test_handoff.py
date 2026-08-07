from __future__ import annotations

import unittest

from scripts import handoff


class HandoffSafetyTests(unittest.TestCase):
    def test_private_paths_are_rejected_but_examples_are_allowed(self) -> None:
        self.assertIsNotNone(handoff._path_forbidden(".env"))
        self.assertIsNotNone(handoff._path_forbidden("src/ref/vpnsettings.xml"))
        self.assertIsNotNone(handoff._path_forbidden("config/live.nmconnection"))
        self.assertIsNone(handoff._path_forbidden(".env.example"))
        self.assertIsNone(
            handoff._path_forbidden("config/l2tp-ipsec.example.nmconnection")
        )

    def test_credential_shapes_are_detected_without_storing_a_credential(self) -> None:
        telegram_shaped = "123456789" + ":" + ("A" * 24)
        google_shaped = "AI" + "za" + ("B" * 24)
        self.assertIn(
            "Telegram-token-shaped value",
            handoff._secret_findings_in_text(telegram_shaped),
        )
        self.assertIn(
            "Google-key-shaped value",
            handoff._secret_findings_in_text(google_shaped),
        )

    def test_placeholder_assignments_are_allowed(self) -> None:
        placeholder = "TELEGRAM_BOT" + "_TOKEN=TODO_REPLACE_WITH_PRIVATE_VALUE"
        self.assertEqual(handoff._secret_findings_in_text(placeholder), [])

    def test_configured_sensitive_assignment_is_detected(self) -> None:
        configured = "GEMINI_API" + "_KEY=" + "not-a-placeholder"
        self.assertIn(
            "configured GEMINI_API_KEY assignment",
            handoff._secret_findings_in_text(configured),
        )


if __name__ == "__main__":
    unittest.main()

