import json

import pytest
from conftest import NOW, SECRET_EMAIL, SERVICE_KEY, FakePortal

from hubspot_audit import bundle
from hubspot_audit.categories import registry
from hubspot_audit.client import HubSpotClient
from hubspot_audit.context import AuditContext
from hubspot_audit.errors import BundleValidationError
from hubspot_audit.probes import PROBES


def run_all(portal, sample_size=50):
    client = HubSpotClient(SERVICE_KEY, session=portal, sleep=lambda _s: None)
    probes = client.run_probes(PROBES)
    ctx = AuditContext(client=client, now=NOW, sample_size=sample_size)
    results = [mod.run(ctx) for _, (_, mod) in sorted(registry().items())]
    data = bundle.assemble(
        results,
        "Acme Co",
        "12345",
        NOW,
        client.granted_areas(),
        [{"area": p.area, "endpoint": p.endpoint, "status": p.status} for p in probes],
        sample_size,
    )
    tables = {k: v for r in results for k, v in r.tables.items()}
    return data, tables


def metrics_by_key(data):
    return {m["key"]: m for c in data["categories"].values() for m in c["metrics"]}


def test_full_run_validates_and_values(portal, tmp_path):
    data, tables = run_all(portal)
    bundle.validate(data, SERVICE_KEY)
    m = metrics_by_key(data)
    assert len(data["categories"]) == 10
    assert m["properties.contacts.custom"]["value"] == 3
    assert m["properties.contacts.naming_flags"]["value"]["non_snake_case_name"] == 1
    assert m["properties.contacts.duplicate_custom_labels"]["value"] == 1
    assert m["contacts.duplicates.email"]["value"]["clusters"] == 1
    assert m["users.super_admin_count"]["value"] == 1
    assert m["users.role_distribution"]["value"] == {"Admin": 1, "Sales": 1}
    assert m["deals.open.total"]["value"] == 2
    assert m["deals.open.past_due_close_date"]["value"] == 1
    assert m["deals.open.no_activity_14d"]["value"] == 1
    assert m["contacts.no_owner"]["value"] == {"count": 250, "pct_of_contacts": 25.0}
    assert m["contacts.unsubscribed"]["value"]["pct_of_contacts"] == 5.0
    assert m["workflows.stale_but_active"]["value"] == 1
    assert m["workflows.routing_related"]["value"]["count"] == 1
    assert m["activity.owners_with_zero_activity_30d"]["value"] == ["Bo Idle"]
    assert m["emails.sent_90d.rates"]["value"]["open"] == 40.0
    assert m["contacts.creation_source"]["value"]["manual_or_offline_pct"] == 50.0
    assert m["sequences.inventory"]["status"] == "unsupported"
    assert m["deals.renewals_next_60d"]["status"] == "ok"
    assert all(
        item["status"] in {"ok", "empty", "forbidden", "unsupported", "error"}
        for c in data["categories"].values()
        for item in c["metrics"]
    )


def test_missing_scope_yields_forbidden_not_crash():
    portal = FakePortal(deny={"/automation/v4/flows"})
    data, _ = run_all(portal)
    bundle.validate(data, SERVICE_KEY)
    m = metrics_by_key(data)
    assert m["workflows.total"]["status"] == "forbidden" and m["workflows.total"]["value"] is None
    assert "automation" not in data["meta"]["scopes_granted"]
    assert m["properties.contacts.total"]["status"] == "ok"


def test_outputs_have_no_key_or_emails(portal, tmp_path):
    data, tables = run_all(portal)
    run_dir = bundle.write_outputs(data, tables, tmp_path, "Acme Co", NOW)
    blob = "".join(p.read_text() for p in run_dir.iterdir())
    assert SERVICE_KEY not in blob
    assert SECRET_EMAIL not in blob and "@example.com" not in blob
    assert "[redacted-email]" in (run_dir / "open_deals.csv").read_text()
    assert (
        json.loads((run_dir / "audit_bundle.json").read_text())["meta"]["csv_email_redactions"] >= 1
    )
    assert run_dir.name == "acme-co_2026-10-09"


def test_validation_rejects_duplicate_keys_and_key_leak(portal):
    data, _ = run_all(portal)
    first = data["categories"]["1"]["metrics"]
    first.append(dict(first[0]))
    with pytest.raises(BundleValidationError):
        bundle.validate(data, SERVICE_KEY)
    data, _ = run_all(portal)
    data["categories"]["1"]["metrics"][0]["note"] = SERVICE_KEY
    with pytest.raises(BundleValidationError):
        bundle.validate(data, SERVICE_KEY)


def test_schema_rejects_non_null_value_on_gap(portal):
    data, _ = run_all(portal)
    bad = data["categories"]["1"]["metrics"][0]
    bad["status"], bad["value"] = "forbidden", 5
    with pytest.raises(BundleValidationError):
        bundle.validate(data, SERVICE_KEY)


def test_category_subset_and_calls_logged(portal):
    client = HubSpotClient(SERVICE_KEY, session=portal, sleep=lambda _s: None)
    ctx = AuditContext(client=client, now=NOW)
    registry()[7][1].run(ctx)
    assert client.call_log == []
