"""Billkind web app: FastAPI with server-rendered Jinja2 pages."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .ai import agent, bulk, llm, support
from .ai.intake import CATEGORY_LABELS
from .config import settings
from .core import automation, events, funding, jobs, notify, posting, states, webhook_handlers
from .core.money import FX_AS_OF, USD_RATES, fmt, to_minor
from .paypal import fake, webhooks
from .paypal.client import PayPalError, client
from .web import auth, insights_routes, views

log = logging.getLogger("billkind")
HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "web" / "templates"))
templates.env.globals.update(fmt=fmt, country_name=views.country_name, category_label=views.category_label,
                             settings=settings, FX_AS_OF=FX_AS_OF, BILL_LABELS=states.BILL_LABELS)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    task = asyncio.create_task(jobs.loop()) if settings.background_jobs else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Billkind", lifespan=lifespan, docs_url=None, redoc_url=None)
# Compress text responses (CSS, JS, HTML). Server-sent events are excluded by Starlette's middleware.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax",
                   https_only=settings.app_base_url.startswith("https"), max_age=14 * 86400)
# The AR model files: Scene Viewer reads the glTF type, and iOS Quick Look needs the USDZ type to open the file.
# WebP and woff2 are listed too: slim server images have no mime.types file and would send them as text/plain.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/vnd.usdz+zip", ".usdz")
app.mount("/static", StaticFiles(directory=str(HERE / "web" / "static")), name="static")
if settings.is_fake:
    app.include_router(fake.router)
app.include_router(insights_routes.router)


@app.exception_handler(auth.LoginRequired)
async def _login_required(request: Request, exc: auth.LoginRequired):
    return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    """Browsers get a designed page for 403 and 404; API clients keep the JSON error."""
    if request.method == "GET" and exc.status_code in (403, 404) and "text/html" in request.headers.get("accept", ""):
        if exc.status_code == 404:
            title, message = "We could not find that page", "It may have moved, or it belongs to another account."
        else:
            title, message = "This page is for another kind of account", "Sign in with a different demo account to see it."
        resp = render(request, "error.html", code=exc.status_code, title=title, message=message)
        resp.status_code = exc.status_code
        return resp
    return await http_exception_handler(request, exc)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    user = auth.current_user(request)
    ctx.update(request=request, user=user, csrf=auth.csrf_token(request),
               unread=notify.unread(user["id"]) if user else 0, paypal_mode=settings.paypal_mode)
    return templates.TemplateResponse(request, name, ctx)


def stats() -> dict:
    paid = db.one("SELECT COUNT(*) AS n FROM bills WHERE status = 'PAID'")["n"]
    countries = db.one("SELECT COUNT(DISTINCT country) AS n FROM bills WHERE status = 'PAID'")["n"]
    sponsors = db.one("SELECT COUNT(DISTINCT sponsor_id) AS n FROM contributions WHERE status IN "
                      "('CAPTURED','PARTIALLY_REFUNDED')")["n"]
    totals = db.q("SELECT settle_currency AS c, SUM(settle_minor) AS m FROM bills WHERE status = 'PAID' "
                  "GROUP BY settle_currency ORDER BY m DESC")
    live = db.one("SELECT COUNT(*) AS n FROM bills WHERE status IN ('LISTED','FUNDING')")["n"]
    return {"paid": paid, "countries": countries, "sponsors": sponsors, "live": live,
            "totals": [fmt(t["m"], t["c"]) for t in totals]}


# Public pages ------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    sample = db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') ORDER BY due_date LIMIT 3")
    by_country = db.q("SELECT country, COUNT(*) AS n FROM bills WHERE status = 'PAID' GROUP BY country ORDER BY n DESC")
    open_by_country = db.q("SELECT country, COUNT(*) AS n FROM bills WHERE status IN ('LISTED','FUNDING') "
                           "GROUP BY country")
    return render(request, "home.html", stats=stats(), cards=[views.card(b) for b in sample],
                  feed=events.recent_public(6), by_country=by_country, open_by_country=open_by_country,
                  countries=views.COUNTRIES)


@app.get("/live", response_class=HTMLResponse)
async def live(request: Request):
    by_country = db.q("SELECT country, COUNT(*) AS n FROM bills WHERE status = 'PAID' GROUP BY country ORDER BY n DESC")
    open_by_country = db.q("SELECT country, COUNT(*) AS n FROM bills WHERE status IN ('LISTED','FUNDING') "
                           "GROUP BY country")
    feed = events.recent_public(40)
    return render(request, "live.html", stats=stats(), by_country=by_country, open_by_country=open_by_country,
                  feed=feed, countries=views.COUNTRIES)


@app.get("/live/stats")
async def live_stats():
    return stats()


@app.get("/events/stream")
async def stream(request: Request):
    user = auth.current_user(request)
    q = events.broker.subscribe(user["id"] if user else None)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                out = {k: ev.get(k) for k in ("id", "ts", "kind", "bill_id", "country", "city", "category",
                                              "currency", "amount_minor", "message", "body", "msg_kind")}
                if ev.get("amount_minor"):
                    out["amount"] = fmt(ev["amount_minor"], ev.get("currency") or "USD")
                out["mine"] = bool(user and user["id"] in (ev.get("audience") or []))
                out["country_name"] = views.country_name(ev.get("country") or "")
                yield f"event: {ev.get('kind', 'message')}\ndata: {json.dumps(out)}\n\n"
        finally:
            events.broker.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/how", response_class=HTMLResponse)
async def how(request: Request):
    return render(request, "how.html")


# Auth ----------------------------------------------------------------------------------

DEMO_ACCOUNTS = [
    ("sponsor@billkind.demo", "Sponsor", "Swipe and pay bills"),
    ("bigsponsor@billkind.demo", "Big sponsor", "Clear many bills at once, run a giving agent"),
    ("poster@billkind.demo", "Poster", "Post a bill and see it paid"),
    ("biller@billkind.demo", "Biller", "See payouts arrive"),
    ("admin@billkind.demo", "Admin", "Review queue, billers, webhooks"),
]


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, demo=DEMO_ACCOUNTS, error="")


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(""), next: str = Form("/"),
                demo: str = Form("")):
    user = db.one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    ok = user and (auth.check_password(password, user["password_hash"]) or
                   (demo == "1" and email.endswith("@billkind.demo")))
    if not ok:
        return render(request, "login.html", next=next, demo=DEMO_ACCOUNTS, error="Email or password is not right.")
    resp_cookie = None
    if demo == "1" and user["email"] == "poster@billkind.demo":
        # Each browser gets its own demo poster account, so testers do not trip each other's velocity limits.
        own = request.cookies.get("bk_demo_poster", "")
        mine = db.one("SELECT * FROM users WHERE id = ? AND role = 'poster'", (own,)) if own else None
        if not mine:
            uid = db.new_id("U-DEMO")
            db.insert("users", {"id": uid, "email": f"poster+{uid[-6:].lower()}@billkind.demo", "name": "Kavya Raman",
                                "role": "poster", "password_hash": user["password_hash"], "country": "IN",
                                "currency": "INR", "prefs": "{}", "created_at": db.now()})
            mine = db.one("SELECT * FROM users WHERE id = ?", (uid,))
            resp_cookie = uid
        user = mine
    request.session["uid"] = user["id"]
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    if next == "/":
        next = {"sponsor": "/deck", "poster": "/me", "admin": "/admin", "biller": "/biller"}.get(user["role"], "/")
    resp = RedirectResponse(next, status_code=303)
    if resp_cookie:
        resp.set_cookie("bk_demo_poster", resp_cookie, max_age=60 * 86400, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request):
    user = auth.require(request)
    msgs = notify.inbox(user["id"])
    db.run("UPDATE messages SET read = 1 WHERE user_id = ?", (user["id"],))
    return render(request, "inbox.html", msgs=msgs)


# Poster --------------------------------------------------------------------------------

CURRENCIES = sorted(USD_RATES)
SCAN_LIMIT = (8, 600)  # at most 8 bill readings per poster in 10 minutes (keeps the free AI tier for everyone)


@app.get("/post", response_class=HTMLResponse)
async def post_page(request: Request):
    auth.require(request, "poster")
    samples = sorted(p.name for p in (HERE.parent / "samples").glob("*") if p.suffix in (".jpg", ".png", ".pdf"))
    return render(request, "post.html", error="", samples=samples, drop_samples=DROP_SAMPLES,
                  categories=CATEGORY_LABELS, currencies=CURRENCIES)


@app.post("/post")
async def post_upload(request: Request, file: UploadFile = File(...), csrf: str = Form(""), device: str = Form("")):
    """No-JavaScript path: upload, then check the reading on its own page."""
    user = auth.require(request, "poster")
    auth.check_csrf(request, csrf)
    data = await file.read(posting.intake.MAX_BYTES + 1)
    try:
        bill = await posting.upload(user, data, file.filename or "", file.content_type or "", device=device[:64])
    except posting.PostingError as exc:
        return render(request, "post.html", error=str(exc), samples=[], drop_samples=DROP_SAMPLES,
                      categories=CATEGORY_LABELS, currencies=CURRENCIES)
    return RedirectResponse(f"/post/{bill['id']}", status_code=303)


@app.post("/api/post/scan")
async def post_scan(request: Request, file: UploadFile = File(...), device: str = Form("")):
    """Drop a bill: read one photo or PDF and return the pre-filled form with a confidence level per field.

    Creates a DRAFT bill that only its poster can see. Nothing is listed until the poster confirms on
    POST /post/{bill_id}, which runs the same nine checks as before.
    """
    user = auth.require(request, "poster")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    recent = db.one("SELECT COUNT(*) AS n FROM bills WHERE poster_id = ? AND created_at > ?",
                    (user["id"], db.now() - SCAN_LIMIT[1]))["n"]
    if recent >= SCAN_LIMIT[0]:
        return JSONResponse({"error": "That is a lot of bills in a few minutes. Please wait a little and try again."},
                            status_code=429)
    data = await file.read(posting.intake.MAX_BYTES + 1)
    try:
        bill = await posting.upload(user, data, file.filename or "", file.content_type or "", device=device[:64])
    except posting.PostingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return posting.scan_view(bill)


# "Try a sample bill": fictional bills generated fresh on every request (new account number, amount and due date),
# so a tester never trips the duplicate checks.
DROP_SAMPLES = {"electricity": "Electricity bill, Detroit", "school": "School fee receipt, Chennai",
                "water": "Water bill, Leeds", "clinic": "Clinic bill, Tampa"}


@app.get("/samples/fresh/{kind}.jpg")
async def fresh_drop_sample(kind: str):
    from fastapi.responses import Response
    from scripts.make_samples import fresh_bill
    if kind not in DROP_SAMPLES:
        raise HTTPException(404)
    data = await asyncio.to_thread(fresh_bill, kind)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store",
                    "Content-Disposition": f"inline; filename=sample-{kind}-bill.jpg"})


@app.get("/samples/fresh-school-bill.jpg")
async def fresh_sample():
    from fastapi.responses import Response
    from scripts.make_samples import fresh_sample as make
    data = await asyncio.to_thread(make)
    return Response(data, media_type="image/jpeg",
                    headers={"Content-Disposition": "attachment; filename=fresh-school-bill.jpg"})


@app.get("/samples/{name}")
async def sample_file(name: str):
    path = (HERE.parent / "samples" / name).resolve()
    if path.parent != (HERE.parent / "samples").resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/post/{bill_id}", response_class=HTMLResponse)
async def post_review(request: Request, bill_id: str):
    user = auth.require(request, "poster")
    bill = db.one("SELECT * FROM bills WHERE id = ? AND poster_id = ?", (bill_id, user["id"]))
    if not bill:
        raise HTTPException(404)
    if bill["status"] != "DRAFT":
        return RedirectResponse(f"/me#{bill_id}", status_code=303)
    ext = json.loads(bill["extraction"])
    return render(request, "review.html", bill=bill, ext=ext, error="", form={},
                  amount=(ext.get("amount") or ""), categories=CATEGORY_LABELS,
                  conf=ext.get("confidence_levels") or {})


@app.post("/post/{bill_id}")
async def post_submit(request: Request, bill_id: str):
    user = auth.require(request, "poster")
    form = dict(await request.form())
    auth.check_csrf(request, form.get("csrf"))
    try:
        bill = await posting.submit(user, bill_id, form)
    except posting.PostingError as exc:
        bill = db.one("SELECT * FROM bills WHERE id = ? AND poster_id = ?", (bill_id, user["id"]))
        if not bill:
            raise HTTPException(404)
        return render(request, "review.html", bill=bill, ext=json.loads(bill["extraction"]), error=str(exc),
                      form=form, amount=form.get("amount", ""), categories=CATEGORY_LABELS,
                      conf=json.loads(bill["extraction"]).get("confidence_levels") or {})
    return RedirectResponse(f"/me?posted={bill['id']}#{bill['id']}", status_code=303)


@app.get("/me", response_class=HTMLResponse)
async def poster_home(request: Request, posted: str = ""):
    user = auth.require(request, "poster")
    bills = db.q("SELECT * FROM bills WHERE poster_id = ? AND status != 'DRAFT' ORDER BY created_at DESC", (user["id"],))
    rows = []
    for b in bills:
        c = views.card(b, user["currency"])
        c["reasons"] = json.loads(b["risk_reasons"] or "[]")
        c["reject_reason"] = b["reject_reason"]
        c["story_source"] = json.loads(b["extraction"] or "{}").get("story_source", "")
        rows.append(c)
    return render(request, "me.html", bills=rows, posted=posted)


@app.post("/me/{bill_id}/withdraw")
async def poster_withdraw(request: Request, bill_id: str, csrf: str = Form("")):
    user = auth.require(request, "poster")
    auth.check_csrf(request, csrf)
    bill = db.one("SELECT * FROM bills WHERE id = ? AND poster_id = ?", (bill_id, user["id"]))
    if not bill:
        raise HTTPException(404)
    try:
        await funding.withdraw(bill_id, "You withdrew this bill.")
    except (funding.FundingError, states.IllegalTransition) as exc:
        notify.send(user["id"], "notice", "Could not withdraw", str(exc), bill_id=bill_id)
    return RedirectResponse(f"/me#{bill_id}", status_code=303)


@app.post("/me/{bill_id}/thanks")
async def poster_thanks(request: Request, bill_id: str, text: str = Form(...), csrf: str = Form(""),
                        polish: str = Form("")):
    user = auth.require(request, "poster")
    auth.check_csrf(request, csrf)
    from .ai import story
    clean = await story.polish_thanks(text, names=[user["name"]]) if polish else story.scrub(text, names=[user["name"]])[:260]
    try:
        funding.thank(bill_id, user["id"], clean)
    except funding.FundingError as exc:
        notify.send(user["id"], "notice", "Thank-you not sent", str(exc), bill_id=bill_id)
    return RedirectResponse(f"/me#{bill_id}", status_code=303)


# Sponsor -------------------------------------------------------------------------------

@app.get("/deck", response_class=HTMLResponse)
async def deck(request: Request):
    user = auth.require(request, "sponsor")
    return render(request, "deck.html", cards=views.deck_for(user), client_id=settings.paypal_client_id)


@app.post("/api/pass/{bill_id}")
async def api_pass(request: Request, bill_id: str):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    db.run("INSERT OR IGNORE INTO passes (sponsor_id, bill_id, created_at) VALUES (?, ?, ?)",
           (user["id"], bill_id, db.now()))
    return {"ok": True}


@app.post("/api/passes/reset")
async def api_pass_reset(request: Request):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    db.run("DELETE FROM passes WHERE sponsor_id = ?", (user["id"],))
    return {"ok": True}


@app.post("/api/orders")
async def api_create_order(request: Request):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    body = await request.json()
    try:
        picks = [(p["bill_id"], int(p["amount_minor"])) for p in body.get("picks", [])]
        result = await funding.start_payment(user, picks, source=body.get("source", "swipe")[:12])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "Malformed request."}, status_code=400)
    except funding.FundingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except PayPalError as exc:
        log.warning("create order failed: %s", exc)
        return JSONResponse({"error": "PayPal could not create the order. Please try again."}, status_code=502)
    return {"id": result["order_id"], "approve_url": result["approve_url"], "status": result["status"]}


@app.post("/api/orders/{order_id}/capture")
async def api_capture(request: Request, order_id: str):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    try:
        result = await funding.capture(order_id, user)
    except funding.FundingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except PayPalError as exc:
        if exc.issue == "INSTRUMENT_DECLINED":
            return JSONResponse({"error": "declined", "restart": True}, status_code=422)
        settled = funding.order_outcome(order_id, user)
        if settled["status"] == "COMPLETED":
            return settled
        return JSONResponse({"error": "PayPal could not complete the payment.", "issue": exc.issue}, status_code=502)
    except Exception:
        # Never report a failure for money that was captured (for example a webhook captured it at the same time).
        log.exception("capture %s raised", order_id)
        settled = funding.order_outcome(order_id, user)
        if settled["status"] == "COMPLETED":
            return settled
        return JSONResponse({"error": "We could not confirm this payment yet. Check My giving in a minute before "
                                      "trying again.", "unconfirmed": True}, status_code=500)
    return result


@app.get("/api/orders/{order_id}/status")
async def api_order_status(request: Request, order_id: str):
    user = auth.require(request, "sponsor")
    return funding.order_outcome(order_id, user)


@app.get("/paypal/return")
async def paypal_return(request: Request, token: str = ""):
    """Redirect flow (no popup): capture on return from buyer approval."""
    user = auth.require(request, "sponsor")
    try:
        await funding.capture(token, user)
    except (funding.FundingError, PayPalError):
        pass
    return RedirectResponse("/sponsor", status_code=303)


@app.get("/paypal/cancel")
async def paypal_cancel(request: Request):
    return RedirectResponse("/deck", status_code=303)


@app.get("/api/bills/{bill_id}")
async def api_bill(request: Request, bill_id: str):
    user = auth.require(request)
    bill = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not bill or bill["status"] in ("DRAFT", "SUBMITTED", "REVIEW", "REJECTED") and bill["poster_id"] != user["id"] \
            and user["role"] != "admin":
        raise HTTPException(404)
    return views.card(bill, user["currency"])


@app.get("/sponsor", response_class=HTMLResponse)
async def sponsor_home(request: Request):
    user = auth.require(request, "sponsor")
    return render(request, "sponsor.html", **sponsor_data(user))


@app.get("/sponsor/feed", response_class=HTMLResponse)
async def sponsor_feed(request: Request):
    user = auth.require(request, "sponsor")
    return render(request, "_sponsor_feed.html", **sponsor_data(user))


def sponsor_data(user: dict) -> dict:
    rows = db.q("SELECT c.*, b.status AS bill_status FROM contributions c JOIN bills b ON b.id = c.bill_id "
                "WHERE c.sponsor_id = ? AND c.status IN ('CAPTURED','PARTIALLY_REFUNDED','REFUNDED') "
                "ORDER BY c.captured_at DESC", (user["id"],))
    items = []
    seen = set()
    for r in rows:
        b = db.one("SELECT * FROM bills WHERE id = ?", (r["bill_id"],))
        c = views.card(b, user["currency"])
        c.update(contribution=r["id"], given=fmt(r["amount_minor"] - r["refunded_minor"], r["currency"]),
                 refunded=fmt(r["refunded_minor"], r["currency"]) if r["refunded_minor"] else "",
                 order_id=r["order_id"], capture_id=r["capture_id"], source=r["source"],
                 payout_item=b["payout_item_id"], first=b["id"] not in seen)
        seen.add(b["id"])
        items.append(c)
    totals = db.q("SELECT currency, SUM(amount_minor - refunded_minor) AS m FROM contributions WHERE sponsor_id = ? "
                  "AND status IN ('CAPTURED','PARTIALLY_REFUNDED') GROUP BY currency", (user["id"],))
    paid = db.one("SELECT COUNT(DISTINCT c.bill_id) AS n FROM contributions c JOIN bills b ON b.id = c.bill_id "
                  "WHERE c.sponsor_id = ? AND b.status = 'PAID' AND c.status IN ('CAPTURED','PARTIALLY_REFUNDED')",
                  (user["id"],))["n"]
    billers = db.one("SELECT COUNT(DISTINCT b.biller_id) AS n FROM contributions c JOIN bills b ON b.id = c.bill_id "
                     "WHERE c.sponsor_id = ? AND b.status = 'PAID'", (user["id"],))["n"]
    thanks = db.q("SELECT DISTINCT b.* FROM bills b JOIN contributions c ON c.bill_id = b.id WHERE c.sponsor_id = ? "
                  "AND b.thank_you != '' ORDER BY b.updated_at DESC LIMIT 6", (user["id"],))
    return {"items": items, "totals": [fmt(t["m"], t["currency"]) for t in totals], "paid_count": paid,
            "biller_count": billers, "thanks": thanks, "pledges": automation.pledges_for(user["id"]),
            "has_token": bool(automation.active_token(user["id"]))}


@app.get("/receipts/{contribution_id}", response_class=HTMLResponse)
async def receipt(request: Request, contribution_id: str):
    user = auth.require(request)
    c = db.one("SELECT * FROM contributions WHERE id = ?", (contribution_id,))
    if not c or (c["sponsor_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404)
    b = db.one("SELECT * FROM bills WHERE id = ?", (c["bill_id"],))
    item = db.one("SELECT * FROM payout_items WHERE bill_id = ? ORDER BY created_at DESC", (b["id"],))
    return render(request, "receipt.html", c=c, b=b, card=views.card(b, user["currency"]), item=item)


# Biller ----------------------------------------------------------------------------------

@app.get("/biller", response_class=HTMLResponse)
async def biller_home(request: Request):
    user = auth.require(request, "biller")
    mine = db.q("SELECT * FROM billers WHERE user_id = ?", (user["id"],))
    ids = [b["id"] for b in mine] or [""]
    marks = ",".join("?" for _ in ids)
    items = db.q(f"SELECT p.*, b.account_private, b.account_masked, b.category, b.due_date FROM payout_items p "
                 f"JOIN bills b ON b.id = p.bill_id WHERE b.biller_id IN ({marks}) ORDER BY p.created_at DESC", ids)
    return render(request, "biller.html", billers=mine, items=items)


# Admin -----------------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    auth.require(request, "admin")
    review = db.q("SELECT * FROM bills WHERE status = 'REVIEW' ORDER BY risk_score DESC")
    for b in review:
        b["reasons"] = json.loads(b["risk_reasons"] or "[]")
        b["ext"] = json.loads(b["extraction"] or "{}")
    failed = db.q("SELECT * FROM bills WHERE status = 'PAYOUT_FAILED'")
    billers = db.q("SELECT * FROM billers ORDER BY verified, name")
    hooks = db.q("SELECT id, event_type, received_at, verified, method, note FROM webhook_events "
                 "ORDER BY received_at DESC LIMIT 25")
    counts = {s: db.one("SELECT COUNT(*) AS n FROM bills WHERE status = ?", (s,))["n"] for s in states.BILL_LABELS}
    tickets = db.q("SELECT t.*, u.email FROM tickets t JOIN users u ON u.id = t.user_id ORDER BY t.status, t.created_at DESC LIMIT 20")
    for t in tickets:
        t["lines"] = json.loads(t["transcript"] or "[]")
    return render(request, "admin.html", review=review, failed=failed, billers=billers, hooks=hooks,
                  counts=counts, tickets=tickets, api_calls=list(reversed(client().calls[-30:])), ai_calls=list(reversed(llm.CALLS[-20:])))


@app.post("/admin/bills/{bill_id}/decide")
async def admin_decide(request: Request, bill_id: str, decision: str = Form(...), note: str = Form(""),
                       csrf: str = Form("")):
    auth.require(request, "admin")
    auth.check_csrf(request, csrf)
    try:
        posting.admin_decide(bill_id, decision == "approve", note)
    except posting.PostingError as exc:
        return render(request, "error.html", message=str(exc))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/bills/{bill_id}/payout")
async def admin_payout(request: Request, bill_id: str, action: str = Form(...), csrf: str = Form("")):
    auth.require(request, "admin")
    auth.check_csrf(request, csrf)
    try:
        if action == "retry":
            await funding.retry_payout(bill_id)
        else:
            await funding.withdraw(bill_id, "Our team could not deliver the payment to the biller, so every "
                                   "sponsor was refunded.", by_admin=True)
    except (funding.FundingError, PayPalError) as exc:
        return render(request, "error.html", message=str(exc))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/billers/{biller_id}/verify")
async def admin_verify_biller(request: Request, biller_id: str, csrf: str = Form("")):
    auth.require(request, "admin")
    auth.check_csrf(request, csrf)
    db.update("billers", "id", biller_id, verified=1)
    return RedirectResponse("/admin#billers", status_code=303)


@app.get("/admin/docs/{bill_id}")
async def admin_doc(request: Request, bill_id: str):
    auth.require(request, "admin")
    b = db.one("SELECT doc_path FROM bills WHERE id = ?", (bill_id,))
    if not b or not b["doc_path"] or not Path(b["doc_path"]).exists():
        raise HTTPException(404)
    return FileResponse(b["doc_path"])


# Webhooks ----------------------------------------------------------------------------------

@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    ok, method, note = await webhooks.verify(dict(request.headers), raw, event)
    fresh = webhook_handlers.record(event, ok, method, note)
    if not ok:
        log.warning("webhook %s rejected: %s", event.get("event_type"), note)
        return JSONResponse({"error": "signature verification failed"}, status_code=400)
    if not fresh:
        return {"ok": True, "duplicate": True}
    try:
        outcome = await webhook_handlers.handle(event)
    except Exception as exc:  # keep 500 so PayPal retries
        log.exception("webhook handling failed")
        db.run("UPDATE webhook_events SET note = note || ' | error ' || ? WHERE id = ?", (str(exc)[:200], event.get("id")))
        return JSONResponse({"error": "processing failed"}, status_code=500)
    return {"ok": True, "outcome": outcome}


# Big-sponsor mode ------------------------------------------------------------------------

@app.get("/sponsor/bulk", response_class=HTMLResponse)
async def bulk_page(request: Request):
    auth.require(request, "sponsor")
    return render(request, "bulk.html", client_id=settings.paypal_client_id)


@app.post("/api/bulk/preview")
async def bulk_preview(request: Request):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, request.headers.get("x-csrf"))
    body = await request.json()
    if body.get("text"):
        f, source = await bulk.parse(str(body["text"]))
    else:
        f, source = bulk.clean(body.get("filter") or {}), "form"
    sel = bulk.select(f, user["id"])
    cur = sel["currency"]
    picks = [{"bill_id": p["bill"]["id"], "amount_minor": p["amount_minor"], "amount": fmt(p["amount_minor"], cur),
              "category": views.category_label(p["bill"]["category"]), "where": p["bill"]["city"] or p["bill"]["country"],
              "country": views.country_name(p["bill"]["country"]), "biller": p["bill"]["biller_name"],
              "due": views.due_text(p["bill"]["due_date"])} for p in sel["picks"]]
    return {"filter": f, "source": source, "currency": cur, "picks": picks,
            "total": fmt(sel.get("total_minor", 0), cur) if cur else "", "total_usd": f"{sel.get('total_usd', 0):.2f}",
            "others": sel["others"]}


# Giving agent, vault, recurring ---------------------------------------------------------------

@app.get("/sponsor/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    user = auth.require(request, "sponsor")
    a = db.one("SELECT * FROM agents WHERE sponsor_id = ? ORDER BY created_at DESC", (user["id"],))
    decisions = []
    spent = None
    if a:
        decisions = [agent.decision_view(d) for d in db.q(
            "SELECT * FROM agent_decisions WHERE agent_id = ? ORDER BY created_at DESC LIMIT 40", (a["id"],))]
        spent = float(agent.month_spent_usd(a["id"], user["id"]))
    return render(request, "agent.html", agent=a, policy=json.loads(a["policy"]) if a else None,
                  decisions=decisions, spent=spent, token=automation.active_token(user["id"]),
                  period="since the demo budget was renewed" if a and agent.budget_renewed(user["id"]) else "this month",
                  draft=request.session.pop("agent_draft", None), pledges=automation.pledges_for(user["id"]))


@app.post("/sponsor/agent/parse")
async def agent_parse(request: Request, rules: str = Form(...), csrf: str = Form("")):
    auth.require(request, "sponsor")
    auth.check_csrf(request, csrf)
    policy, source = await agent.parse_rules(rules)
    request.session["agent_draft"] = {"rules": rules[:1000], "policy": policy, "source": source}
    return RedirectResponse("/sponsor/agent#confirm", status_code=303)


@app.post("/sponsor/agent/save")
async def agent_save(request: Request):
    user = auth.require(request, "sponsor")
    form = await request.form()
    auth.check_csrf(request, form.get("csrf"))
    policy = agent.validate({
        "monthly_budget_usd": form.get("monthly_budget_usd"), "per_bill_max_usd": form.get("per_bill_max_usd"),
        "categories": form.getlist("categories"), "max_risk": form.get("max_risk"),
        "countries": [c.strip() for c in str(form.get("countries", "")).split(",") if c.strip()],
        "urgent_first": True})
    db.run("UPDATE agents SET active = 0 WHERE sponsor_id = ?", (user["id"],))
    db.insert("agents", {"id": db.new_id("AGT"), "sponsor_id": user["id"], "rules_text": str(form.get("rules", ""))[:1000],
                         "policy": json.dumps(policy), "active": 1, "created_at": db.now(), "last_run": db.now()})
    return RedirectResponse("/sponsor/agent", status_code=303)


@app.post("/sponsor/agent/run")
async def agent_run(request: Request, csrf: str = Form("")):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, csrf)
    a = db.one("SELECT * FROM agents WHERE sponsor_id = ? AND active = 1", (user["id"],))
    if a and settings.demo_self_heal:
        automation.renew_demo_agent_budget(user["id"])
    if a:
        await agent.run(a)
    return RedirectResponse("/sponsor/agent#log", status_code=303)


@app.post("/sponsor/agent/stop")
async def agent_stop(request: Request, csrf: str = Form("")):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, csrf)
    db.run("UPDATE agents SET active = 0 WHERE sponsor_id = ?", (user["id"],))
    return RedirectResponse("/sponsor/agent", status_code=303)


@app.get("/sponsor/vault/connect")
async def vault_connect(request: Request, next: str = "/sponsor/agent"):
    user = auth.require(request, "sponsor")
    if not next.startswith("/") or next.startswith("//"):
        next = "/sponsor/agent"
    request.session["vault_next"] = next
    try:
        st = await client().create_setup_token(return_url=settings.app_base_url + "/sponsor/vault/return",
                                               cancel_url=settings.app_base_url + next)
    except PayPalError as exc:
        return render(request, "error.html", message=f"PayPal could not start saving your account: {exc}")
    db.insert("vault_tokens", {"id": db.new_id("VLT"), "sponsor_id": user["id"], "setup_token_id": st["id"],
                               "status": "PENDING", "created_at": db.now()})
    link = [lk["href"] for lk in st.get("links", []) if lk.get("rel") == "approve"]
    return RedirectResponse(link[0] if link else next, status_code=303)


@app.get("/sponsor/vault/return")
async def vault_return(request: Request, approval_token_id: str = ""):
    user = auth.require(request, "sponsor")
    row = db.one("SELECT * FROM vault_tokens WHERE sponsor_id = ? AND setup_token_id = ?", (user["id"], approval_token_id))
    nxt = request.session.pop("vault_next", "/sponsor/agent")
    if not row:
        return RedirectResponse(nxt, status_code=303)
    try:
        pt = await client().create_payment_token(approval_token_id)
    except PayPalError as exc:
        return render(request, "error.html", message=f"PayPal did not save the account: {exc}")
    db.run("UPDATE vault_tokens SET status = 'REPLACED' WHERE sponsor_id = ? AND status = 'ACTIVE'", (user["id"],))
    db.update("vault_tokens", "id", row["id"], payment_token_id=pt["id"], status="ACTIVE",
              payer_email=((pt.get("payment_source") or {}).get("paypal") or {}).get("email_address", ""))
    notify.send(user["id"], "notice", "PayPal account saved for automatic giving",
                "Your giving agent and monthly sponsorships can now pay bills within the limits you set. "
                "You can stop them at any time.")
    return RedirectResponse(nxt, status_code=303)


@app.post("/sponsor/recurring")
async def recurring_create(request: Request, bill_id: str = Form(...), amount: str = Form(...), csrf: str = Form("")):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, csrf)
    bill = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not bill:
        raise HTTPException(404)
    try:
        automation.create_pledge(user, bill_id, to_minor(amount, bill["settle_currency"]))
    except (funding.FundingError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    if not automation.active_token(user["id"]):
        return RedirectResponse("/sponsor/vault/connect?next=/sponsor", status_code=303)
    return RedirectResponse("/sponsor#monthly", status_code=303)


@app.post("/sponsor/recurring/{rid}/stop")
async def recurring_stop(request: Request, rid: str, csrf: str = Form("")):
    user = auth.require(request, "sponsor")
    auth.check_csrf(request, csrf)
    db.run("UPDATE recurring SET active = 0 WHERE id = ? AND sponsor_id = ?", (rid, user["id"]))
    return RedirectResponse("/sponsor#monthly", status_code=303)


# Support ---------------------------------------------------------------------------------------

@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    user = auth.require(request)
    tickets = db.q("SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (user["id"],))
    return render(request, "help.html", tickets=tickets, policies=support.POLICIES)


@app.post("/api/help")
async def help_ask(request: Request):
    user = auth.require(request)
    auth.check_csrf(request, request.headers.get("x-csrf"))
    body = await request.json()
    return await support.answer(user, str(body.get("question", "")), body.get("history") or [])


@app.post("/api/help/escalate")
async def help_escalate(request: Request):
    user = auth.require(request)
    auth.check_csrf(request, request.headers.get("x-csrf"))
    body = await request.json()
    tid = support.escalate(user, body.get("history") or [], str(body.get("subject", "")))
    return {"ticket": tid}


@app.post("/admin/tickets/{tid}")
async def admin_ticket(request: Request, tid: str, reply: str = Form(...), csrf: str = Form("")):
    auth.require(request, "admin")
    auth.check_csrf(request, csrf)
    support.resolve(tid, reply)
    return RedirectResponse("/admin#tickets", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "paypal_mode": settings.paypal_mode, "ai": settings.ai_enabled}
