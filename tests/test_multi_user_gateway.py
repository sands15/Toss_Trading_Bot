from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_gateway_module():
    module_path = Path(__file__).resolve().parents[1] / "ops" / "multi_user_gateway.py"
    spec = importlib.util.spec_from_file_location("multi_user_gateway", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_slugify_keeps_user_container_names_safe() -> None:
    gateway = _load_gateway_module()

    assert gateway.slugify("Alice Kim") == "alice-kim"
    assert gateway.slugify(" Bob_01 ") == "bob_01"
    assert gateway.slugify("!!!") == "user"


def test_registry_port_and_ip_mapping_are_stable(tmp_path: Path) -> None:
    gateway = _load_gateway_module()
    registry_path = tmp_path / "registry.json"

    registry = gateway.load_registry(registry_path)
    first_port = gateway.next_available_port(registry)
    slug = gateway.unique_slug(registry, "Alice")
    registry["users"][slug] = {"slug": slug, "port": first_port}
    registry["ip_map"]["100.64.0.10"] = slug
    gateway.save_registry(registry_path, registry)

    loaded = gateway.load_registry(registry_path)
    assert loaded["ip_map"]["100.64.0.10"] == "alice"
    assert loaded["users"]["alice"]["port"] == gateway.DEFAULT_FIRST_PORT
    assert gateway.next_available_port(loaded) == gateway.DEFAULT_FIRST_PORT + 1
    assert gateway.unique_slug(loaded, "Alice") == "alice-2"


def test_setup_page_contains_required_first_user_fields() -> None:
    gateway = _load_gateway_module()
    html = gateway.setup_page("100.64.0.10").decode("utf-8")

    assert "처음 접속한 사용자 설정" in html
    assert 'name="display_name"' in html
    assert 'name="client_id"' in html
    assert 'name="client_secret"' in html
    assert 'name="account_seq"' in html
    assert gateway.SETUP_CONFIRMATION in html


def test_create_user_rolls_back_registry_when_container_creation_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = _load_gateway_module()

    def fail_container(_self, _user):
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(gateway.UserGateway, "ensure_container", fail_container)
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
    )
    manager = gateway.UserGateway(config)

    try:
        manager.create_user(
            client_ip="100.64.0.10",
            display_name="Alice",
            account_seq="7",
            client_id="client-id",
            client_secret="client-secret",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("container creation unexpectedly succeeded")

    registry = gateway.load_registry(config.registry_path)
    assert registry["ip_map"] == {}
    assert registry["users"] == {}


def test_create_user_maps_ip_only_after_container_creation_succeeds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = _load_gateway_module()

    monkeypatch.setattr(gateway.UserGateway, "ensure_container", lambda _self, _user: None)
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
    )
    manager = gateway.UserGateway(config)

    user = manager.create_user(
        client_ip="100.64.0.10",
        display_name="Alice",
        account_seq="7",
        client_id="client-id",
        client_secret="client-secret",
    )

    registry = gateway.load_registry(config.registry_path)
    assert registry["ip_map"]["100.64.0.10"] == user["slug"]
    assert registry["users"][user["slug"]]["status"] == "running"
    assert manager.user_for_ip("100.64.0.10")["slug"] == user["slug"]


def test_create_user_rejects_non_numeric_account_sequence(tmp_path: Path) -> None:
    gateway = _load_gateway_module()
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
    )
    manager = gateway.UserGateway(config)

    try:
        manager.create_user(
            client_ip="100.64.0.10",
            display_name="Alice",
            account_seq="abc",
            client_id="client-id",
            client_secret="client-secret",
        )
    except ValueError as exc:
        assert "digits only" in str(exc)
    else:
        raise AssertionError("invalid account sequence unexpectedly succeeded")
