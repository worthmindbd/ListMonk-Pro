import csv
import json
import pytest

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
