"""Deterministic architecture dependency tests for DZTGBot.

Scans Python modules under src/dztgbot using standard library AST to enforce:
1. Domain layer purity (no services, infrastructure, UI, Telegram, httpx, google-genai, sqlite3, or pydantic).
2. Services layer purity (no UI, concrete infrastructure adapters, Telegram, httpx, google-genai, sqlite3, or pydantic).
3. Infrastructure layer isolation (no Telegram API calls or UI imports).
4. Provider SDK exception leakage prevention in domain and services layers.
5. Absolute prevention of package import cycles across clean architecture layers.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
DZTBOT_DIR = SRC_ROOT / "dztgbot"


class TestArchitectureDependencies(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DZTBOT_DIR.exists():
            raise unittest.SkipTest(f"Directory {DZTBOT_DIR} does not exist.")
        cls.modules = cls._discover_and_parse_modules()

    @classmethod
    def _discover_and_parse_modules(cls) -> dict[str, dict]:
        """Discovers all .py files under dztgbot/ and parses their AST and imports."""
        parsed_modules = {}

        for root, _, files in os.walk(DZTBOT_DIR):
            for file in files:
                if not file.endswith(".py"):
                    continue

                full_path = Path(root) / file
                rel_path = full_path.relative_to(SRC_ROOT)

                # Convert path to module name: e.g. dztgbot/domain/models.py -> dztgbot.domain.models
                parts = list(rel_path.with_suffix("").parts)
                is_init = parts[-1] == "__init__"
                if is_init:
                    parts.pop()
                module_name = ".".join(parts)

                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()

                try:
                    tree = ast.parse(content, filename=str(full_path))
                except SyntaxError as err:
                    raise RuntimeError(f"Syntax error parsing {full_path}: {err}") from err

                direct_imports = []
                imported_symbols = []
                resolved_internal_imports = set()

                package_parts = parts if is_init else parts[:-1]

                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            direct_imports.append(alias.name)
                            target = alias.name
                            if target.startswith("dztgbot.") or target.startswith("domain.") or target.startswith("services.") or target.startswith("infrastructure."):
                                resolved_internal_imports.add(target)

                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        names = [alias.name for alias in node.names]
                        imported_symbols.extend(names)

                        if node.level > 0:
                            # Relative import
                            if len(package_parts) >= node.level:
                                base_parts = package_parts[: len(package_parts) - node.level + 1]
                                resolved = ".".join(base_parts)
                                if mod:
                                    resolved += f".{mod}"
                                resolved_internal_imports.add(resolved)
                        else:
                            if mod:
                                direct_imports.append(mod)
                                if mod.startswith("dztgbot.") or mod.startswith("domain.") or mod.startswith("services.") or mod.startswith("infrastructure."):
                                    resolved_internal_imports.add(mod)
                                for n in names:
                                    sub_mod = f"{mod}.{n}"
                                    if sub_mod.startswith("dztgbot."):
                                        resolved_internal_imports.add(sub_mod)

                parsed_modules[module_name] = {
                    "file_path": full_path,
                    "module_name": module_name,
                    "direct_imports": direct_imports,
                    "imported_symbols": imported_symbols,
                    "resolved_internal_imports": resolved_internal_imports,
                }

        return parsed_modules

    def test_domain_layer_purity(self) -> None:
        """Enforces that dztgbot.domain has zero imports of services, infrastructure, UI, Telegram, or provider SDKs."""
        forbidden_prefixes = (
            "dztgbot.services",
            "services",
            "dztgbot.infrastructure",
            "infrastructure",
            "dztgbot.ui",
            "ui",
            "telegram",
            "httpx",
            "google",
            "pydantic",
            "sqlite3",
        )

        domain_modules = [m for name, m in self.modules.items() if name == "dztgbot.domain" or name.startswith("dztgbot.domain.")]
        self.assertTrue(len(domain_modules) > 0, "No domain modules discovered.")

        violations = []
        for mod in domain_modules:
            for imp in mod["direct_imports"]:
                if any(imp == prefix or imp.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append(f"{mod['module_name']} imports forbidden dependency '{imp}'")

        self.assertEqual([], violations, f"Domain layer isolation violations found:\n" + "\n".join(violations))

    def test_services_layer_purity(self) -> None:
        """Enforces that dztgbot.services has zero imports of UI, concrete infrastructure adapters, Telegram, or provider SDKs."""
        forbidden_prefixes = (
            "dztgbot.ui",
            "ui",
            "dztgbot.infrastructure",
            "infrastructure",
            "telegram",
            "httpx",
            "google",
            "pydantic",
            "sqlite3",
        )

        service_modules = [m for name, m in self.modules.items() if name == "dztgbot.services" or name.startswith("dztgbot.services.")]
        self.assertTrue(len(service_modules) > 0, "No service modules discovered.")

        violations = []
        for mod in service_modules:
            for imp in mod["direct_imports"]:
                if any(imp == prefix or imp.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append(f"{mod['module_name']} imports forbidden dependency '{imp}'")

        self.assertEqual([], violations, f"Services layer isolation violations found:\n" + "\n".join(violations))

    def test_infrastructure_layer_never_imports_telegram_or_ui(self) -> None:
        """Enforces that dztgbot.infrastructure never imports Telegram or UI modules."""
        forbidden_prefixes = (
            "dztgbot.ui",
            "ui",
            "telegram",
        )

        infra_modules = [m for name, m in self.modules.items() if name == "dztgbot.infrastructure" or name.startswith("dztgbot.infrastructure.")]
        self.assertTrue(len(infra_modules) > 0, "No infrastructure modules discovered.")

        violations = []
        for mod in infra_modules:
            for imp in mod["direct_imports"]:
                if any(imp == prefix or imp.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append(f"{mod['module_name']} imports forbidden presentation dependency '{imp}'")

        self.assertEqual([], violations, f"Infrastructure layer violations found:\n" + "\n".join(violations))

    def test_ui_layer_never_imports_infrastructure_or_provider_sdks(self) -> None:
        """Enforces that dztgbot.ui (when present) never imports concrete infrastructure adapters or provider SDKs."""
        ui_modules = [m for name, m in self.modules.items() if name == "dztgbot.ui" or name.startswith("dztgbot.ui.")]
        if not ui_modules:
            # UI module does not exist yet (Phase 5 cutover target); skip gracefully without false error
            return

        forbidden_prefixes = (
            "dztgbot.infrastructure",
            "infrastructure",
            "httpx",
            "google",
            "sqlite3",
        )

        violations = []
        for mod in ui_modules:
            for imp in mod["direct_imports"]:
                if any(imp == prefix or imp.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append(f"{mod['module_name']} imports forbidden dependency '{imp}'")

        self.assertEqual([], violations, f"UI layer isolation violations found:\n" + "\n".join(violations))

    def test_no_provider_sdk_exception_leaks_in_domain_or_services(self) -> None:
        """Ensures provider SDK exception symbols are not imported into domain or services layers."""
        forbidden_exceptions = {
            "HTTPError",
            "HTTPStatusError",
            "RequestError",
            "ConnectTimeout",
            "ReadTimeout",
            "TimeoutException",
            "APIError",
            "OperationalError",
            "IntegrityError",
            "DatabaseError",
            "ValidationError",
        }

        target_modules = [
            m for name, m in self.modules.items()
            if name.startswith("dztgbot.domain") or name.startswith("dztgbot.services")
        ]

        violations = []
        for mod in target_modules:
            for sym in mod["imported_symbols"]:
                if sym in forbidden_exceptions:
                    violations.append(f"{mod['module_name']} imports provider exception symbol '{sym}'")

        self.assertEqual([], violations, f"Provider exception leakage violations found:\n" + "\n".join(violations))

    def test_no_package_import_cycles_in_clean_architecture(self) -> None:
        """Verifies that no package import cycles exist among domain, services, and infrastructure modules."""
        clean_modules = {
            name: mod for name, mod in self.modules.items()
            if name.startswith("dztgbot.domain") or name.startswith("dztgbot.services") or name.startswith("dztgbot.infrastructure")
        }

        # Normalize target dependencies to existing module names
        graph: dict[str, set[str]] = {name: set() for name in clean_modules}

        for name, mod in clean_modules.items():
            for target in mod["resolved_internal_imports"]:
                # Map short names (e.g. domain.models -> dztgbot.domain.models)
                norm_target = target
                if target.startswith("domain.") or target == "domain":
                    norm_target = "dztgbot." + target
                elif target.startswith("services.") or target == "services":
                    norm_target = "dztgbot." + target
                elif target.startswith("infrastructure.") or target == "infrastructure":
                    norm_target = "dztgbot." + target

                # Find matching module in graph
                for candidate in clean_modules:
                    if candidate == norm_target or candidate.startswith(norm_target + "."):
                        if candidate != name:
                            graph[name].add(candidate)

        # Detect cycles using Tarjan's Strongly Connected Components algorithm
        index = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        sccs: list[list[str]] = []

        def strongconnect(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlink[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for successor in graph.get(node, ()):
                if successor not in indices:
                    strongconnect(successor)
                    lowlink[node] = min(lowlink[node], lowlink[successor])
                elif successor in on_stack:
                    lowlink[node] = min(lowlink[node], indices[successor])

            if lowlink[node] == indices[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    scc.append(w)
                    if w == node:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        for node in graph:
            if node not in indices:
                strongconnect(node)

        self.assertEqual([], sccs, f"Import cycles detected among clean architecture packages:\n{sccs}")


if __name__ == "__main__":
    unittest.main()
