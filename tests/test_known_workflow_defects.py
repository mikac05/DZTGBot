"""Named target tests for known workflow defects (P0-C).

Each case documents an audit defect. Until the corresponding remediation lands,
tests that cannot yet pass are marked ``expectedFailure``. They must never
assert that insecure behavior is *desired* — they only track that the defect
still exists or that the fix is not yet wired.

When a defect is fixed, remove ``expectedFailure`` so the test becomes a
normal regression gate.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dztgbot.core import MAX_BATCH_SIZE, forwarded_message_in
from dztgbot.domain.callbacks import parse_callback_data, CallbackParseError
from dztgbot.domain.policy import (
    classify_credential_input,
    CredentialInputKind,
    is_private_chat,
)
from dztgbot.user_store import JiraCredentials, UserStore
from tests.support.security_fakes import (
    TEST_ONLY_COOKIE_SHAPE,
    TEST_ONLY_PASSWORD_SHAPE,
    TEST_ONLY_PAT,
)
from tests.support.workflow_fakes import (
    make_forwarded_photo_message,
    make_forwarded_text_message,
)


def _core_source() -> str:
    import dztgbot.core as core_mod

    return Path(core_mod.__file__).read_text(encoding="utf-8")


def _jira_client_source() -> str:
    import dztgbot.jira_client as mod

    return Path(mod.__file__).read_text(encoding="utf-8")


def _jira_auth_source() -> str:
    import dztgbot.jira_auth as mod

    return Path(mod.__file__).read_text(encoding="utf-8")


class KnownDefectTrackingTests(unittest.TestCase):
    """Static/source and behavior probes for named audit defects."""

    def test_defect_photo_appended_before_batch_cap_check(self) -> None:
        """21st forward can still append a photo file_id before size reject."""
        source = _core_source()
        photo_idx = source.find("pending_photo_file_ids")
        cap_idx = source.find("len(batch) >= MAX_BATCH_SIZE")
        self.assertGreater(photo_idx, 0)
        self.assertGreater(cap_idx, 0)
        # Defect signature: photo append appears before batch cap in analyze_forward.
        analyze = source[source.find("async def analyze_forward") : source.find("async def batch_worker")]
        self.assertIn("pending_photo_file_ids", analyze)
        photo_in_analyze = analyze.find("pending_photo_file_ids")
        cap_in_analyze = analyze.find("MAX_BATCH_SIZE")
        self.assertLess(
            photo_in_analyze,
            cap_in_analyze,
            "expected photo append before batch cap (known defect order)",
        )

    def test_defect_ordinary_photo_path_appends_without_editing_guard_first(self) -> None:
        """handle_edited_text_input appends photos before editing_draft guard."""
        source = _core_source()
        start = source.find("async def handle_edited_text_input")
        self.assertGreater(start, 0)
        # Slice until next top-level async def after this function
        rest = source[start + 1 :]
        end_rel = rest.find("\n    async def ")
        body = source[start : start + 1 + end_rel] if end_rel > 0 else source[start:]
        photo_pos = body.find("pending_photo_file_ids")
        guard_pos = body.find('get("editing_draft")')
        self.assertGreater(photo_pos, 0)
        self.assertGreater(guard_pos, 0)
        self.assertLess(
            photo_pos,
            guard_pos,
            "known defect: photo append precedes editing_draft check",
        )

    def test_defect_create_uses_unbound_callback_data(self) -> None:
        """Production confirm button is not j1:<action>:<token> yet."""
        source = _core_source()
        self.assertIn('callback_data="jira_confirm"', source)
        with self.assertRaises(CallbackParseError):
            parse_callback_data("jira_confirm")

    def test_defect_create_pops_template_before_jira_call(self) -> None:
        source = _core_source()
        confirm_region = source[source.find('query.data != "jira_confirm"') :]
        pop_idx = confirm_region.find('pop("pending_template"')
        create_idx = confirm_region.find("create_issue")
        self.assertGreater(pop_idx, 0)
        self.assertGreater(create_idx, 0)
        self.assertLess(
            pop_idx,
            create_idx,
            "known defect: draft removed before create_issue",
        )

    def test_defect_silent_issuetype_fallback_present(self) -> None:
        source = _jira_client_source()
        self.assertIn('fields["issuetype"] = {"name": "Task"}', source)
        self.assertIn("retrying with 'Task'", source)

    def test_defect_auth_accepts_password_and_cookie_shapes(self) -> None:
        """Target policy is PAT-only; runtime auth UI still advertises other forms."""
        source = _jira_auth_source()
        self.assertIn("帳號密碼", source)
        self.assertIn("JSESSIONID", source)
        # Domain policy already rejects non-PAT — fix not wired to handlers.
        self.assertEqual(
            classify_credential_input(TEST_ONLY_PASSWORD_SHAPE),
            CredentialInputKind.REJECTED_PASSWORD,
        )
        self.assertEqual(
            classify_credential_input(TEST_ONLY_COOKIE_SHAPE),
            CredentialInputKind.REJECTED_COOKIE,
        )
        self.assertEqual(
            classify_credential_input(TEST_ONLY_PAT),
            CredentialInputKind.PAT,
        )

    def test_defect_batch_worker_uses_raw_create_task(self) -> None:
        source = _core_source()
        self.assertIn("asyncio.create_task(batch_worker())", source)

    def test_defect_max_batch_constant(self) -> None:
        self.assertEqual(MAX_BATCH_SIZE, 20)


class UserStoreMemoryDiskDefectTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_write_failure_keeps_memory_and_disk_aligned(self) -> None:
        """P2-G: failed disk write must not leave credentials only in memory."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            store = UserStore(path)
            await store.initialize()
            credentials = JiraCredentials(
                jira_username="user",
                jira_display_name="User",
                jira_pat=TEST_ONLY_PAT,
            )

            def boom(creds: dict) -> None:
                raise OSError("disk full")

            with patch.object(store, "_write_store", side_effect=boom):
                with self.assertRaises(OSError):
                    await store.store(7, credentials)

            in_memory = await store.get(7)
            self.assertIsNone(
                in_memory,
                "memory must not retain credentials after a failed durable write",
            )
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                self.assertNotIn(TEST_ONLY_PAT, raw)


class TargetInvariantPlaceholders(unittest.TestCase):
    """Invariants that domain already enforces; runtime cutover still pending."""

    def test_target_private_chat_policy_exists(self) -> None:
        self.assertTrue(is_private_chat("private"))
        self.assertFalse(is_private_chat("group"))

    def test_target_bound_callback_grammar_exists(self) -> None:
        from dztgbot.domain.callbacks import (
            CallbackAction,
            encode_callback_data,
            generate_opaque_token,
        )

        token = generate_opaque_token()
        encoded = encode_callback_data(CallbackAction.CONFIRM, token)
        parsed = parse_callback_data(encoded)
        self.assertEqual(parsed.action, CallbackAction.CONFIRM)

    @unittest.expectedFailure
    def test_target_runtime_handlers_use_bound_callbacks(self) -> None:
        """Fails until Phase 5/6 wires j1 tokens into core/ui handlers."""
        source = _core_source()
        self.assertIn("j1:cfm:", source)
        self.assertNotIn('callback_data="jira_confirm"', source)

    @unittest.expectedFailure
    def test_target_runtime_auth_is_pat_only_copy(self) -> None:
        """Fails until Phase 5 removes password/cookie prompts from jira_auth."""
        source = _jira_auth_source()
        self.assertNotIn("帳號密碼", source)
        self.assertNotIn("JSESSIONID", source)

    @unittest.expectedFailure
    def test_target_no_silent_issuetype_substitution(self) -> None:
        """Fails until Phase 4 removes Task fallback in jira_client."""
        source = _jira_client_source()
        self.assertNotIn("retrying with 'Task'", source)

    def test_target_user_store_cow_on_write_failure(self) -> None:
        """P2-G landed: store builds a snapshot and swaps memory only after write."""
        import inspect

        source = inspect.getsource(UserStore.store)
        # Must not assign into self._credentials before the durable write call.
        assign_idx = source.find("self._credentials =")
        write_idx = source.find("_write_store")
        self.assertGreater(write_idx, 0)
        self.assertGreater(assign_idx, 0)
        self.assertLess(
            write_idx,
            assign_idx,
            "copy-on-write requires durable write before memory swap",
        )



if __name__ == "__main__":
    unittest.main()
