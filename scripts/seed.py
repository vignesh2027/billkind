"""Seed demo users, the verified biller registry and open bills.

Usage: python -m scripts.seed [--reset] [--refresh-demo]

All people, billers and bills are fictional. Nothing is pre-paid: every "cleared" number on the
live wall comes from payments made in the running app.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.ai import redact  # noqa: E402
from app.config import settings  # noqa: E402
from app.core import billers  # noqa: E402
from app.core.money import USD_RATES, convert, fmt, settlement_for, to_minor  # noqa: E402
from app.web.auth import hash_password  # noqa: E402

DEMO_PASSWORD = "billkind-demo"
BILLER_EMAIL = os.environ.get("BILLER_SANDBOX_EMAIL", "").strip()

USERS = [
    ("U-SPONSOR", "sponsor@billkind.demo", "Sam Rivera", "sponsor", "US", "USD", {"categories": [], "urgent_first": True}),
    ("U-BIG", "bigsponsor@billkind.demo", "Morgan Hale", "sponsor", "US", "USD",
     {"categories": ["school_fees", "medical"], "urgent_first": True}),
    ("U-POSTER", "poster@billkind.demo", "Kavya Raman", "poster", "IN", "INR", {}),
    ("U-BILLER", "biller@billkind.demo", "Biller desk", "biller", "US", "USD", {}),
    ("U-ADMIN", "admin@billkind.demo", "Operations", "admin", "US", "USD", {}),
]

# (id, name, category, country, city, aliases)
BILLERS = [
    ("BLR-SUNRISE", "Sunrise Public School", "school_fees", "IN", "Chennai", ["Sunrise Public School Chennai"]),
    ("BLR-LAKESIDE", "Lakeside Power & Light", "electricity", "US", "Detroit", ["Lakeside Power and Light"]),
    ("BLR-STCLARE", "St. Clare Community Clinic", "medical", "PH", "Quezon City", ["St Clare Clinic"]),
    ("BLR-HILLS", "Nairobi Hills Academy", "school_fees", "KE", "Nairobi", []),
    ("BLR-KASARANI", "Kasarani Water Services", "water", "KE", "Nairobi", []),
    ("BLR-IKEJA", "Ikeja Grid Electricity", "electricity", "NG", "Lagos", ["Ikeja Grid"]),
    ("BLR-MERCY", "Mercy Family Hospital", "medical", "NG", "Lagos", []),
    ("BLR-VERDE", "Ciudad Verde Electrica", "electricity", "MX", "Guadalajara", []),
    ("BLR-RIVERSIDE", "Riverside Water", "water", "GB", "Leeds", []),
    ("BLR-HARBOR", "Harbor View Apartments", "rent", "US", "Baltimore", []),
    ("BLR-BAYAN", "Bayanihan Telecom", "phone", "PH", "Cebu", []),
    ("BLR-KOTA", "Kota Sehat Clinic", "medical", "ID", "Surabaya", []),
    ("BLR-ACCRA", "Accra Bright Futures School", "school_fees", "GH", "Accra", []),
    ("BLR-SOWETO", "Soweto Community Power", "electricity", "ZA", "Johannesburg", []),
    ("BLR-ROSE", "Rosengarten Stadtwerke", "electricity", "DE", "Leipzig", []),
    ("BLR-COAST", "Coastline Pediatrics", "medical", "US", "Tampa", []),
]

# (poster alias, biller id, amount, currency, due in days, story)
BILLS = [
    ("Wanjiru", "BLR-HILLS", "4800", "KES", 5, "A mother of two in Nairobi is covering her daughter's term fees after a slow month at her market stall. The fee is due before the new term starts."),
    ("", "BLR-KASARANI", "1650", "KES", 3, "A retired teacher in Nairobi is behind on the household water bill after a hospital stay. The supply is cut if it stays unpaid."),
    ("Tunde", "BLR-IKEJA", "38500", "NGN", 2, "A tailor in Lagos fell behind on the electricity bill after a quiet season. He needs the power on to keep taking orders."),
    ("", "BLR-MERCY", "62000", "NGN", 9, "A father in Lagos is paying the balance on his son's treatment for a broken arm. The hospital asked for it before the follow-up visit."),
    ("Ana", "BLR-STCLARE", "4200", "PHP", 6, "A caregiver in Quezon City is settling her mother's clinic balance after a week of tests. She is paying the rest herself."),
    ("", "BLR-BAYAN", "999", "PHP", 4, "A student in Cebu needs her phone line for online classes and job applications. The account is two weeks past due."),
    ("Luis", "BLR-VERDE", "1240", "MXN", 3, "A family in Guadalajara is catching up on the electricity bill after a medical expense took this month's savings."),
    ("", "BLR-RIVERSIDE", "46.80", "GBP", 7, "A pensioner in Leeds is asking for help with this quarter's water bill. Her heating costs went up sharply this autumn."),
    ("Dee", "BLR-HARBOR", "250.00", "USD", 5, "A home health aide in Baltimore is short on this month's rent after her hours were cut. She has covered most of it already; this is the remainder."),
    ("", "BLR-COAST", "64.00", "USD", 8, "A parent in Tampa has a balance left after their toddler's ear infection visit. Insurance covered the rest."),
    ("Kavitha", "BLR-SUNRISE", "2800", "INR", 6, "A single mother in Chennai is paying her son's exam fee. Her tailoring income dropped this month."),
    ("", "BLR-ACCRA", "540", "GHS", 10, "A grandmother in Accra is raising two grandchildren and needs help with one term's school fees."),
    ("Sipho", "BLR-SOWETO", "780", "ZAR", 4, "A household in Johannesburg needs prepaid electricity restored. The family includes a newborn."),
    ("", "BLR-KOTA", "425000", "IDR", 6, "A young father in Surabaya is paying for his wife's prenatal checkups at a community clinic."),
    ("", "BLR-ROSE", "58.40", "EUR", 12, "A student in Leipzig is behind on her electricity after her part-time job ended. She starts a new one next month."),
    ("Joy", "BLR-HILLS", "3200", "KES", 8, "A father in Nairobi is short on his daughter's lunch and exam fees for the term."),
]


def _ensure_poster(pid: str, email: str, name: str, currency: str, now: float) -> None:
    if not db.one("SELECT 1 FROM users WHERE id = ?", (pid,)):
        db.insert("users", {"id": pid, "email": email, "name": name, "role": "poster", "country": "",
                            "currency": currency, "prefs": "{}", "password_hash": hash_password(DEMO_PASSWORD),
                            "created_at": now - 60 * 86400})


def list_demo_bill(i: int, bid: str, poster_id: str, rnd: random.Random, now: float, *, listed_at: float,
                   refill: bool = False) -> str:
    """List fictional demo bill number i from BILLS, due a few days from today. Returns the bill ID."""
    alias, biller_id, amount, cur, days, story = BILLS[i]
    b = db.one("SELECT * FROM billers WHERE id = ?", (biller_id,))
    amount_minor = to_minor(amount, cur)
    settle = settlement_for(cur)
    settle_minor = amount_minor if settle == cur else convert(amount_minor, cur, settle)
    fx = "" if settle == cur else (f"{cur} is not a PayPal settlement currency, so this bill settles in {settle} at an "
                                   f"indicative {USD_RATES[cur]} {cur} per USD ({fmt(settle_minor, settle)}).")
    account = f"{rnd.randint(10**7, 10**8 - 1)}"
    extraction = {"source": "seed", "story_source": "seed", "template": i}
    if refill:
        extraction["demo_refill"] = True
    db.insert("bills", {
        "id": bid, "poster_id": poster_id, "biller_id": biller_id, "biller_name": b["name"], "category": b["category"],
        "country": b["country"], "city": b["city"], "currency": cur, "amount_minor": amount_minor,
        "settle_currency": settle, "settle_minor": settle_minor, "fx_note": fx,
        "due_date": (dt.date.today() + dt.timedelta(days=days)).isoformat(),
        "account_masked": redact.mask_account(account), "account_hash": redact.account_hash(account, biller_id),
        "account_private": account, "status": "LISTED", "risk_score": rnd.choice([0, 0, 5, 5, 10]),
        "risk_reasons": "[]", "decision": "list", "extraction": json.dumps(extraction),
        "story": story, "story_consent": 1, "alias": alias, "created_at": listed_at,
        "updated_at": now, "listed_at": listed_at,
    })
    return bid


def refill_demo_bill(i: int, now: float) -> str:
    """List a fresh copy of demo bill i under a new ID and a new fictional household."""
    rnd = random.SystemRandom()
    while True:
        suffix = "".join(rnd.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=4))
        bid = f"BK-D{suffix}"
        if not db.one("SELECT 1 FROM bills WHERE id = ?", (bid,)):
            break
    pid = f"U-D{suffix}"
    _ensure_poster(pid, f"poster-d{suffix.lower()}@billkind.demo", BILLS[i][0] or "Demo household", BILLS[i][3], now)
    return list_demo_bill(i, bid, pid, rnd, now, listed_at=now, refill=True)


def reset() -> None:
    path = Path(settings.db_path)
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if p.exists():
            p.unlink()


def seed() -> None:
    db.connect()
    now = db.now()
    for uid, email, name, role, country, cur, prefs in USERS:
        if not db.one("SELECT 1 FROM users WHERE id = ?", (uid,)):
            db.insert("users", {"id": uid, "email": email, "name": name, "role": role, "country": country,
                                "currency": cur, "prefs": json.dumps(prefs), "password_hash": hash_password(DEMO_PASSWORD),
                                "created_at": now - 30 * 86400})
    for bid, name, cat, country, city, aliases in BILLERS:
        if not db.one("SELECT 1 FROM billers WHERE id = ?", (bid,)):
            slug = bid.split("-", 1)[1].lower()
            email = BILLER_EMAIL or f"{slug}-biller@business.example.com"
            billers.add(name, cat, country, city, email, True, aliases, user_id="U-BILLER", biller_id=bid)
    # One unverified biller to show the verification step.
    if not db.one("SELECT 1 FROM billers WHERE id = 'BLR-NEWLEAF'"):
        billers.add("New Leaf Montessori", "school_fees", "IN", "Bengaluru", BILLER_EMAIL or "newleaf-biller@business.example.com",
                    False, [], user_id="U-BILLER", biller_id="BLR-NEWLEAF")
    rnd = random.Random(7)
    for i in range(len(BILLS)):
        pid = f"U-P{i:02d}"
        _ensure_poster(pid, f"poster{i:02d}@billkind.demo", BILLS[i][0] or f"Poster {i}", BILLS[i][3], now)
        bid = f"BK-S{i:03d}"
        if db.one("SELECT 1 FROM bills WHERE id = ?", (bid,)):
            continue
        list_demo_bill(i, bid, pid, rnd, now, listed_at=now - (20 - i) * 3600)
    print(f"seeded: {db.one('SELECT COUNT(*) AS n FROM users')['n']} users, "
          f"{db.one('SELECT COUNT(*) AS n FROM billers')['n']} billers, {db.one('SELECT COUNT(*) AS n FROM bills')['n']} bills")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    seed()
    if "--refresh-demo" in sys.argv:
        # Re-list fictional demo bills that are no longer open and give the big sponsor's giving agent a fresh
        # monthly budget if it is used up. Sandbox and local fake only.
        from app.core import automation
        print("refreshed:", automation.demo_heal(force=True))
