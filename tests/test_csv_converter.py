import csv
import json

from app.services.csv_converter import detect_columns, convert_csv


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
