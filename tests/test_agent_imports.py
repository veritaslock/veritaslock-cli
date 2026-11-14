"""The agent imports only from vl.lib, never from vl.commands (vl-agent-spec.md §8 item 8)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).parent.parent / "src" / "vl" / "agent"


def test_no_agent_module_imports_vl_commands() -> None:
    for path in AGENT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not name.startswith("vl.commands") and name != "vl.app", (path.name, name)


def test_importing_the_agent_loads_neither_vl_commands_nor_kafka() -> None:
    code = ("import sys, vl.agent.runner; "
            "bad = [m for m in sys.modules if m.startswith(('vl.commands', 'confluent_kafka')) or m == 'vl.app']; "
            "print(bad); sys.exit(1 if bad else 0)")
    subprocess.run([sys.executable, "-c", code], check=True)
