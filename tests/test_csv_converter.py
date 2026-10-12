import csv
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.csv_converter import convert_csv, detect_columns, preview_conversion


@pytest.fixture
def client():
    # The context manager is deliberately not used: the conversion endpoints
    # never touch ListMonk, so the lifespan (and its background tasks) can be
    # skipped.
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


def test_detect_columns_normal():
    csv_data = b"email,name,company\nalice@example.com,Alice,Acme\nbob@example.com,Bob,Beta\n"
    res = detect_columns(csv_data)
    assert res["columns"] == ["email", "name", "company"]
    assert len(res["sample_rows"]) == 2
    assert res["sample_rows"][0]["email"] == "alice@example.com"
    assert res["row_count_estimate"] == 2


def test_detect_columns_with_ragged_extra_columns():
    # Row 2 has 4 fields when header only has 3 fields.
    # csv.DictReader puts extra fields in row[None].
    csv_data = b"email,name,company\nalice@example.com,Alice,Acme\nbob@example.com,Bob,Beta,ExtraField\n"
    res = detect_columns(csv_data)
    assert res["columns"] == ["email", "name", "company"]
    assert len(res["sample_rows"]) == 2
    assert res["sample_rows"][1]["email"] == "bob@example.com"


def test_convert_csv_normal():
    csv_data = b"email,name,company,city\nalice@example.com,Alice,Acme,Paris\n"
    res = convert_csv(
        csv_data,
        email_column="email",
        name_column="name",
        attribute_columns=["company", "city"],
    )
    assert "error" not in res["stats"]
    assert res["stats"]["converted"] == 1

    reader = csv.DictReader(res["csv_content"].splitlines())
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["email"] == "alice@example.com"
    assert rows[0]["name"] == "Alice"
    attribs = json.loads(rows[0]["attributes"])
    assert attribs == {"company": "Acme", "city": "Paris"}


def test_convert_csv_ragged_extra_columns():
    # Row with extra field (None key in DictReader)
    csv_data = b"email,name,company\nalice@example.com,Alice,Acme,SurplusField\n"
    res = convert_csv(
        csv_data,
        email_column="email",
        name_column="name",
        attribute_columns=["company"],
    )
    assert "error" not in res["stats"]
    assert res["stats"]["converted"] == 1
    reader = csv.DictReader(res["csv_content"].splitlines())
    rows = list(reader)
    assert rows[0]["email"] == "alice@example.com"
    attribs = json.loads(rows[0]["attributes"])
    assert attribs == {"company": "Acme"}


def test_convert_csv_missing_email_col():
    csv_data = b"username,fullname\nalice,Alice\n"
    res = convert_csv(csv_data, email_column="email")
    assert "error" in res["stats"]
    assert "Email column 'email' not found" in res["stats"]["error"]


# ── Column suggestions (email/name auto-detection) ────────────
def test_suggest_columns_from_headers():
    csv_data = b"Email Address,Full Name,Company\nmaria@x.io,Maria Lopez,Brightloop\n"
    res = detect_columns(csv_data)
    assert res["suggested"] == {"email": "Email Address", "name": "Full Name"}


def test_suggest_columns_handles_punctuation_and_case():
    csv_data = b"E-MAIL,NAME\nbob@y.com,Bob\n"
    res = detect_columns(csv_data)
    assert res["suggested"] == {"email": "E-MAIL", "name": "NAME"}


def test_suggest_columns_falls_back_to_sample_values():
    # No recognisable header names: the column holding addresses must win.
    csv_data = b"contact_person,work_mail,city\nJane,jane@corp.com,Paris\n"
    res = detect_columns(csv_data)
    assert res["suggested"]["email"] == "work_mail"
    assert res["suggested"]["name"] == "contact_person"


def test_suggest_columns_never_reuses_email_for_name():
    csv_data = b"email,notes\nalice@example.com,hello\n"
    res = detect_columns(csv_data)
    assert res["suggested"]["email"] == "email"
    assert res["suggested"]["name"] != "email"


def test_suggest_columns_no_email_column():
    csv_data = b"city,country\nParis,France\n"
    res = detect_columns(csv_data)
    assert res["suggested"]["email"] is None


# ── Ragged row reporting ──────────────────────────────────────
def test_detect_columns_reports_ragged_rows():
    csv_data = b"email,name\nmaria@x.io,Maria\n,bob@y.com,Bob Shifted\n"
    res = detect_columns(csv_data)
    assert res["ragged_rows"] == 1
    assert len(res["warnings"]) == 1
    assert "different number of fields" in res["warnings"][0]


def test_detect_columns_no_warning_when_clean():
    csv_data = b"email,name\na@x.io,A\nb@y.io,B\n"
    res = detect_columns(csv_data)
    assert res["ragged_rows"] == 0
    assert res["warnings"] == []


def test_convert_csv_reports_ragged_rows():
    csv_data = b"email,name\nalice@example.com,Alice\nbob@example.com,Bob,Surplus\n"
    res = convert_csv(csv_data, email_column="email", name_column="name")
    assert res["stats"]["ragged_rows"] == 1
    assert res["stats"]["converted"] == 2


# ── Dynamic attribute promotion (any CSV column must be preserved) ──────────
def test_auto_attributes_promotes_every_unmapped_column():
    csv_data = (
        b"Email Address,Full Name,Company,City,Score\n"
        b"alice@example.com,Alice,Acme,Paris,10\n"
        b"bob@example.com,Bob,Beta,Berlin,20\n"
    )
    res = convert_csv(csv_data, email_column="Email Address", name_column="Full Name")
    assert res["stats"]["converted"] == 2
    assert res["stats"]["auto_attributes"] is True
    assert res["stats"]["attribute_columns"] == ["Company", "City", "Score"]

    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert rows[0]["email"] == "alice@example.com"
    assert rows[0]["name"] == "Alice"
    assert json.loads(rows[0]["attributes"]) == {"Company": "Acme", "City": "Paris", "Score": "10"}


def test_auto_attributes_excludes_email_and_name_columns():
    csv_data = b"email,name,city\nalice@example.com,Alice,Paris\n"
    res = convert_csv(csv_data, email_column="email", name_column="name")
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    attribs = json.loads(rows[0]["attributes"])
    assert "email" not in attribs
    assert "name" not in attribs
    assert attribs == {"city": "Paris"}


def test_auto_attributes_off_keeps_only_selected_columns():
    csv_data = b"email,name,company,city\nalice@example.com,Alice,Acme,Paris\n"
    res = convert_csv(
        csv_data,
        email_column="email",
        name_column="name",
        attribute_columns=["company"],
        auto_attributes=False,
    )
    assert res["stats"]["auto_attributes"] is False
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert json.loads(rows[0]["attributes"]) == {"company": "Acme"}


def test_selected_attribute_columns_are_never_duplicated():
    # A selected attribute still appears in auto mode, and only once, even when
    # the selection differs in case from the header.
    csv_data = b"email,company\nalice@example.com,Acme\n"
    res = convert_csv(
        csv_data,
        email_column="email",
        attribute_columns=["COMPANY"],
        auto_attributes=True,
    )
    assert res["stats"]["attribute_columns"] == ["COMPANY"]
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert json.loads(rows[0]["attributes"]) == {"COMPANY": "Acme"}


def test_json_cell_values_merge_into_attributes():
    csv_data = (
        b'email,meta\nalice@example.com,"{""age"": 30, ""plan"": ""pro""}"\n'
    )
    res = convert_csv(csv_data, email_column="email", auto_attributes=True)
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    attribs = json.loads(rows[0]["attributes"])
    assert attribs["age"] == 30
    assert attribs["plan"] == "pro"


def test_changing_header_punctuation_still_resolves_column():
    csv_data = b"E-Mail Address,My Name,city\nalice@example.com,Alice,Paris\n"
    res = convert_csv(csv_data, email_column="E-Mail Address", name_column="My Name")
    assert "error" not in res["stats"]
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert rows[0]["email"] == "alice@example.com"
    assert rows[0]["name"] == "Alice"


def test_tab_delimited_file_is_promoted_to_comma_output():
    csv_data = b"email\tname\tcity\nalice@example.com\tAlice\tParis\n"
    res = convert_csv(csv_data, email_column="email", name_column="name")
    assert "error" not in res["stats"]
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert rows[0]["email"] == "alice@example.com"
    assert json.loads(rows[0]["attributes"]) == {"city": "Paris"}


def test_semicolon_delimited_file_with_bom_and_blank_lines():
    # Ordinary European exports: csv.Sniffer alone gives up on this shape and
    # used to collapse the whole file into a single column.
    csv_data = (
        "\ufeffE-Mail;Vorname;Firma\r\n"
        "hans@example.de;Hans;Müller GmbH\r\n"
        "\r\n"
        "marie@example.fr;Marie;Acme\r\n"
    ).encode("utf-8")
    res = detect_columns(csv_data)
    assert res["columns"] == ["E-Mail", "Vorname", "Firma"]
    assert res["suggested"]["email"] == "E-Mail"
    assert res["ragged_rows"] == 0

    conv = convert_csv(csv_data, email_column="E-Mail", name_column="Vorname")
    assert conv["stats"]["converted"] == 2
    rows = list(csv.DictReader(conv["csv_content"].splitlines()))
    assert rows[0]["email"] == "hans@example.de"
    assert json.loads(rows[0]["attributes"])["Firma"] == "Müller GmbH"


def test_blank_lines_are_not_reported_as_ragged():
    csv_data = b"email,name\nalice@example.com,Alice\n\n\nbob@example.com,Bob\n"
    res = detect_columns(csv_data)
    assert res["ragged_rows"] == 0
    assert res["warnings"] == []
    conv = convert_csv(csv_data, email_column="email")
    assert conv["stats"]["converted"] == 2
    assert conv["stats"]["ragged_rows"] == 0


# ── Invalid / missing e-mail handling ────────────────────────
def test_invalid_emails_are_skipped_and_counted():
    # ListMonk's importer rejects these, so reporting them beats importing
    # nothing and claiming success.
    csv_data = b"email,name\nnot-an-email,No\nbob@company,Maybe\nok@example.com,Yes\n"
    res = convert_csv(csv_data, email_column="email", name_column="name")
    assert res["stats"]["converted"] == 1
    assert res["stats"]["invalid_emails"] == 2
    assert res["stats"]["skipped"] == 2
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert [r["email"] for r in rows] == ["ok@example.com"]


def test_empty_emails_are_counted_separately():
    csv_data = b"email,name\n,No\nalice@example.com,Yes\n"
    res = convert_csv(csv_data, email_column="email")
    assert res["stats"]["converted"] == 1
    assert res["stats"]["empty_emails"] == 1
    assert res["stats"]["invalid_emails"] == 0
    assert res["stats"]["total"] == 2


def test_emails_are_lowercased_like_listmonk_does():
    csv_data = b"email,name\nAlice@Example.COM,A\n"
    res = convert_csv(csv_data, email_column="email")
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert rows[0]["email"] == "alice@example.com"


def test_single_label_domain_is_accepted():
    # ListMonk's net/mail based sanitizer accepts "a@b.c"; the converter must
    # not drop what the importer would take.
    csv_data = b"email,name\na@b.c,A\n"
    res = convert_csv(csv_data, email_column="email")
    assert res["stats"]["converted"] == 1
    assert res["stats"]["invalid_emails"] == 0


def test_addresses_without_a_dotted_domain_are_still_rejected():
    csv_data = b"email,name\nuser@localhost,A\nbob@company,B\n"
    res = convert_csv(csv_data, email_column="email")
    assert res["stats"]["converted"] == 0
    assert res["stats"]["invalid_emails"] == 2


def test_possible_column_suggestion_prefers_real_emails():
    # Unrecognised headers, no column holds a valid dotted address: nothing is
    # guessed, rather than picking the typo column.
    csv_data = b"name,contact\nalice,bob@company\nbob,alice\n"
    res = detect_columns(csv_data)
    assert res["suggested"]["email"] is None


def test_mixed_ragged_rows_never_crash_or_shift_email():
    # Surplus fields used to leak into the row dict as a list and blow up the
    # CSV writer mid-file, silently dropping every row after it.
    csv_data = (
        b"email,name,city\n"
        b"a@example.com,Alice,Paris,Surplus1,Surplus2\n"
        b"b@example.com,Bob\n"
        b"c@example.com,Cara,Berlin\n"
    )
    res = convert_csv(csv_data, email_column="email", name_column="name")
    assert res["stats"]["converted"] == 3
    assert res["stats"]["ragged_rows"] == 2
    rows = list(csv.DictReader(res["csv_content"].splitlines()))
    assert [r["email"] for r in rows] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]


# ── Preview used by the UI ───────────────────────────────────
def test_preview_conversion_returns_rows_and_stats():
    csv_data = (
        b"email,name,city\na@example.com,A,Paris\nb@example.com,B,Berlin\nc@example.com,C,Rome\n"
    )
    res = preview_conversion(csv_data, "email", "name", limit=2)
    assert len(res["rows"]) == 2
    assert res["rows"][0]["email"] == "a@example.com"
    assert json.loads(res["rows"][0]["attributes"]) == {"city": "Paris"}
    # Stats cover the full file, not just the previewed slice.
    assert res["stats"]["converted"] == 3


def test_preview_conversion_reports_missing_email_column():
    csv_data = b"username,fullname\nalice,Alice\n"
    res = preview_conversion(csv_data, "email")
    assert res["rows"] == []
    assert "error" in res["stats"]


# ── API endpoints ────────────────────────────────────────────
def test_preview_endpoint_defaults_to_auto_attributes(client):
    csv_data = b"email,name,company,city\nalice@example.com,Alice,Acme,Paris\n"
    resp = client.post(
        "/api/converter/preview",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={"email_column": "email", "name_column": "name", "attribute_mode": "auto"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["converted"] == 1
    assert body["stats"]["auto_attributes"] is True
    assert json.loads(body["rows"][0]["attributes"]) == {"company": "Acme", "city": "Paris"}


def test_preview_endpoint_selected_mode_uses_only_checked_columns(client):
    csv_data = b"email,name,company,city\nalice@example.com,Alice,Acme,Paris\n"
    resp = client.post(
        "/api/converter/preview",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={
            "email_column": "email",
            "name_column": "name",
            "attribute_columns": json.dumps(["company"]),
            "attribute_mode": "selected",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["auto_attributes"] is False
    assert json.loads(body["rows"][0]["attributes"]) == {"company": "Acme"}


def test_preview_endpoint_rejects_bad_attribute_mode(client):
    csv_data = b"email\nalice@example.com\n"
    resp = client.post(
        "/api/converter/preview",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={"email_column": "email", "attribute_mode": "nonsense"},
    )
    assert resp.status_code == 400


def test_preview_endpoint_rejects_bad_attribute_columns_json(client):
    csv_data = b"email\nalice@example.com\n"
    resp = client.post(
        "/api/converter/preview",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={"email_column": "email", "attribute_columns": "{not json}"},
    )
    assert resp.status_code == 400


def test_convert_endpoint_auto_mode_preserves_every_column(client):
    csv_data = b"email,name,company,city\nalice@example.com,Alice,Acme,Paris\n"
    resp = client.post(
        "/api/converter/convert",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={"email_column": "email", "name_column": "name"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    stats = json.loads(resp.headers["x-conversion-stats"])
    assert stats["converted"] == 1
    assert stats["attribute_columns"] == ["company", "city"]
    rows = list(csv.DictReader(resp.text.splitlines()))
    assert json.loads(rows[0]["attributes"]) == {"company": "Acme", "city": "Paris"}


def test_convert_endpoint_reports_invalid_emails(client):
    csv_data = b"email,name\nnope,X\nalice@example.com,Alice\n"
    resp = client.post(
        "/api/converter/convert",
        files={"file": ("t.csv", csv_data, "text/csv")},
        data={"email_column": "email"},
    )
    assert resp.status_code == 200
    stats = json.loads(resp.headers["x-conversion-stats"])
    assert stats["converted"] == 1
    assert stats["invalid_emails"] == 1