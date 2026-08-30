from __future__ import annotations

import ast
import http.client
import os
import plistlib
import re
import socket
from pathlib import Path
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TOSS_ORIGIN = "https://openapi.tossinvest.com"
ORDER_PATHS = {
    "_GENERAL_ORDER_PATH": "/api/v1/orders",
    "_CONDITIONAL_ORDER_PATH": "/api/v1/conditional-orders",
}
SHADOW_PLISTS = {
    "com.sands15.toss-intraday-shadow.plist.example": (
        "com.sands15.toss-intraday-shadow"
    ),
    "com.sands15.toss-market-stream-shadow.plist.example": (
        "com.sands15.toss-market-stream-shadow"
    ),
    "com.sands15.toss-discord-approval.plist.example": (
        "com.sands15.toss-discord-approval"
    ),
    "com.sands15.toss-news-shadow.plist.example": "com.sands15.toss-news-shadow",
    "com.sands15.toss-shadow-watchdog.plist.example": (
        "com.sands15.toss-shadow-watchdog"
    ),
}
SENSITIVE_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|WEBHOOK|CREDENTIAL|"
    r"AUTHORIZATION|ACCOUNT|TOSS|DISCORD)",
    re.IGNORECASE,
)


@pytest.fixture(scope="module", autouse=True)
def deny_real_network_for_release_gate() -> dict[str, int]:
    """Make an accidental HTTP, WebSocket, DNS, or socket call fail locally."""

    counters = {
        "real_http_calls": 0,
        "real_ws_connections": 0,
        "dns_calls": 0,
        "socket_calls": 0,
    }
    patch = pytest.MonkeyPatch()

    def deny_http(*args: object, **kwargs: object) -> None:
        counters["real_http_calls"] += 1
        raise AssertionError("non-live release gate attempted a real HTTP call")

    def deny_ws(*args: object, **kwargs: object) -> None:
        counters["real_ws_connections"] += 1
        raise AssertionError("non-live release gate attempted a real WebSocket connection")

    def deny_dns(*args: object, **kwargs: object) -> None:
        counters["dns_calls"] += 1
        raise AssertionError("non-live release gate attempted DNS resolution")

    def deny_socket(*args: object, **kwargs: object) -> None:
        counters["socket_calls"] += 1
        raise AssertionError("non-live release gate attempted a socket connection")

    patch.setattr(socket, "getaddrinfo", deny_dns)
    patch.setattr(socket, "create_connection", deny_socket)
    patch.setattr(http.client.HTTPConnection, "connect", deny_http)
    patch.setattr(http.client.HTTPSConnection, "connect", deny_http)
    patch.setattr(urllib_request, "urlopen", deny_http)
    patch.setattr(urllib_request.OpenerDirector, "open", deny_http)

    # Patch the optional default WebSocket dependency when it is installed.
    # UrllibTossTransport terminates at OpenerDirector.open, already denied
    # above; avoiding a turtle_bot import also keeps this static gate from
    # accidentally resolving an unrelated editable checkout.
    try:
        from websockets.sync import client as websocket_client
    except ImportError:
        websocket_client = None
    if websocket_client is not None:
        patch.setattr(websocket_client, "connect", deny_ws)

    try:
        yield counters
    finally:
        patch.undo()

    assert counters == {
        "real_http_calls": 0,
        "real_ws_connections": 0,
        "dns_calls": 0,
        "socket_calls": 0,
    }


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        urls.extend(
            re.findall(r"\b(?:https?|wss?)://[^\s'\"<>]+", node.value)
        )
    return urls


def test_production_cli_does_not_dispatch_intraday_live_runtime() -> None:
    for relative in ("src/turtle_bot/cli.py", "src/turtle_bot/operations.py"):
        path = ROOT / relative
        source = path.read_text(encoding="utf-8")
        tree = _tree(path)
        imported = [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("intraday_live")
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.endswith("intraday_live") for alias in node.names)
            )
        ]
        constructed = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "IntradayLiveRuntime"
        ]
        assert "intraday_live" not in source, relative
        assert "IntradayLiveRuntime" not in source, relative
        assert not imported, f"{relative} imports intraday_live"
        assert not constructed, f"{relative} constructs IntradayLiveRuntime"


def test_intraday_simulator_never_constructs_a_default_network_adapter() -> None:
    tree = _tree(ROOT / "src/turtle_bot/intraday_live.py")
    forbidden = {
        "TossClient",
        "UrllibTossTransport",
        "TossLiveBrokerAdapter",
        "TossConditionalOrderAdapter",
    }
    calls = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if (name := _call_name(node)) in forbidden
    }
    assert not calls


def test_paper_ledger_has_no_broker_network_or_live_runtime_dependency() -> None:
    path = ROOT / "src/turtle_bot/intraday_paper.py"
    tree = _tree(path)
    forbidden_roots = {
        "http",
        "requests",
        "socket",
        "urllib",
        "websockets",
        "turtle_bot.intraday_live",
        "turtle_bot.toss_client",
        "turtle_bot.toss_live_adapter",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {
        name
        for name in imported
        if any(name == root or name.startswith(f"{root}.") for root in forbidden_roots)
    }


def test_checked_in_yaml_keeps_live_ordering_hard_off() -> None:
    paths = sorted((ROOT / "config").glob("*.y*ml"))
    assert paths
    intraday_templates: set[str] = set()
    for path in paths:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), path.name
        toss = payload.get("toss")
        live = payload.get("live")
        assert isinstance(toss, dict), path.name
        assert isinstance(live, dict), path.name
        assert toss.get("live_enabled") is False, path.name
        assert live.get("emergency_stop") is True, path.name
        assert live.get("allowed_symbols") == [], path.name
        assert toss.get("base_url", TOSS_ORIGIN) == TOSS_ORIGIN, path.name

        strategy = payload.get("strategy")
        if isinstance(strategy, dict) and strategy.get("kind") == "intraday":
            intraday_templates.add(path.name)
            runtime = payload.get("runtime")
            intraday = strategy.get("intraday")
            assert isinstance(runtime, dict), path.name
            assert isinstance(intraday, dict), path.name
            assert runtime.get("mode") == "shadow", path.name
            assert intraday.get("live_execution_enabled") is False, path.name
    assert intraday_templates == {
        "intraday-simulation.example.yaml",
        "intraday.example.yaml",
    }


def test_simulation_manifest_is_exactly_five_shadow_jobs() -> None:
    launchd = ROOT / "ops/launchd"
    paths = {path.name: path for path in launchd.glob("*.plist.example")}
    assert set(paths) == set(SHADOW_PLISTS)

    forbidden_fragments = (
        "intraday_live",
        "live-writer",
        "receipt-consumer",
        "consume-receipt",
        "dashboard",
        "gateway",
        "multi_user_gateway",
    )
    forbidden_env = {
        "TOSS_CLIENT_ID",
        "TOSS_CLIENT_SECRET",
        "TOSS_ACCOUNT_SEQ",
        "ACCOUNT_SEQ",
        "DISCORD_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DISCORD_WEBHOOK_URL",
    }
    secret_env_fragment = re.compile(
        r"(?:CLIENT_ID|CLIENT_SECRET|ACCOUNT_SEQ|BOT_TOKEN|WEBHOOK|ORDER.*CREDENTIAL)",
        re.IGNORECASE,
    )
    for name, expected_label in SHADOW_PLISTS.items():
        with paths[name].open("rb") as handle:
            payload = plistlib.load(handle)
        assert payload["Label"] == expected_label
        arguments = payload.get("ProgramArguments")
        assert isinstance(arguments, list) and len(arguments) == 1
        environment = payload.get("EnvironmentVariables", {})
        assert isinstance(environment, dict)
        assert forbidden_env.isdisjoint(environment)
        assert not [key for key in environment if secret_env_fragment.search(str(key))]
        manifest_text = "\n".join(
            [payload["Label"], *map(str, arguments), *map(str, environment)]
        ).lower()
        assert not any(fragment in manifest_text for fragment in forbidden_fragments)


def test_intraday_runtime_uses_one_canonical_constant_per_order_endpoint() -> None:
    path = ROOT / "src/turtle_bot/intraday_live.py"
    tree = _tree(path)
    assigned: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        value = statement.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assigned[target.id] = value.value

    string_values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    for constant_name, endpoint in ORDER_PATHS.items():
        assert assigned.get(constant_name) == endpoint
        assert string_values.count(endpoint) == 1

    # Adapter modules remain the canonical production endpoint contract.
    assert ORDER_PATHS["_GENERAL_ORDER_PATH"] in (
        ROOT / "src/turtle_bot/toss_live_adapter.py"
    ).read_text(encoding="utf-8")
    assert ORDER_PATHS["_CONDITIONAL_ORDER_PATH"] in (
        ROOT / "src/turtle_bot/toss_conditional.py"
    ).read_text(encoding="utf-8")


def test_fake_intraday_origins_are_reserved_invalid_domains() -> None:
    # The lifecycle suite owns dependency-injected fake transports.  Planner
    # operation tests also contain expected production-origin and rejection
    # fixtures, so treating every URL in those files as a fake is incorrect.
    paths = [ROOT / "tests/test_intraday_live.py"]
    assert paths
    for path in paths:
        for url in _literal_urls(path):
            hostname = urllib_parse.urlsplit(url).hostname
            assert hostname and hostname.endswith(".invalid"), (path.name, url)


def test_gate_runners_scrub_environment_before_starting_pytest() -> None:
    shell = (ROOT / "ops/run-non-live-gate.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "ops/run-non-live-gate.ps1").read_text(encoding="utf-8")
    assert "/usr/bin/env -i" in shell
    assert "sandbox-exec" in shell
    assert "(deny network*)" in shell
    assert "NON_LIVE_GATE=1" in shell
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in shell
    assert "exit 78" in shell
    assert "Get-ChildItem Env:" in powershell
    assert "Remove-Item" in powershell
    assert "NON_LIVE_GATE" in powershell
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in powershell
    runner = (ROOT / "ops/non_live_gate_runner.py").read_text(encoding="utf-8")
    assert "tests/test_intraday_live.py" in runner
    assert "tests/test_intraday_crash_replay.py" in runner
    assert "tests/test_approval_consumer.py" in runner


def test_scrubbed_gate_process_contains_no_sensitive_environment_names() -> None:
    if os.environ.get("NON_LIVE_GATE") != "1":
        pytest.skip("environment contract is checked when run through the gate runner")
    leaked = sorted(name for name in os.environ if SENSITIVE_ENV_NAME.search(name))
    assert leaked == []
