"""Repayment statements: issued by the (mock) lender from its records, downloadable only by the owner."""

from engine.core import execute
from tests.test_server import H, api, events, login  # noqa: F401  (fixture + helpers)


async def test_statement_defaults_to_last_financial_year_and_totals_match_schedule(session):
    s = session("CUST100001")
    r = await execute(s, "request_repayment_statement", {"loan_id": "HL1000001"})
    d = r["data"]
    assert d["document"]["financial_year"] == "2025-26" and d["document"]["file_name"].endswith(".pdf")
    t = d["totals"]
    assert (t["instalments_due"], t["instalments_paid"]) == (12, 12)
    assert (t["interest_paid"], t["principal_paid"]) == (387564.42, 114668.58)
    assert t["charges_paid"] == 835.44  # the August bounce charge + late interest
    assert d["fee"] == 0 and "policy:LP-SVC-002 v1" in r["sources"]


async def test_current_year_statement_is_year_to_date(session):
    r = await execute(session("CUST100001"), "request_repayment_statement",
                      {"loan_id": "HL1000001", "financial_year": "2026-27"})
    t = r["data"]["totals"]
    assert t["period_end"] == "2026-10-22" and t["instalments_due"] == 7  # Apr..Oct 2026


async def test_invalid_financial_year_is_refused(session):
    r = await execute(session("CUST100001"), "request_repayment_statement",
                      {"loan_id": "HL1000001", "financial_year": "2025-27"})
    assert not r["ok"] and r["error"] == "INVALID_FINANCIAL_YEAR"


async def test_cannot_request_another_customers_statement(session):
    r = await execute(session("CUST100002"), "request_repayment_statement", {"loan_id": "HL1000001"})
    assert r["error"] == "NOT_FOUND"


async def test_statement_streams_a_document_and_only_the_owner_can_download_it(api):  # noqa: F811
    priya = (await login(api, "+919800000001"))["token"]
    ev = events((await api.post("/api/messages", headers=H(priya), json={"text": "Send me my repayment statement"})).text)
    doc = next(d for e, d in ev if e == "document")
    assert doc["loan_id"] == "HL1000001" and doc["financial_year"] == "2025-26"
    assert "ready to download" in ev[-1][1]["text"]

    pdf = await api.get(f"/api/documents/{doc['document_id']}", headers=H(priya))
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:5] == b"%PDF-"

    arjun = (await login(api, "+919800000002"))["token"]
    assert (await api.get(f"/api/documents/{doc['document_id']}", headers=H(arjun))).status_code == 404
    assert (await api.get(f"/api/documents/{doc['document_id']}")).status_code == 401
