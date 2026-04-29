from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import sqlite3
import stripe
import secrets
import requests
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

app = FastAPI()

# =========================
# CONFIG
# =========================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_live_replace_me")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_replace_me")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "replace_me")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "replace_me")

DEFAULT_PRODUCT_ID = "BUSY_ALL"
DB_PATH = "busytrader.db"

stripe.api_key = STRIPE_SECRET_KEY

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        stripe_customer_id TEXT UNIQUE,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        license_key TEXT NOT NULL UNIQUE,
        product_id TEXT NOT NULL,
        status TEXT NOT NULL,
        allowed_account TEXT,
        platform TEXT,
        expires_at TEXT NOT NULL,
        stripe_subscription_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS validation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT NOT NULL,
        account_number TEXT,
        platform TEXT,
        product_id TEXT,
        ip_address TEXT,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS signal_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        entry TEXT,
        sl TEXT,
        tp1 TEXT,
        tp2 TEXT,
        tp3 TEXT,
        confidence TEXT,
        reason TEXT,
        sent_to_telegram INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


init_db()

# =========================
# HELPERS
# =========================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def generate_license_key() -> str:
    parts = [
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
    ]
    return "BT-" + "-".join(parts)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ensure_customer(email: str, stripe_customer_id: Optional[str]) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM customers WHERE email = ?", (email,))
    row = cur.fetchone()
    now = iso(utc_now())

    if row:
        if stripe_customer_id:
            cur.execute(
                "UPDATE customers SET stripe_customer_id = ? WHERE email = ?",
                (stripe_customer_id, email),
            )
    else:
        cur.execute(
            "INSERT INTO customers (email, stripe_customer_id, created_at) VALUES (?, ?, ?)",
            (email, stripe_customer_id, now),
        )

    conn.commit()
    conn.close()


def create_or_update_license(
    email: str,
    product_id: str,
    expires_at: datetime,
    subscription_id: Optional[str],
    status: str = "active",
    platform: str = "BOTH",
) -> str:
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT license_key FROM licenses WHERE email = ? AND product_id = ?",
        (email, product_id),
    )
    existing = cur.fetchone()
    now = iso(utc_now())
    expiry = iso(expires_at)

    if existing:
        license_key = existing["license_key"]
        cur.execute("""
            UPDATE licenses
            SET status = ?, expires_at = ?, stripe_subscription_id = ?, platform = ?, updated_at = ?
            WHERE license_key = ?
        """, (status, expiry, subscription_id, platform, now, license_key))
    else:
        license_key = generate_license_key()
        cur.execute("""
            INSERT INTO licenses (
                email, license_key, product_id, status, allowed_account, platform,
                expires_at, stripe_subscription_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email, license_key, product_id, status, None, platform,
            expiry, subscription_id, now, now
        ))

    conn.commit()
    conn.close()
    return license_key


def update_license_status_by_subscription(
    subscription_id: str,
    status: str,
    expires_at: Optional[datetime] = None,
):
    conn = db()
    cur = conn.cursor()
    now = iso(utc_now())

    if expires_at is None:
        cur.execute("""
            UPDATE licenses
            SET status = ?, updated_at = ?
            WHERE stripe_subscription_id = ?
        """, (status, now, subscription_id))
    else:
        cur.execute("""
            UPDATE licenses
            SET status = ?, expires_at = ?, updated_at = ?
            WHERE stripe_subscription_id = ?
        """, (status, iso(expires_at), now, subscription_id))

    conn.commit()
    conn.close()


def get_license(license_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,))
    row = cur.fetchone()
    conn.close()
    return row


def log_validation(
    license_key: str,
    account_number: str,
    platform: str,
    product_id: str,
    ip: str,
    result: str,
):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO validation_log (
            license_key, account_number, platform, product_id, ip_address, result, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (license_key, account_number, platform, product_id, ip, result, iso(utc_now())))
    conn.commit()
    conn.close()


def log_signal(signal, sent_to_telegram: bool):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO signal_log (
            symbol, direction, entry, sl, tp1, tp2, tp3,
            confidence, reason, sent_to_telegram, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal.symbol,
        signal.direction,
        signal.entry,
        signal.sl,
        signal.tp1,
        signal.tp2,
        signal.tp3,
        signal.confidence,
        signal.reason,
        1 if sent_to_telegram else 0,
        iso(utc_now()),
    ))
    conn.commit()
    conn.close()


# =========================
# MODELS
# =========================
class Signal(BaseModel):
    symbol: str
    direction: str
    entry: str
    sl: str
    tp1: str
    tp2: str
    tp3: str
    confidence: str
    reason: str


# =========================
# HEALTH
# =========================
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "OK"


@app.get("/")
def home():
    return {
        "status": "BusyTrader backend running",
        "endpoints": [
            "/health",
            "/validate",
            "/bind-account",
            "/stripe-webhook",
            "/send-signal",
        ],
    }


# =========================
# TELEGRAM SIGNAL WEBHOOK
# =========================
@app.post("/send-signal")
def send_signal(signal: Signal):
    if TELEGRAM_BOT_TOKEN == "replace_me" or TELEGRAM_CHAT_ID == "replace_me":
        log_signal(signal, False)
        raise HTTPException(
            status_code=500,
            detail="Telegram bot token or chat ID not configured",
        )

    message = f"""
🚨 BUSYTRADER AI SIGNAL 🚨

Market: {signal.symbol}
Direction: {signal.direction}

Entry: {signal.entry}
SL: {signal.sl}

TP1: {signal.tp1}
TP2: {signal.tp2}
TP3: {signal.tp3}

Confidence: {signal.confidence}%

Reason:
{signal.reason}

⚠️ Risk warning: Signals are for education and testing. Manage your risk.
"""

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        log_signal(signal, False)
        raise HTTPException(status_code=500, detail=f"Telegram request failed: {str(e)}")

    if response.status_code != 200:
        log_signal(signal, False)
        raise HTTPException(status_code=500, detail=response.text)

    log_signal(signal, True)

    return {
        "sent": True,
        "symbol": signal.symbol,
        "direction": signal.direction,
    }


# =========================
# MANUAL REGISTER / REBIND
# =========================
@app.post("/bind-account", response_class=PlainTextResponse)
async def bind_account(request: Request):
    data = await request.json()
    license_key = str(data.get("license_key", "")).strip()
    account_number = str(data.get("account_number", "")).strip()

    if not license_key or not account_number:
        raise HTTPException(status_code=400, detail="license_key and account_number required")

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE licenses SET allowed_account = ?, updated_at = ? WHERE license_key = ?",
        (account_number, iso(utc_now()), license_key),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed == 0:
        return PlainTextResponse("ERROR|NOT_FOUND", status_code=404)

    return "OK|BOUND"


# =========================
# EA VALIDATION ENDPOINT
# =========================
@app.post("/validate", response_class=PlainTextResponse)
async def validate(request: Request):
    ip = request.client.host if request.client else ""
    data = await request.json()

    license_key = str(data.get("license_key", "")).strip()
    account_number = str(data.get("account_number", "")).strip()
    platform = str(data.get("platform", "")).strip().upper()
    product_id = str(data.get("product_id", DEFAULT_PRODUCT_ID)).strip()

    if not license_key or not account_number or not platform:
        log_validation(license_key, account_number, platform, product_id, ip, "ERROR_MISSING_FIELDS")
        return PlainTextResponse("ERROR|MISSING_FIELDS", status_code=400)

    row = get_license(license_key)

    if not row:
        log_validation(license_key, account_number, platform, product_id, ip, "ERROR_NOT_FOUND")
        return "ERROR|NOT_FOUND"

    if row["product_id"] != product_id:
        log_validation(license_key, account_number, platform, product_id, ip, "ERROR_WRONG_PRODUCT")
        return "ERROR|WRONG_PRODUCT"

    if row["platform"] not in ("BOTH", platform):
        log_validation(license_key, account_number, platform, product_id, ip, "ERROR_WRONG_PLATFORM")
        return "ERROR|WRONG_PLATFORM"

    if row["status"] != "active":
        log_validation(license_key, account_number, platform, product_id, ip, f"ERROR_{row['status'].upper()}")
        return f"ERROR|{row['status'].upper()}"

    expiry = parse_iso(row["expires_at"])

    if utc_now() > expiry:
        log_validation(license_key, account_number, platform, product_id, ip, "ERROR_EXPIRED")
        return "ERROR|EXPIRED"

    if not row["allowed_account"]:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE licenses SET allowed_account = ?, updated_at = ? WHERE license_key = ?
        """, (account_number, iso(utc_now()), license_key))
        conn.commit()
        conn.close()
    else:
        if row["allowed_account"] != account_number:
            log_validation(license_key, account_number, platform, product_id, ip, "ERROR_WRONG_ACCOUNT")
            return "ERROR|WRONG_ACCOUNT"

    log_validation(license_key, account_number, platform, product_id, ip, "OK")
    return f"OK|{row['expires_at']}|VALID"


# =========================
# STRIPE WEBHOOK
# =========================
@app.post("/stripe-webhook", response_class=PlainTextResponse)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        data_dict = dict(data)

        customer_details = data_dict.get("customer_details") or {}
        if not isinstance(customer_details, dict):
            customer_details = dict(customer_details)

        customer_email = customer_details.get("email") or data_dict.get("customer_email")
        stripe_customer_id = data_dict.get("customer")
        subscription_id = data_dict.get("subscription")

    if customer_email:
        ensure_customer(customer_email, stripe_customer_id)

        if subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id)
            subscription_dict = dict(subscription)

            current_period_end = datetime.fromtimestamp(
                subscription_dict["current_period_end"],
                tz=timezone.utc,
            )
        else:
            current_period_end = utc_now() + timedelta(days=30)

        license_key = create_or_update_license(
            email=customer_email,
            product_id=DEFAULT_PRODUCT_ID,
            expires_at=current_period_end,
            subscription_id=subscription_id,
            status="active",
            platform="BOTH",
        )

        print(f"Created/updated license for {customer_email}: {license_key}")

        if customer_email and subscription_id:
            ensure_customer(customer_email, stripe_customer_id)

            subscription = stripe.Subscription.retrieve(subscription_id)
            current_period_end = datetime.fromtimestamp(
                subscription["current_period_end"],
                tz=timezone.utc,
            )

            license_key = create_or_update_license(
                email=customer_email,
                product_id=DEFAULT_PRODUCT_ID,
                expires_at=current_period_end,
                subscription_id=subscription_id,
                status="active",
                platform="BOTH",
            )

            print(f"Created/updated license for {customer_email}: {license_key}")

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        subscription_id = data["id"]
        status = data["status"]
        current_period_end = datetime.fromtimestamp(
            data["current_period_end"],
            tz=timezone.utc,
        )

        mapped_status = "active" if status in ("active", "trialing", "past_due") else "cancelled"

        update_license_status_by_subscription(
            subscription_id,
            mapped_status,
            current_period_end,
        )

    elif event_type == "customer.subscription.deleted":
        subscription_id = data["id"]
        update_license_status_by_subscription(subscription_id, "cancelled")

    elif event_type == "invoice.payment_failed":
        subscription_id = data.get("subscription")
        if subscription_id:
            update_license_status_by_subscription(subscription_id, "cancelled")

    return "OK"
