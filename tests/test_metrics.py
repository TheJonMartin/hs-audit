from datetime import datetime, UTC

from hubspot_audit import metrics as m
from hubspot_audit.errors import ForbiddenError

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def test_population_rates_and_pct():
    recs = [
        {"properties": {"a": "x"}},
        {"properties": {"a": ""}},
        {"properties": {}},
        {"properties": {"a": "y"}},
    ]
    assert m.population_rates(recs, ["a"]) == {"a": 50.0}
    assert m.pct(1, 0) is None


def test_days_since_handles_iso_ms_and_junk():
    assert round(m.days_since("2026-10-08T00:00:00Z", NOW)) == 1
    assert (
        round(
            m.days_since(
                str(int(datetime(2026, 10, 8, tzinfo=UTC).timestamp() * 1000)), NOW
            )
        )
        == 1
    )
    assert m.days_since("", NOW) is None and m.days_since("garbage", NOW) is None


def test_duplicate_groups():
    recs = [
        {"properties": {"email": "A@x.com"}},
        {"properties": {"email": "a@x.com "}},
        {"properties": {"email": "b@x.com"}},
    ]
    assert m.duplicate_groups(recs, m.email_key) == {
        "clusters": 1,
        "extra_records": 1,
        "records_in_clusters": 2,
    }


def test_name_company_key_needs_all_parts():
    assert m.name_company_key({"properties": {"firstname": "a", "lastname": "b"}}) is None


def test_safe_turns_forbidden_into_null_gap():
    def boom():
        raise ForbiddenError("no", "/x", 403)

    out = m.safe("k.v", "count", "/x", boom)
    assert out["status"] == "forbidden" and out["value"] is None


def test_distribution_labels_blank():
    assert m.distribution(["a", "a", "", None]) == {"a": 2, "(none)": 2}
