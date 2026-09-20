import ast
from pathlib import Path


def test_replay_package_has_no_discovery_or_provider_dependency() -> None:
    replay_root = Path("src/computer_use/replay")
    for path in replay_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any("discovery" in name or "llm" in name for name in imports), (path, imports)
