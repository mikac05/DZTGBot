"""Private-only admin command tests (Phase 5 Task P5-G).

Ensures /rules, /setrules, /vpn, and /vpnstart require numeric admin
authorization in a private chat and never disclose rules or VPN state in groups.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from dztgbot.admin import build_admin_handlers
from dztgbot.domain.policy import DenialCode, user_message_for_denial
from dztgbot.rules import RulesStore
from dztgbot.vpn import VpnState


class _FakeVpnManager:
    def __init__(self) -> None:
        self.status_calls = 0
        self.start_calls = 0

    async def status(self) -> SimpleNamespace:
        self.status_calls += 1
        return SimpleNamespace(
            state=VpnState.UP if hasattr(VpnState, "UP") else "up",
            message="VPN is up (TEST_ONLY)",
        )

    async def start(self) -> SimpleNamespace:
        self.start_calls += 1
        return SimpleNamespace(
            state=VpnState.UP if hasattr(VpnState, "UP") else "up",
            message="VPN start accepted (TEST_ONLY)",
        )


def _user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, full_name="User", username="u")


def _private_chat(chat_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="private")


def _group_chat(chat_id: int = -99) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title="Team")


def _update(
    *,
    user_id: int,
    chat: SimpleNamespace,
    text: str,
) -> tuple[MagicMock, MagicMock]:
    user = _user(user_id)
    message = MagicMock()
    message.text = text
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    return update, message


class AdminPrivateOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        rules_path = Path(self._tmp.name) / "rules.txt"
        rules_path.write_text("SECRET_RUNTIME_RULES_BODY\nline2\n", encoding="utf-8")
        self.rules_store = RulesStore(rules_path)
        await self.rules_store.initialize()
        self.vpn = _FakeVpnManager()
        self.admins = frozenset({1001, 2002})
        handlers = build_admin_handlers(self.rules_store, self.admins, self.vpn)
        self.by_command = {
            next(iter(h.commands)): h.callback  # type: ignore[attr-defined]
            for h in handlers
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_private_admin_can_view_rules(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_private_chat(),
            text="/rules",
        )
        await self.by_command["rules"](update, MagicMock())
        message.reply_text.assert_awaited()
        body = message.reply_text.await_args.args[0]
        self.assertIn("SECRET_RUNTIME_RULES_BODY", body)

    async def test_group_admin_cannot_view_rules(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_group_chat(),
            text="/rules",
        )
        await self.by_command["rules"](update, MagicMock())
        message.reply_text.assert_awaited()
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("SECRET_RUNTIME_RULES_BODY", body)
        self.assertNotIn("line2", body)

    async def test_group_non_admin_cannot_view_rules(self) -> None:
        update, message = _update(
            user_id=9,
            chat=_group_chat(),
            text="/rules",
        )
        await self.by_command["rules"](update, MagicMock())
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("SECRET_RUNTIME_RULES_BODY", body)

    async def test_private_non_admin_is_denied_without_rules(self) -> None:
        update, message = _update(
            user_id=9,
            chat=_private_chat(chat_id=9),
            text="/rules",
        )
        await self.by_command["rules"](update, MagicMock())
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_ADMIN))
        self.assertNotIn("SECRET_RUNTIME_RULES_BODY", body)

    async def test_group_setrules_does_not_mutate_or_disclose(self) -> None:
        before = await self.rules_store.current_rules()
        update, message = _update(
            user_id=1001,
            chat=_group_chat(),
            text="/setrules NEW_RULES_SHOULD_NOT_APPLY",
        )
        await self.by_command["setrules"](update, MagicMock())
        after = await self.rules_store.current_rules()
        self.assertEqual(before, after)
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("NEW_RULES_SHOULD_NOT_APPLY", body)
        self.assertNotIn("SECRET_RUNTIME_RULES_BODY", body)

    async def test_private_admin_setrules_works(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_private_chat(),
            text="/setrules replacement rules body",
        )
        await self.by_command["setrules"](update, MagicMock())
        self.assertEqual(await self.rules_store.current_rules(), "replacement rules body")
        body = message.reply_text.await_args.args[0]
        self.assertIn("updated", body.lower())

    async def test_group_vpn_status_does_not_call_manager(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_group_chat(),
            text="/vpn",
        )
        await self.by_command["vpn"](update, MagicMock())
        self.assertEqual(self.vpn.status_calls, 0)
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("VPN is up", body)
        self.assertNotIn("TEST_ONLY", body)

    async def test_group_vpnstart_does_not_call_manager(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_group_chat(),
            text="/vpnstart",
        )
        await self.by_command["vpnstart"](update, MagicMock())
        self.assertEqual(self.vpn.start_calls, 0)
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_PRIVATE_CHAT))
        self.assertNotIn("VPN start", body)

    async def test_private_admin_vpn_status_works(self) -> None:
        update, message = _update(
            user_id=1001,
            chat=_private_chat(),
            text="/vpn",
        )
        await self.by_command["vpn"](update, MagicMock())
        self.assertEqual(self.vpn.status_calls, 1)
        body = message.reply_text.await_args.args[0]
        self.assertIn("VPN is up", body)

    async def test_private_non_admin_vpn_denied(self) -> None:
        update, message = _update(
            user_id=9,
            chat=_private_chat(chat_id=9),
            text="/vpn",
        )
        await self.by_command["vpn"](update, MagicMock())
        self.assertEqual(self.vpn.status_calls, 0)
        body = message.reply_text.await_args.args[0]
        self.assertEqual(body, user_message_for_denial(DenialCode.NOT_ADMIN))


if __name__ == "__main__":
    unittest.main()
