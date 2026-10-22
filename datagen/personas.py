"""Hand-scripted demo customers. Each one exists to make specific journeys from the brief demonstrable.

| Customer      | Built to answer                                                                 |
|---------------|---------------------------------------------------------------------------------|
| Priya Sharma  | "Why did my EMI increase?", failed payments in 6 months, prepay Rs 50,000, waiver |
| Arjun Mehta   | Prepay blocked (lock-in + overdue), foreclose blocked (lock-in); card late fee     |
| Kavya Iyer    | Rate hike extended tenure, not EMI; duplicate debit with breached SLA; card dispute |
| Rohan Gupta   | Two loans (which one?); "what are my options" with prepayment charges              |
"""
from __future__ import annotations

from datetime import date

from shared.finance import PartPrepayment

from .cards import CardSpec, ScriptedTxn, StatementScript
from .loans import LoanSpec, Outcome

CUSTOMERS = [
    {
        "customer_id": "CUST100001", "first_name": "Priya", "last_name": "Sharma", "dob": "1988-03-14", "gender": "F",
        "phone": "+91-9800000001", "email": "priya.sharma@example.com", "city": "Gurugram", "state": "Haryana",
        "pincode": "122002", "preferred_language": "en", "segment": "AFFLUENT", "customer_since": "2023-06-18",
        "risk_profile": "GOOD",
    },
    {
        "customer_id": "CUST100002", "first_name": "Arjun", "last_name": "Mehta", "dob": "1994-11-02", "gender": "M",
        "phone": "+91-9800000002", "email": "arjun.mehta@example.com", "city": "Noida", "state": "Uttar Pradesh",
        "pincode": "201301", "preferred_language": "hi", "segment": "MASS", "customer_since": "2024-07-01",
        "risk_profile": "STRESSED",
    },
    {
        "customer_id": "CUST100003", "first_name": "Kavya", "last_name": "Iyer", "dob": "1985-07-22", "gender": "F",
        "phone": "+91-9800000003", "email": "kavya.iyer@example.com", "city": "Chandigarh", "state": "Chandigarh",
        "pincode": "160017", "preferred_language": "en", "segment": "AFFLUENT", "customer_since": "2024-02-10",
        "risk_profile": "GOOD",
    },
    {
        "customer_id": "CUST100004", "first_name": "Rohan", "last_name": "Gupta", "dob": "1990-01-30", "gender": "M",
        "phone": "+91-9800000004", "email": "rohan.gupta@example.com", "city": "Delhi", "state": "Delhi",
        "pincode": "110017", "preferred_language": "hi", "segment": "AFFLUENT", "customer_since": "2022-11-25",
        "risk_profile": "GOOD",
    },
]

LOANS = [
    LoanSpec(
        loan_id="HL1000001", customer_id="CUST100001", product="HOME_LOAN", principal=4_500_000, tenure_months=240,
        disbursal_date=date(2023, 6, 18), first_due_date=date(2023, 8, 5), rate_type="FLOATING", spread=1.0,
        reset_mode="EMI", bank="HDFC Bank", account_last4="4417",
        prepayments=[PartPrepayment(date(2025, 3, 20), 200_000, "REDUCE_TENURE")],
        scripted={
            (2025, 8): Outcome("CUSTOMER_PAID", "INSUFFICIENT_FUNDS", 9, "UPI"),   # >12 months ago: waiver still possible
            (2026, 5): Outcome("REPRESENT_OK", "INSUFFICIENT_FUNDS", 5),           # chargeable, inside 6-month window
            (2026, 8): Outcome("REPRESENT_OK", "BANK_TECHNICAL_ERROR", 3),         # bank-side: charge waived (LP-CHG-002)
        },
    ),
    LoanSpec(
        loan_id="PL1000002", customer_id="CUST100002", product="PERSONAL_LOAN", principal=600_000, tenure_months=36,
        disbursal_date=date(2026, 1, 20), first_due_date=date(2026, 3, 5), rate_type="FIXED", fixed_rate=14.5,
        bank="Axis Bank", account_last4="9021",
        scripted={(2026, 10): Outcome("UNPAID", "INSUFFICIENT_FUNDS")},
    ),
    LoanSpec(
        loan_id="LP1000003", customer_id="CUST100003", product="LAP", principal=3_000_000, tenure_months=144,
        disbursal_date=date(2024, 2, 10), first_due_date=date(2024, 4, 5), rate_type="FLOATING", spread=2.25,
        reset_mode="TENURE", bank="ICICI Bank", account_last4="7730",
        scripted={(2026, 10): Outcome("DUPLICATE")},
    ),
    LoanSpec(
        loan_id="HL1000004", customer_id="CUST100004", product="HOME_LOAN", principal=6_200_000, tenure_months=300,
        disbursal_date=date(2022, 11, 25), first_due_date=date(2023, 1, 5), rate_type="FLOATING", spread=0.6,
        reset_mode="TENURE", bank="State Bank of India", account_last4="1186",
    ),
    LoanSpec(
        loan_id="PL1000005", customer_id="CUST100004", product="PERSONAL_LOAN", principal=800_000, tenure_months=48,
        disbursal_date=date(2024, 3, 12), first_due_date=date(2024, 4, 5), rate_type="FIXED", fixed_rate=12.75,
        bank="State Bank of India", account_last4="1186",
    ),
]

CARDS = [
    CardSpec(
        card_id="CC1000001", customer_id="CUST100002", product="Everyday Cashback", network="RUPAY", last4="5521",
        credit_limit=150_000, autopay_mode="FULL", behaviour="TRANSACTOR", spend_level=0.25,
        bank="Axis Bank", account_last4="9021", late_probability=0.0,
        scripted_statements={(2026, 9): StatementScript("AUTOPAY_FAILED", 4, "INSUFFICIENT_FUNDS")},
        scripted_txns=[ScriptedTxn(date(2026, 8, 25), "Croma", "ELECTRONICS", 14_999.0)],
    ),
    CardSpec(
        card_id="CC1000002", customer_id="CUST100003", product="Platinum Rewards", network="VISA", last4="8834",
        credit_limit=300_000, autopay_mode="FULL", behaviour="TRANSACTOR", spend_level=0.8,
        bank="ICICI Bank", account_last4="7730", late_probability=0.0,
        scripted_txns=[ScriptedTxn(date(2026, 10, 14), "QuickMart Online", "ONLINE_SHOPPING", 8_499.0,
                                   dispute={"status": "OPEN", "raised_on": "2026-10-15",
                                            "reason": "Transaction not recognised by cardholder",
                                            "ticket_id": "SR0000003"})],
    ),
]

# Hand-written CRM interactions. Amounts in notices are filled from generated data at build time.
INTERACTIONS = [
    ("CUST100001", "2025-08-05", "SMS", "OUTBOUND", "EMI auto-debit failed: insufficient funds.", "NOTIFIED", None),
    ("CUST100001", "2026-05-05", "SMS", "OUTBOUND", "EMI auto-debit failed: insufficient funds. Re-presentation on 10-May.", "NOTIFIED", None),
    ("CUST100001", "2026-05-07", "CALL", "INBOUND", "Asked about failed May EMI. Informed about re-presentation and bounce charge of Rs 590.", "RESOLVED", None),
    ("CUST100001", "2026-09-18", "EMAIL", "OUTBOUND", "{rate_notice}", "NOTIFIED", None),
    ("CUST100001", "2026-10-06", "CALL", "INBOUND", "Customer asked why EMI increased this month. Transferred from IVR to loans desk; call disconnected after 9 minutes on hold.", "ABANDONED", None),
    ("CUST100002", "2026-10-02", "SMS", "OUTBOUND", "Credit card autopay failed: insufficient funds. Please pay by another channel.", "NOTIFIED", None),
    ("CUST100002", "2026-10-05", "SMS", "OUTBOUND", "EMI auto-debit failed: insufficient funds.", "NOTIFIED", None),
    ("CUST100002", "2026-10-10", "SMS", "OUTBOUND", "EMI re-presentation failed. Please pay the overdue amount to avoid further charges.", "NOTIFIED", None),
    ("CUST100002", "2026-10-15", "CALL", "OUTBOUND", "Collections reminder for October EMI. Customer promised to pay by 25-Oct-2026.", "PROMISE_TO_PAY", None),
    ("CUST100003", "2026-10-07", "CALL", "INBOUND", "October EMI debited twice from ICICI account. Dispute raised.", "TICKET_CREATED", "SR0000001"),
    ("CUST100003", "2026-10-14", "CHAT", "INBOUND", "Asked for status of duplicate debit refund. Told it is under process.", "FOLLOW_UP", "SR0000001"),
    ("CUST100003", "2026-10-15", "CALL", "INBOUND", "Reported unrecognised card transaction of Rs 8,499 at QuickMart Online.", "TICKET_CREATED", "SR0000003"),
    ("CUST100004", "2026-04-10", "APP", "INBOUND", "Requested repayment statement for FY 2025-26 for home loan.", "TICKET_CREATED", "SR0000002"),
]

TICKETS = [
    {
        "ticket_id": "SR0000001", "customer_id": "CUST100003", "category": "PAYMENT_DISPUTE",
        "related_entity": {"type": "LOAN", "id": "LP1000003"}, "status": "IN_PROGRESS", "priority": "HIGH",
        "channel": "CALL", "created_at": "2026-10-07T11:42:10+05:30",
        "description": "October 2026 EMI debited twice via NACH on 05-Oct-2026. Customer requests refund of the duplicate debit.",
        "assigned_team": "PAYMENTS_OPS", "resolution": None, "resolved_at": None,
        "history": [
            {"at": "2026-10-07T11:42:10+05:30", "event": "CREATED", "by": "agent:AG-2231", "note": "Raised on call"},
            {"at": "2026-10-09T15:05:00+05:30", "event": "ASSIGNED", "by": "system", "note": "Assigned to PAYMENTS_OPS"},
            {"at": "2026-10-14T12:10:00+05:30", "event": "CUSTOMER_FOLLOW_UP", "by": "chat", "note": "Customer asked for status"},
        ],
    },
    {
        "ticket_id": "SR0000002", "customer_id": "CUST100004", "category": "STATEMENT_REQUEST",
        "related_entity": {"type": "LOAN", "id": "HL1000004"}, "status": "CLOSED", "priority": "LOW",
        "channel": "APP", "created_at": "2026-04-10T09:15:44+05:30",
        "description": "Repayment statement for FY 2025-26.", "assigned_team": "LOAN_SERVICING",
        "resolution": "Statement emailed to registered address.", "resolved_at": "2026-04-10T09:16:02+05:30",
        "history": [
            {"at": "2026-04-10T09:15:44+05:30", "event": "CREATED", "by": "app", "note": None},
            {"at": "2026-04-10T09:16:02+05:30", "event": "RESOLVED", "by": "system", "note": "Auto-generated and emailed"},
        ],
    },
    {
        "ticket_id": "SR0000003", "customer_id": "CUST100003", "category": "CARD_DISPUTE",
        "related_entity": {"type": "CARD", "id": "CC1000002"}, "status": "OPEN", "priority": "MEDIUM",
        "channel": "CALL", "created_at": "2026-10-15T18:20:31+05:30",
        "description": "Cardholder does not recognise Rs 8,499 transaction at QuickMart Online on 14-Oct-2026.",
        "assigned_team": "CARD_DISPUTES", "resolution": None, "resolved_at": None,
        "history": [{"at": "2026-10-15T18:20:31+05:30", "event": "CREATED", "by": "agent:AG-1187", "note": None}],
    },
]
