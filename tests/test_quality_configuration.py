"""Static contracts for the incremental, offline Phase 8 quality gate."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class DependencySeparationTests(unittest.TestCase):
    def test_runtime_and_development_dependencies_are_exactly_pinned(self) -> None:
        runtime = _read("requirements.txt")
        development = _read("requirements-dev.txt")

        for document in (runtime, development):
            dependency_lines = [
                line.split("#", 1)[0].strip()
                for line in document.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "-r"))
            ]
            self.assertTrue(dependency_lines)
            self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line) for line in dependency_lines))

        self.assertIn("-r requirements.txt", development)
        self.assertNotRegex(runtime, r"(?mi)^(ruff|mypy|coverage)==")
        self.assertRegex(development, r"(?m)^ruff==")
        self.assertRegex(development, r"(?m)^mypy==")
        self.assertRegex(development, r"(?m)^coverage==")


class StaticToolConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.configuration = tomllib.loads(_read("pyproject.toml"))

    def test_ruff_targets_python_312_and_correctness_rules(self) -> None:
        ruff = self.configuration["tool"]["ruff"]
        self.assertEqual("py312", ruff["target-version"])
        selected = set(ruff["lint"]["select"])
        self.assertTrue({"E9", "F", "B", "ASYNC"}.issubset(selected))

    def test_mypy_is_strict_and_excludes_legacy_facades(self) -> None:
        mypy = self.configuration["tool"]["mypy"]
        self.assertIs(mypy["strict"], True)
        self.assertEqual("3.12", mypy["python_version"])
        scopes = set(mypy["files"])
        self.assertEqual(
            {
                "src/dztgbot/domain",
                "src/dztgbot/services",
                "src/dztgbot/infrastructure",
                "src/dztgbot/ui",
                "src/dztgbot/__main__.py",
            },
            scopes,
        )
        self.assertFalse(any(name in " ".join(scopes) for name in ("core.py", "analysis.py", "jira_client.py")))

    def test_coverage_is_branch_aware_with_incremental_floor(self) -> None:
        coverage = self.configuration["tool"]["coverage"]
        self.assertIs(coverage["run"]["branch"], True)
        self.assertEqual(["dztgbot"], coverage["run"]["source"])
        self.assertEqual(75, coverage["report"]["fail_under"])


class WorkflowConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = _read(".github/workflows/quality.yml")

    def test_runner_python_dependency_and_shell_gates_are_fixed(self) -> None:
        self.assertIn("runs-on: ubuntu-24.04", self.workflow)
        self.assertIn('python-version: "3.12"', self.workflow)
        self.assertIn("--requirement requirements-dev.txt", self.workflow)
        self.assertIn("python -m pip check", self.workflow)
        self.assertIn("python -m compileall -q src tests", self.workflow)
        self.assertIn("shellcheck scripts/deploy.sh", self.workflow)

    def test_lint_and_type_commands_use_the_checked_in_configuration(self) -> None:
        self.assertIn("python -m ruff check", self.workflow)
        self.assertIn("src/dztgbot/domain", self.workflow)
        self.assertIn("src/dztgbot/services", self.workflow)
        self.assertIn("src/dztgbot/infrastructure", self.workflow)
        self.assertIn("src/dztgbot/ui", self.workflow)
        self.assertIn("src/dztgbot/__main__.py", self.workflow)
        self.assertIn("run: python -m mypy", self.workflow)

    def test_offline_unit_and_focused_branch_gates_are_explicit(self) -> None:
        self.assertIn("python -m coverage run -m unittest discover -s tests -v", self.workflow)
        self.assertIn("--fail-under=90", self.workflow)
        self.assertIn("--fail-under=75", self.workflow)
        for critical_file in (
            "domain/fsm.py",
            "domain/callbacks.py",
            "domain/policy.py",
            "services/callback_service.py",
            "services/submission_service.py",
            "infrastructure/persistence/workflow_sqlite.py",
        ):
            self.assertIn(critical_file, self.workflow)

    def test_ci_contains_no_live_service_or_deployment_mutation(self) -> None:
        lowered = self.workflow.lower()
        for forbidden in (
            "jira create",
            "jira update",
            "telegram_bot_token",
            "gemini_api_key",
            "systemctl",
            "nmcli",
            "scripts/deploy.sh --",
        ):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
