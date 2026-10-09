import json

import pytest
from conftest import SERVICE_KEY, FakePortal

from hubspot_audit import cli
from hubspot_audit.errors import AuthError, BatchError, ConfigError

CONFIG = """
[defaults]
categories = [1]
sample_size = 20

[[clients]]
name = "Acme Co"
auth = "service-key"
service_key_env = "ACME_KEY"

[[clients]]
name = "Bad Key Inc"
auth = "service-key"
service_key_env = "BAD_KEY"

[[clients]]
name = "Off"
auth = "service-key"
service_key_env = "OFF_KEY"
enabled = false

[[clients]]
name = "Oauth Pending"
auth = "oauth"
enabled = false
"""


def fake_request(portal):
    def request(self, method, url, params=None, json=None, timeout=None):
        portal.headers = dict(self.headers)
        return portal.request(method, url, params=params, json=json)

    return request


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clients.toml").write_text(CONFIG)
    monkeypatch.setenv("ERROR_ROUTER_URL", "http://router.test")
    monkeypatch.setenv("ACME_KEY", SERVICE_KEY)
    monkeypatch.setenv("BAD_KEY", "wrong")
    monkeypatch.setattr("requests.Session.request", fake_request(FakePortal()))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    posted = []
    monkeypatch.setattr(
        cli, "handle_failure", lambda cfg, exc, ctx: posted.append((exc, dict(ctx)))
    )
    return tmp_path, posted


def test_run_client_uses_config_categories_sample_and_key_env(env):
    tmp, _ = env
    assert cli.main(["run", "--client", "acme-co", "--output-dir", str(tmp / "o")]) == 0
    bundle = json.loads(next((tmp / "o").glob("*/audit_bundle.json")).read_text())
    assert bundle["meta"]["prospect"] == "Acme Co"
    assert bundle["meta"]["categories_run"] == [1]
    assert bundle["meta"]["contact_sample_size"] == 20


def test_flags_override_config(env):
    tmp, _ = env
    cli.main(
        [
            "run",
            "--client",
            "Acme Co",
            "--categories",
            "3",
            "--sample-size",
            "5",
            "--output-dir",
            str(tmp / "o"),
        ]
    )
    meta = json.loads(next((tmp / "o").glob("*/audit_bundle.json")).read_text())["meta"]
    assert meta["categories_run"] == [3] and meta["contact_sample_size"] == 5


def test_run_all_continues_past_a_failing_client_then_raises(env):
    tmp, posted = env
    with pytest.raises(BatchError, match="1 client.*Bad Key Inc"):
        cli.main(["run", "--all", "--output-dir", str(tmp / "o")])
    assert [p.name for p in (tmp / "o").iterdir()][0].startswith("acme-co")  # good client still ran
    assert len(posted) == 1 and isinstance(
        posted[0][0], AuthError
    )  # one payload, for the bad client
    assert posted[0][1]["record_id"] == "Bad Key Inc"  # disabled clients were skipped


def test_unknown_client_and_oauth_without_portal(env):
    with pytest.raises(ConfigError, match="No client matches"):
        cli.main(["run", "--client", "zzz"])
    with pytest.raises(ConfigError, match="no portal_id yet"):
        cli.main(["run", "--client", "oauth-pending"])


def test_portal_mismatch_is_fatal(env, monkeypatch):
    tmp, posted = env
    (tmp / "clients.toml").write_text(
        CONFIG.replace('name = "Acme Co"', 'name = "Acme Co"\nportal_id = 999')
    )
    with pytest.raises(AuthError, match="Portal mismatch"):
        cli.main(["run", "--client", "acme-co", "--output-dir", str(tmp / "o")])


def test_missing_service_key_env_names_the_variable(env, monkeypatch):
    monkeypatch.delenv("ACME_KEY")
    with pytest.raises(AuthError, match="ACME_KEY is not set"):
        cli.main(["run", "--client", "acme-co"])


def test_check_config_and_bad_config(env, capsys):
    tmp, _ = env
    assert cli.main(["check-config"]) == 0
    assert "OK: 4 client(s), 2 enabled." in capsys.readouterr().out
    (tmp / "clients.toml").write_text("[[clients]]\nname = 'A'\nservice_key = 'x'")
    with pytest.raises(ConfigError):
        cli.main(["check-config"])


def test_clients_table_merges_config_and_receiver(env, monkeypatch, capsys):
    tmp, _ = env
    (tmp / "clients.toml").write_text(
        '[[clients]]\nname = "Acme"\nportal_id = 1\n[[clients]]\nname = "Gone"\nportal_id = 2\n'
    )
    monkeypatch.setenv("TOKEN_RECEIVER_URL", "https://r.test")
    monkeypatch.setenv("TOKEN_FETCH_SECRET", "fs")
    monkeypatch.setattr(
        cli,
        "list_clients",
        lambda *a: [{"hub_id": "1", "last_used": 5}, {"hub_id": "7", "last_used": None}],
    )
    cli.main(["clients"])
    rows = [line.split("\t") for line in capsys.readouterr().out.strip().splitlines()[1:]]
    assert rows[0][0] == "Acme" and rows[0][5] == "yes" and rows[0][6] == "5"
    assert rows[1][0] == "Gone" and rows[1][5] == "NO"
    assert rows[2][0] == "(not in config)" and rows[2][3] == "7"


def test_revoke_revokes_at_hubspot_then_deletes(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "cs")
    monkeypatch.setattr(
        cli, "fetch_refresh_token", lambda *a, **k: {"refresh_token": "RT", "hub_id": 1}
    )
    monkeypatch.setattr(cli.OAuthAuth, "revoke_refresh_token", lambda self: calls.append("revoke"))
    monkeypatch.setattr(cli, "delete_client", lambda *a: calls.append("delete"))
    cli.main(["revoke", "--portal", "1"])
    assert calls == ["revoke", "delete"] and "revoked at HubSpot" in capsys.readouterr().out


def test_revoke_still_deletes_when_hubspot_revoke_fails(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "cs")
    monkeypatch.setattr(
        cli, "fetch_refresh_token", lambda *a, **k: {"refresh_token": "RT", "hub_id": 1}
    )

    def boom(self):
        raise AuthError("Token revoke failed: HTTP 400")

    monkeypatch.setattr(cli.OAuthAuth, "revoke_refresh_token", boom)
    monkeypatch.setattr(cli, "delete_client", lambda *a: calls.append("delete"))
    cli.main(["revoke", "--portal", "1"])
    out = capsys.readouterr().out
    assert calls == ["delete"] and "WARNING" in out and "revoked at HubSpot" not in out
