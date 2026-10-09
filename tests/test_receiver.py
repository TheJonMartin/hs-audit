import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import FakeResponse
from cryptography.hazmat.primitives import serialization

from hubspot_audit import cli, receiver
from hubspot_audit.errors import AuthError

NETLIFY = Path(__file__).resolve().parents[1] / "netlify"
SECRET = "signing-secret"


def test_new_state_is_signed_and_verifiable():
    nonce, signature = receiver.new_signed_state(SECRET).split(".")
    assert signature == receiver.sign_nonce(nonce, SECRET)
    with pytest.raises(AuthError):
        receiver.new_signed_state("")


def test_keygen_writes_private_key_mode_600_and_never_overwrites(tmp_path):
    path = tmp_path / "keys" / "private.pem"
    public_pem = receiver.generate_keypair(path)
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        receiver.generate_keypair(path)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_encrypts_and_python_decrypts(tmp_path):
    key_path = tmp_path / "private.pem"
    public_pem = receiver.generate_keypair(key_path)
    script = (
        "import {encryptPayload} from './functions/lib/security.mjs';"
        "process.stdout.write(encryptPayload({refresh_token:'RT-123'}, process.env.PEM));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=NETLIFY,
        env={**os.environ, "PEM": public_pem},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert receiver.decrypt_ciphertext(out, key_path) == {"refresh_token": "RT-123"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_and_python_agree_on_state_signature():
    script = (
        "import {signNonce} from './functions/lib/security.mjs';"
        "process.stdout.write(signNonce('abcdefghijklmnopqrstuvwxyz012345','signing-secret'));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=NETLIFY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == receiver.sign_nonce("abcdefghijklmnopqrstuvwxyz012345", SECRET)


class Receiver:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body or {}, []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        return FakeResponse(self.status, self.body)


def test_fetch_decrypts_and_sends_bearer_secret(tmp_path):
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_path = tmp_path / "k.pem"
    public_pem = receiver.generate_keypair(key_path)
    public = serialization.load_pem_public_key(public_pem.encode())
    cipher = public.encrypt(
        json.dumps({"refresh_token": "RT"}).encode(),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    fake = Receiver(
        body={"ciphertext": base64.b64encode(cipher).decode(), "hub_id": 7, "scopes": ["a"]}
    )
    out = receiver.fetch_refresh_token(
        "https://site.test/", "fs", state="N.S", private_key_path=key_path, session=fake
    )
    assert out == {"refresh_token": "RT", "hub_id": 7, "scopes": ["a"]}
    method, url, params, _, headers = fake.calls[0]
    assert (method, url, params) == ("GET", "https://site.test/api/token", {"state": "N.S"})
    assert headers["Authorization"] == "Bearer fs"
    receiver.fetch_refresh_token(
        "https://site.test", "fs", portal="7", private_key_path=key_path, session=fake
    )
    assert fake.calls[1][2] == {"portal": "7"}


@pytest.mark.parametrize("status,word", [(404, "not found"), (410, "expired"), (401, "401")])
def test_fetch_errors_are_specific_and_leak_nothing(status, word, tmp_path):
    with pytest.raises(AuthError, match=word) as exc:
        receiver.fetch_refresh_token(
            "https://s",
            "secret",
            state="N.S",
            private_key_path=tmp_path / "k",
            session=Receiver(status),
        )
    assert "secret" not in str(exc.value)


def test_fetch_requires_config_and_exactly_one_identifier(tmp_path):
    key = tmp_path / "k"
    with pytest.raises(AuthError):
        receiver.fetch_refresh_token("", "", portal="1", private_key_path=key)
    with pytest.raises(AuthError):
        receiver.fetch_refresh_token("u", "s", private_key_path=key)
    with pytest.raises(AuthError):
        receiver.fetch_refresh_token("u", "s", portal="1", state="x", private_key_path=key)


def test_fetch_by_portal_sends_portal_param(tmp_path):
    key = tmp_path / "k.pem"
    receiver.generate_keypair(key)
    fake = Receiver(body={"ciphertext": "AAAA", "hub_id": 7})
    with pytest.raises(AuthError):  # ciphertext is junk; we only care about the request shape
        receiver.fetch_refresh_token(
            "https://s", "fs", portal="7", private_key_path=key, session=fake
        )
    assert fake.calls[0][2] == {"portal": "7"}


def test_store_refresh_token_encrypts_before_sending(tmp_path):
    key_path = tmp_path / "k.pem"
    receiver.generate_keypair(key_path)
    fake = Receiver(body={"ok": True})
    receiver.store_refresh_token("https://s", "fs", "7", "NEW-RT", key_path, session=fake)
    method, _, params, body, _ = fake.calls[0]
    assert (method, params) == ("PUT", {"portal": "7"})
    assert "NEW-RT" not in json.dumps(body)
    assert receiver.decrypt_ciphertext(body["ciphertext"], key_path) == {"refresh_token": "NEW-RT"}


def test_list_and_delete_clients():
    fake = Receiver(body={"clients": [{"hub_id": "7"}], "ok": True})
    assert receiver.list_clients("https://s", "fs", session=fake) == [{"hub_id": "7"}]
    receiver.delete_client("https://s", "fs", "7", session=fake)
    assert [c[0] for c in fake.calls] == ["GET", "DELETE"]


def test_build_auth_with_portal_wires_rotation_writeback(monkeypatch):
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "cs")
    stored = []
    monkeypatch.setattr(
        cli,
        "fetch_refresh_token",
        lambda *a, **k: {"refresh_token": "RT-1", "hub_id": 7, "scopes": []},
    )
    monkeypatch.setattr(cli, "store_refresh_token", lambda *a: stored.append(a[3]))
    auth = cli.build_auth("oauth", portal="7")
    auth._on_refresh_token_change("RT-2")
    assert stored == ["RT-2"]


def test_decrypt_with_wrong_key_is_auth_error(tmp_path):
    with pytest.raises(AuthError):
        receiver.decrypt_ciphertext("AAAA", tmp_path / "missing.pem")


def test_cli_install_url_and_keygen(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "cs")
    monkeypatch.setenv("HUBSPOT_REDIRECT_URI", "https://site.test/oauth/callback")
    monkeypatch.setenv("STATE_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("TOKEN_PRIVATE_KEY_PATH", str(tmp_path / "p.pem"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["install-url"]) == 0
    out = capsys.readouterr().out
    assert (
        "client_id=cid" in out and "redirect_uri=https%3A%2F%2Fsite.test%2Foauth%2Fcallback" in out
    )
    assert "client_secret" not in out and "cs&" not in out
    assert cli.main(["keygen"]) == 0
    assert "BEGIN PUBLIC KEY" in capsys.readouterr().out
    assert (tmp_path / "p.pem").exists()


def test_build_auth_with_state_uses_fetched_refresh_token(monkeypatch):
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "cid")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "cs")
    monkeypatch.delenv("HUBSPOT_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(
        cli,
        "fetch_refresh_token",
        lambda *a, **k: {"refresh_token": "RT-9", "hub_id": 1, "scopes": []},
    )
    auth = cli.build_auth("oauth", "N.S")
    assert "RT-9" in auth.secrets()
