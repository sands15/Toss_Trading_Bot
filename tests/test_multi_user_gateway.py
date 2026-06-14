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


def test_registry_admin_helpers_remove_mappings_without_secrets() -> None:
    gateway = _load_gateway_module()
    registry = {
        "version": 1,
        "next_port": 19001,
        "ip_map": {"100.64.0.10": "alice"},
        "users": {
            "alice": {
                "slug": "alice",
                "display_name": "Alice",
                "client_ip": "100.64.0.10",
                "container_name": "toss-dashboard-alice",
                "port": 19000,
                "client_secret": "should-not-appear",
                "password_hash": "should-not-appear",
            }
        },
    }

    view = gateway.registry_public_view(registry)
    assert "client_secret" not in view["users"]["alice"]
    assert "password_hash" not in view["users"]["alice"]
    assert gateway.unmap_ip(registry, "100.64.0.10") == "alice"
    assert registry["ip_map"] == {}
    registry["ip_map"]["100.64.0.10"] = "alice"
    removed = gateway.delete_user(registry, "alice")
    assert removed["slug"] == "alice"
    assert registry["users"] == {}
    assert registry["ip_map"] == {}


def test_setup_page_contains_required_first_user_fields() -> None:
    gateway = _load_gateway_module()
    html = gateway.setup_page("100.64.0.10", csrf_token="token-123").decode("utf-8")

    assert "처음 접속한 사용자 설정" in html
    assert 'name="csrf_token"' in html
    assert 'value="token-123"' in html
    assert 'name="display_name"' in html
    assert 'name="login_id"' in html
    assert 'name="password"' in html
    assert 'name="client_id"' in html
    assert 'name="client_secret"' in html
    assert 'name="account_seq"' in html
    assert gateway.SETUP_CONFIRMATION in html


def test_setup_csrf_token_must_match_cookie() -> None:
    gateway = _load_gateway_module()

    assert gateway.csrf_is_valid("toss_gateway_setup=abc; other=1", "abc")
    assert not gateway.csrf_is_valid("toss_gateway_setup=abc", "wrong")
    assert not gateway.csrf_is_valid("", "abc")
    assert gateway.cookie_value("a=1; toss_gateway_setup=xyz", "toss_gateway_setup") == "xyz"


def test_password_hash_and_session_cookie_are_verifiable(tmp_path: Path) -> None:
    gateway = _load_gateway_module()
    password_hash = gateway.make_password_hash("correct horse")

    assert gateway.verify_password("correct horse", password_hash)
    assert not gateway.verify_password("wrong password", password_hash)

    secret_path = tmp_path / "session_secret"
    secret = gateway.load_or_create_secret(secret_path)
    assert secret_path.exists()
    assert gateway.load_or_create_secret(secret_path) == secret

    cookie_value = gateway.make_session_cookie_value(secret, "alice", now=100)
    cookie_header = f"toss_gateway_session={cookie_value}"
    assert gateway.session_slug_from_cookie(cookie_header, secret, now=101) == "alice"
    assert gateway.session_slug_from_cookie(cookie_header, b"wrong", now=101) is None
    assert gateway.session_slug_from_cookie(cookie_header, secret, now=100 + gateway.SESSION_TTL_SECONDS + 1) is None


def test_secret_backend_resolves_by_platform(monkeypatch) -> None:
    gateway = _load_gateway_module()

    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    assert gateway.resolve_secret_backend("auto") == "keychain"
    monkeypatch.setattr(gateway.sys, "platform", "win32")
    assert gateway.resolve_secret_backend("auto") == "file"
    assert gateway.resolve_secret_backend("file") == "file"
    assert gateway.resolve_secret_backend("keychain") == "keychain"


def test_keychain_secret_store_uses_security_without_public_secret_echo(monkeypatch) -> None:
    gateway = _load_gateway_module()
    calls = []

    monkeypatch.setattr(gateway.shutil, "which", lambda name: "/usr/bin/security" if name == "security" else None)

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "find-generic-password":
            account = args[args.index("-a") + 1]
            value = "client-id" if account.endswith(gateway.SECRET_CLIENT_ID) else "client-secret"
            return gateway.subprocess.CompletedProcess(args, 0, stdout=value + "\n")
        return gateway.subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    store = gateway.KeychainSecretStore("test-service")

    store.put_user_credentials("alice", client_id="client-id", client_secret="client-secret")
    loaded = store.get_user_credentials("alice")
    store.delete_user_credentials("alice")

    assert loaded == {"TOSS_CLIENT_ID": "client-id", "TOSS_CLIENT_SECRET": "client-secret"}
    assert calls[0][0][:2] == ["security", "add-generic-password"]
    assert calls[0][1]["stdout"] == gateway.subprocess.DEVNULL
    assert calls[0][1]["stderr"] == gateway.subprocess.DEVNULL
    assert any(call[0][1] == "delete-generic-password" for call in calls)


def test_secret_store_imports_existing_env_and_rewrites_placeholder(tmp_path: Path) -> None:
    gateway = _load_gateway_module()

    class MemorySecretStore(gateway.SecretStore):
        backend_name = "memory"

        def __init__(self):
            self.values = {}

        def put_user_credentials(self, user_slug, *, client_id, client_secret):
            self.values[user_slug] = {
                "TOSS_CLIENT_ID": client_id,
                "TOSS_CLIENT_SECRET": client_secret,
            }

        def get_user_credentials(self, user_slug):
            return dict(self.values.get(user_slug, {}))

        def delete_user_credentials(self, user_slug):
            self.values.pop(user_slug, None)

    env_file = tmp_path / ".env"
    env_file.write_text("TOSS_CLIENT_ID=id\nTOSS_CLIENT_SECRET=secret\n", encoding="utf-8")
    store = MemorySecretStore()

    assert store.import_user_env_if_needed("alice", env_file)
    assert store.get_user_credentials("alice") == {
        "TOSS_CLIENT_ID": "id",
        "TOSS_CLIENT_SECRET": "secret",
    }
    rewritten = env_file.read_text(encoding="utf-8")
    assert "Secret backend: memory" in rewritten
    assert "TOSS_CLIENT_SECRET=secret" not in rewritten


def test_setup_content_length_is_limited() -> None:
    gateway = _load_gateway_module()

    assert gateway.parse_content_length("10", max_bytes=20) == 10
    for value in ("abc", "-1", "21"):
        try:
            gateway.parse_content_length(value, max_bytes=20)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid content length unexpectedly passed: {value}")


def test_registration_allowlist_accepts_exact_ips_and_cidr() -> None:
    gateway = _load_gateway_module()

    allowlist = gateway.parse_allowlist("100.64.0.10, 100.80.0.0/16")

    assert gateway.client_ip_is_allowed("100.64.0.10", allowlist)
    assert gateway.client_ip_is_allowed("100.80.1.5", allowlist)
    assert not gateway.client_ip_is_allowed("100.81.1.5", allowlist)
    assert not gateway.client_ip_is_allowed("not-an-ip", allowlist)
    assert gateway.client_ip_is_allowed("203.0.113.10", ())


def test_setup_attempts_are_rate_limited_per_client_ip(tmp_path: Path) -> None:
    gateway = _load_gateway_module()
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
        setup_rate_limit=2,
        setup_rate_window_seconds=60,
    )
    manager = gateway.UserGateway(config)

    assert manager.consume_setup_attempt("100.64.0.10", now=100.0) == (True, 0)
    assert manager.consume_setup_attempt("100.64.0.10", now=110.0) == (True, 0)
    allowed, retry_after = manager.consume_setup_attempt("100.64.0.10", now=120.0)
    assert not allowed
    assert retry_after == 40
    assert manager.consume_setup_attempt("100.64.0.10", now=161.0) == (True, 0)
    assert manager.consume_setup_attempt("100.64.0.11", now=120.0) == (True, 0)


def test_registration_allowed_uses_gateway_allowlist(tmp_path: Path) -> None:
    gateway = _load_gateway_module()
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
        registration_allowlist=("100.64.0.0/24",),
    )
    manager = gateway.UserGateway(config)

    assert manager.registration_allowed("100.64.0.10")
    assert not manager.registration_allowed("100.64.1.10")


def test_gateway_user_lifecycle_commands_update_registry(tmp_path: Path, monkeypatch) -> None:
    gateway = _load_gateway_module()
    registry_path = tmp_path / "registry.json"
    registry = gateway.load_registry(registry_path)
    registry["users"]["alice"] = {
        "slug": "alice",
        "display_name": "Alice",
        "client_ip": "100.64.0.10",
        "container_name": "toss-dashboard-alice",
        "port": 19000,
        "status": "running",
    }
    gateway.save_registry(registry_path, registry)
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=registry_path,
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
    )
    manager = gateway.UserGateway(config)
    commands = []
    removed = []

    monkeypatch.setattr(gateway, "run_command", lambda args, *, cwd: commands.append((args, cwd)))
    monkeypatch.setattr(gateway.UserGateway, "wait_for_user_http", lambda _self, _user: None)
    monkeypatch.setattr(gateway.UserGateway, "ensure_container", lambda _self, _user: commands.append((["ensure"], tmp_path)))
    monkeypatch.setattr(
        gateway.UserGateway,
        "remove_container",
        lambda _self, container_name: removed.append(container_name),
    )

    assert manager.stop_user("alice")["status"] == "stopped"
    assert commands[-1][0] == ["docker", "stop", "toss-dashboard-alice"]
    assert manager.start_user("alice")["status"] == "running"
    assert commands[-1][0] == ["ensure"]
    assert manager.restart_user("alice")["status"] == "running"
    assert commands[-1][0] == ["docker", "restart", "toss-dashboard-alice"]
    assert manager.remove_user_container("alice")["status"] == "container_removed"
    assert removed == ["toss-dashboard-alice"]
    audit_lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert any('"event": "container_stopped"' in line for line in audit_lines)
    assert any('"event": "container_started"' in line for line in audit_lines)
    assert any('"event": "container_restarted"' in line for line in audit_lines)
    assert any('"event": "container_removed"' in line for line in audit_lines)


def test_new_user_container_uses_resource_and_log_limits(tmp_path: Path, monkeypatch) -> None:
    gateway = _load_gateway_module()
    config = gateway.GatewayConfig(
        repo_root=tmp_path,
        registry_path=tmp_path / "registry.json",
        users_root=tmp_path / "users",
        image_name="test-image",
        container_port=8765,
        container_memory="768m",
        container_cpus="1.5",
        container_log_max_size="5m",
        container_log_max_files="2",
    )
    manager = gateway.UserGateway(config)
    user_root = tmp_path / "users" / "alice"
    (user_root / "config").mkdir(parents=True)
    (user_root / "state").mkdir()
    (user_root / "logs").mkdir()
    (user_root / ".env").write_text("TOSS_CLIENT_ID=id\nTOSS_CLIENT_SECRET=secret\n", encoding="utf-8")
    commands = []

    def fake_subprocess_run(args, **_kwargs):
        assert args[:3] == ["docker", "ps", "-a"]
        return gateway.subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_subprocess_run)
    command_envs = []

    def fake_run_command(args, *, cwd, env=None):
        commands.append(args)
        command_envs.append(env or {})

    monkeypatch.setattr(gateway, "run_command", fake_run_command)
    monkeypatch.setattr(gateway.UserGateway, "wait_for_user_http", lambda _self, _user: None)

    manager.ensure_container(
        {
            "slug": "alice",
            "container_name": "toss-dashboard-alice",
            "port": 19000,
        }
    )

    docker_run = commands[-1]
    assert docker_run[:3] == ["docker", "run", "-d"]
    assert docker_run[docker_run.index("--memory") + 1] == "768m"
    assert docker_run[docker_run.index("--cpus") + 1] == "1.5"
    assert "max-size=5m" in docker_run
    assert "max-file=2" in docker_run
    assert "--env-file" not in docker_run
    assert docker_run[docker_run.index("--env") + 1] == "TOSS_CLIENT_ID"
    assert "TOSS_CLIENT_SECRET" in docker_run
    assert command_envs[-1]["TOSS_CLIENT_ID"] == "id"
    assert command_envs[-1]["TOSS_CLIENT_SECRET"] == "secret"


def test_create_user_rolls_back_registry_when_container_creation_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = _load_gateway_module()

    def fail_container(_self, _user):
        raise RuntimeError("docker unavailable")

    removed_containers = []

    monkeypatch.setattr(gateway.UserGateway, "ensure_container", fail_container)
    monkeypatch.setattr(
        gateway.UserGateway,
        "remove_container",
        lambda _self, container_name: removed_containers.append(container_name),
    )
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
            login_id="alice",
            password="correct horse",
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
    assert registry["login_map"] == {}
    assert registry["users"] == {}
    assert removed_containers == ["toss-dashboard-alice"]


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
        login_id="Alice.Login",
        password="correct horse",
        account_seq="7",
        client_id="client-id",
        client_secret="client-secret",
    )

    registry = gateway.load_registry(config.registry_path)
    assert registry["ip_map"]["100.64.0.10"] == user["slug"]
    assert registry["login_map"]["alice.login"] == user["slug"]
    assert registry["users"][user["slug"]]["status"] == "running"
    assert registry["users"][user["slug"]]["secret_backend"] == "file"
    assert "client-id" not in str(registry)
    assert "client-secret" not in str(registry)
    assert "password_hash" in registry["users"][user["slug"]]
    assert manager.user_for_ip("100.64.0.10")["slug"] == user["slug"]
    assert manager.authenticate("alice.login", "correct horse")["slug"] == user["slug"]
    assert manager.authenticate("alice.login", "wrong password") is None
    session_cookie = f"toss_gateway_session={manager.make_session_cookie(user['slug'])}"
    assert manager.user_for_session_cookie(session_cookie)["slug"] == user["slug"]


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
            login_id="alice",
            password="correct horse",
            account_seq="abc",
            client_id="client-id",
            client_secret="client-secret",
        )
    except ValueError as exc:
        assert "digits only" in str(exc)
    else:
        raise AssertionError("invalid account sequence unexpectedly succeeded")


def test_create_user_rolls_back_when_credential_file_write_fails(
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

    try:
        manager.create_user(
            client_ip="100.64.0.10",
            display_name="Alice",
            login_id="alice",
            password="correct horse",
            account_seq="7",
            client_id="client-id",
            client_secret="bad\nsecret",
        )
    except ValueError as exc:
        assert "new lines" in str(exc)
    else:
        raise AssertionError("invalid credential unexpectedly succeeded")

    registry = gateway.load_registry(config.registry_path)
    assert registry["ip_map"] == {}
    assert registry["login_map"] == {}
    assert registry["users"] == {}
