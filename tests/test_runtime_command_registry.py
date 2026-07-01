from __future__ import annotations

import ast
from pathlib import Path

from hyperliquid_trading_agent.app.runtime import WORKERS
from hyperliquid_trading_agent.app.runtime_commands import COMMAND_REGISTRY, COMMAND_SPECS, command_registry_json

COMMAND_SOURCE_FILES = [
    Path("hyperliquid_trading_agent/app/main.py"),
    Path("hyperliquid_trading_agent/app/engine/routes.py"),
    Path("hyperliquid_trading_agent/app/hip4/routes.py"),
    Path("hyperliquid_trading_agent/app/orchestration/routes.py"),
    Path("hyperliquid_trading_agent/app/world_model/routes.py"),
    Path("hyperliquid_trading_agent/app/newswire/gateway.py"),
]


def _literal(value: ast.AST) -> str | None:
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _enqueued_command_types() -> set[str]:
    commands: set[str] = set()
    for path in COMMAND_SOURCE_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if func_name == "enqueue_worker_command":
                for keyword in node.keywords:
                    if keyword.arg == "command_type":
                        command_type = _literal(keyword.value)
                        if command_type:
                            commands.add(command_type)
            if func_name == "_enqueue_command" and node.args:
                command_type = _literal(node.args[0])
                if command_type:
                    commands.add(command_type)
                for keyword in node.keywords:
                    if keyword.arg == "command_type":
                        command_type = _literal(keyword.value)
                        if command_type:
                            commands.add(command_type)
    return commands


def test_all_enqueued_worker_commands_are_registered() -> None:
    assert _enqueued_command_types() - set(COMMAND_REGISTRY) == set()


def test_command_registry_is_unique_and_json_serializable() -> None:
    assert len(COMMAND_REGISTRY) == len(COMMAND_SPECS)
    payload = command_registry_json()
    assert len(payload) == len(COMMAND_SPECS)
    assert all(item["command_type"] and item["target_role"] and item["source_endpoints"] for item in payload)


def test_registered_commands_have_worker_handlers() -> None:
    missing: list[str] = []
    noop: list[str] = []
    for spec in COMMAND_SPECS:
        worker_cls = WORKERS.get(spec.target_role)
        if worker_cls is None:
            missing.append(f"{spec.command_type}: missing worker for {spec.target_role.value}")
            continue
        if not hasattr(worker_cls, spec.handler_name):
            missing.append(f"{spec.command_type}: {worker_cls.__name__}.{spec.handler_name}")
        if spec.handler_name == "_accepted_noop":
            noop.append(spec.command_type)
    assert missing == []
    assert noop == []
