import pytest

from hubspot_audit.config import load_config, slugify
from hubspot_audit.errors import ConfigError

GOOD = """
[defaults]
categories = [1, 2, 3]
sample_size = 200
output_dir = "out"

[[clients]]
name = "Acme Co"
portal_id = 12345

[[clients]]
name = "Globex"
auth = "service-key"
service_key_env = "GLOBEX_KEY"
categories = [6]
sample_size = 50
enabled = false
"""


def write(tmp_path, text):
    path = tmp_path / "clients.toml"
    path.write_text(text)
    return path


def test_loads_defaults_overrides_and_normalizes(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))
    acme, globex = cfg.clients
    assert cfg.output_dir == "out"
    assert (acme.slug, acme.auth, acme.portal_id) == ("acme-co", "oauth", "12345")
    assert acme.categories == (1, 2, 3) and acme.sample_size == 200
    assert (globex.categories, globex.sample_size, globex.service_key_env) == (
        (6,),
        50,
        "GLOBEX_KEY",
    )
    assert [c.name for c in cfg.enabled_clients()] == ["Acme Co"]


def test_find_by_slug_name_or_portal(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))
    for ident in ("acme-co", "ACME CO", "12345"):
        assert cfg.find(ident).name == "Acme Co"
    with pytest.raises(ConfigError, match="acme-co"):
        cfg.find("nope")


def test_example_file_is_valid():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "clients.example.toml")
    assert len(cfg.clients) == 2


@pytest.mark.parametrize(
    "text,match",
    [
        ('[[clients]]\nauth = "oauth"', "name is required"),
        ('[[clients]]\nname = "A"\nservice_key = "x"', "Credentials do not belong"),
        ('[[clients]]\nname = "A"\nauth = "basic"', "auth must be"),
        ('[[clients]]\nname = "A"\nauth = "service-key"', "service_key_env"),
        (
            '[[clients]]\nname = "A"\nauth = "service-key"\nservice_key_env = "lower"',
            "service_key_env",
        ),
        ('[[clients]]\nname = "A"\nservice_key_env = "X"', "only applies"),
        ('[[clients]]\nname = "A"\nportal_id = "abc"', "portal_id"),
        ('[[clients]]\nname = "A"\ncategories = [11]', "unknown categories"),
        ('[[clients]]\nname = "A"\ncategories = []', "non-empty"),
        ('[[clients]]\nname = "A"\nsample_size = 0', "sample_size"),
        ('[[clients]]\nname = "A"\nenabled = "yes"', "enabled"),
        ('[[clients]]\nname = "A"\nslug = "Bad Slug"', "slug"),
        ('[[clients]]\nname = "A"\n[[clients]]\nname = "a"', "Duplicate client name"),
        (
            '[[clients]]\nname = "A"\nportal_id = 1\n[[clients]]\nname = "B"\nportal_id = 1',
            "Duplicate client portal_id",
        ),
        ("[defaults]\nbogus = 1", "Unknown keys"),
        ("[extra]\nx = 1", "Unknown top-level"),
        ("not toml [", "not valid TOML"),
    ],
)
def test_rejects_bad_config_with_specific_message(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, text))


def test_refuses_a_file_containing_a_token(tmp_path):
    secret = "pat-na1-" + "abcdef12-3456-7890"
    with pytest.raises(ConfigError, match="looks like a HubSpot token") as exc:
        load_config(write(tmp_path, f'[[clients]]\nname = "A"\nnote = "{secret}"'))
    assert secret not in str(exc.value)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path / "nope.toml")


def test_slugify():
    assert slugify("  Acme & Sons, Inc. ") == "acme-sons-inc"
