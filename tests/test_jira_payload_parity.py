from __future__ import annotations

import unittest

from dztgbot.domain.models import JiraTaskTemplate
from dztgbot.infrastructure.jira_gateway import canonical_issue_fields, canonical_request_hash, diff_issue_fields
from dztgbot.services.submission_service import canonical_request_hash as service_hash, complete_template_diff


class JiraPayloadParityTests(unittest.TestCase):
    def test_create_update_hash_and_diff_share_all_fields(self) -> None:
        before = JiraTaskTemplate("BOT", "Task", "old", "body", "Medium", ("a",), ("c",), "", ["one"])
        after = JiraTaskTemplate("BOT", "Bug", "new", "changed", "High", ("b",), ("d",), "alice", ["two"])
        self.assertEqual(canonical_request_hash(after), service_hash(after))
        self.assertEqual(diff_issue_fields(before, after), complete_template_diff(before, after))
        self.assertEqual(set(canonical_issue_fields(after).fields), {"project", "issuetype", "summary", "description", "priority", "labels", "components", "assignee"})


if __name__ == "__main__":
    unittest.main()
