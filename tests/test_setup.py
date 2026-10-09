import json
import shutil
import stat
from pathlib import Path

import pytest

from hubspot_audit import receiver, setup
from hubspot_audit.scopes import OPTIONAL_SCOPES, REQUIRED_SCOPES

REPO = Path(__file__).resolve().parents[1]
SITE = "https://audit-test.netlify.app"


@pytest.fixture
def root(tmp_path):
    app = tmp_path / "hubspot-app/src/app"
    app.mkdir(parents=True)
    shutil.copy(REPO / "hubspot-app/src/app/app-hsmeta.json", app / "app-hsmeta.json")
    return tmp_path


def test_setup_creates_keys_secrets_env_files_and_app_config(root):
    key = root / "keys/private.pem"
    report = setup.run_setup(SITE, "support@example.com", root, key)
    assert "private key" in report.created and key.exists()
    receiver_env = setup.read_env_file(root / ".env.receiver")
    assert receiver_env["OAUTH_REDIRECT_URI"] == SITE + "/oauth/callback"
    assert len(receiver_env["STATE_SIGNING_SECRET"]) == 64
    assert receiver_env["TOKEN_PUBLIC_KEY_PEM"].startswith("-----BEGIN PUBLIC KEY-----")
    assert (
        receiver_env["TOKEN_STORE_NAME"] == "oauth-tokens"
        and receiver_env["PLATFORM_NAME"] == "OTHER"
    )
    assert set(report.blank_required) == {
        "HUBSPOT_CLIENT_ID",
        "HUBSPOT_CLIENT_SECRET",
        "ERROR_ROUTER_URL",
    }
    local = setup.read_env_file(root / ".env")
    assert local["TOKEN_RECEIVER_URL"] == SITE
    assert local["STATE_SIGNING_SECRET"] == receiver_env["STATE_SIGNING_SECRET"]
    assert local["TOKEN_FETCH_SECRET"] == receiver_env["TOKEN_FETCH_SECRET"]
    for path in (root / ".env.receiver", root / ".env", key):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    app = json.loads((root / setup.APP_CONFIG_PATH).read_text())["config"]
    assert app["auth"]["redirectUrls"] == [SITE + "/oauth/callback"]
    assert app["support"]["supportEmail"] == "support@example.com"


def test_report_contains_no_secret_values(root):
    report = setup.run_setup(SITE, None, root, root / "k.pem")
    values = setup.read_env_file(root / ".env.receiver")
    blob = repr(report)
    assert all(
        v not in blob for v in (values["STATE_SIGNING_SECRET"], values["TOKEN_FETCH_SECRET"])
    )


def test_setup_is_idempotent_and_never_rotates(root):
    key = root / "k.pem"
    setup.run_setup(SITE, None, root, key)
    first = setup.read_env_file(root / ".env.receiver")
    pem = key.read_bytes()
    again = setup.run_setup(SITE, None, root, key)
    assert setup.read_env_file(root / ".env.receiver") == first and key.read_bytes() == pem
    assert "private key" in again.kept and "STATE_SIGNING_SECRET" in again.kept


def test_setup_preserves_hand_filled_values_and_follows_new_site_url(root):
    key = root / "k.pem"
    setup.run_setup(SITE, None, root, key)
    env = setup.read_env_file(root / ".env.receiver")
    env.update(
        {"HUBSPOT_CLIENT_ID": "cid", "HUBSPOT_CLIENT_SECRET": "cs", "ERROR_ROUTER_URL": "https://r"}
    )
    setup.write_env_file(root / ".env.receiver", env)
    report = setup.run_setup("https://other.netlify.app/", None, root, key)
    after = setup.read_env_file(root / ".env.receiver")
    assert after["HUBSPOT_CLIENT_ID"] == "cid" and report.blank_required == []
    assert after["OAUTH_REDIRECT_URI"] == "https://other.netlify.app/oauth/callback"
    assert setup.read_env_file(root / ".env")["HUBSPOT_CLIENT_ID"] == "cid"


def test_setup_refuses_a_local_secret_that_disagrees(root):
    key = root / "k.pem"
    setup.run_setup(SITE, None, root, key)
    (root / ".env").write_text("STATE_SIGNING_SECRET=different\n")
    with pytest.raises(setup.SetupError, match="STATE_SIGNING_SECRET"):
        setup.run_setup(SITE, None, root, key)


def test_setup_appends_to_an_existing_env_without_clobbering(root):
    (root / ".env").write_text("HUBSPOT_SERVICE_KEY=abc")  # no trailing newline
    setup.run_setup(SITE, None, root, root / "k.pem")
    local = setup.read_env_file(root / ".env")
    assert local["HUBSPOT_SERVICE_KEY"] == "abc" and local["TOKEN_RECEIVER_URL"] == SITE


@pytest.mark.parametrize(
    "url", ["http://x.netlify.app", "https://x.netlify.app/path", "x.netlify.app", "https://"]
)
def test_site_url_must_be_an_https_origin(url):
    with pytest.raises(setup.SetupError):
        setup.normalize_site_url(url)


def test_env_file_roundtrips_multiline_values_and_rejects_quotes(tmp_path):
    path = tmp_path / "e"
    setup.write_env_file(path, {"A": "line1\nline2", "B": "plain"})
    assert setup.read_env_file(path) == {"A": "line1\nline2", "B": "plain"}
    with pytest.raises(setup.SetupError):
        setup.write_env_file(path, {"A": "it's"})


def test_committed_app_definition_matches_scopes_and_has_expected_shape():
    config = json.loads((REPO / "hubspot-app/src/app/app-hsmeta.json").read_text())
    auth = config["config"]["auth"]
    assert (config["type"], config["config"]["distribution"], auth["type"]) == (
        "app",
        "marketplace",
        "oauth",
    )
    assert auth["requiredScopes"] == REQUIRED_SCOPES and auth["optionalScopes"] == OPTIONAL_SCOPES
    assert not [s for s in auth["requiredScopes"] + auth["optionalScopes"] if "write" in s]
    assert json.loads((REPO / "hubspot-app/hsproject.json").read_text())["srcDir"] == "src"


def test_generated_key_matches_public_key_helper(tmp_path):
    key = tmp_path / "k.pem"
    assert receiver.generate_keypair(key) == setup.public_pem_for(key)
