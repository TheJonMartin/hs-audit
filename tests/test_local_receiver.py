"""Run the real Netlify handlers over HTTP (netlify/dev-server.mjs) and drive them with the CLI code."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

from hubspot_audit import cli, receiver, setup

NETLIFY = Path(__file__).resolve().parents[1] / "netlify"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
REFRESH = "dev-refresh-token"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    root = tmp_path_factory.mktemp("setup")
    (root / "hubspot-app/src/app").mkdir(parents=True)
    key = root / "private.pem"
    setup.run_setup("https://example.netlify.app", None, root, key)
    values = setup.read_env_file(root / ".env.receiver")
    values.update(
        {
            "HUBSPOT_CLIENT_ID": "cid",
            "HUBSPOT_CLIENT_SECRET": "cs",
            "ERROR_ROUTER_URL": "http://router.invalid",
        }
    )
    env = {
        **os.environ,
        **values,
        "PORT": "0",
        "DEV_FAKE_HUB_ID": "4242",
        "DEV_FAKE_REFRESH_TOKEN": REFRESH,
    }
    proc = subprocess.Popen(
        ["node", "dev-server.mjs"], cwd=NETLIFY, env=env, stdout=subprocess.PIPE, text=True
    )
    line = proc.stdout.readline()
    assert line.startswith("LISTENING"), line
    base = f"http://127.0.0.1:{line.split()[1]}"
    yield {"base": base, "values": values, "key": key}
    proc.terminate()
    proc.wait(timeout=10)


def test_receiver_check_passes_against_the_running_handlers(live):
    rows = setup.receiver_check(live["base"], live["values"]["TOKEN_FETCH_SECRET"])
    assert all(passed for _, passed, _ in rows), rows
    assert len(rows) == 5


def test_receiver_check_fails_with_the_wrong_secret(live):
    rows = dict((n, p) for n, p, _ in setup.receiver_check(live["base"], "wrong"))
    assert rows["clients accepts the fetch secret"] is False


def test_full_install_fetch_rotate_and_offboard_over_http(live):
    base, values, key = live["base"], live["values"], live["key"]
    fetch_secret = values["TOKEN_FETCH_SECRET"]
    state = receiver.new_signed_state(values["STATE_SIGNING_SECRET"])

    # HubSpot redirects the client's browser here after they approve the install.
    page = requests.get(
        f"{base}/oauth/callback", params={"code": "abc", "state": state}, timeout=10
    )
    assert page.status_code == 200 and "Connected" in page.text
    again = requests.get(
        f"{base}/oauth/callback", params={"code": "abc", "state": state}, timeout=10
    )
    assert again.status_code == 409

    clients = receiver.list_clients(base, fetch_secret)
    assert [c["hub_id"] for c in clients] == ["4242"] and "ciphertext" not in str(clients)

    by_state = receiver.fetch_refresh_token(base, fetch_secret, state=state, private_key_path=key)
    by_portal = receiver.fetch_refresh_token(
        base, fetch_secret, portal="4242", private_key_path=key
    )
    assert (
        by_state == by_portal
        and by_state["refresh_token"] == REFRESH
        and by_state["hub_id"] == "4242"
    )

    receiver.store_refresh_token(base, fetch_secret, "4242", "rotated-token", key)
    assert (
        receiver.fetch_refresh_token(base, fetch_secret, portal="4242", private_key_path=key)[
            "refresh_token"
        ]
        == "rotated-token"
    )
    assert receiver.list_clients(base, fetch_secret)[0]["last_used"] is not None

    receiver.delete_client(base, fetch_secret, "4242")
    with pytest.raises(Exception, match="not found"):
        receiver.fetch_refresh_token(base, fetch_secret, portal="4242", private_key_path=key)


def test_cli_receiver_check_command(live, monkeypatch, capsys):
    monkeypatch.setenv("TOKEN_RECEIVER_URL", live["base"])
    monkeypatch.setenv("TOKEN_FETCH_SECRET", live["values"]["TOKEN_FETCH_SECRET"])
    assert cli.main(["receiver-check"]) == 0
    assert capsys.readouterr().out.count("PASS") == 5
    monkeypatch.setenv("TOKEN_FETCH_SECRET", "wrong")
    assert cli.main(["receiver-check"]) == 1
    time.sleep(0)
