from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse
import sqlite3
import stripe
import secrets
from datetime import datetime, timezone
from typing import Optional

app = FastAPI()

STRIPE_SECRET_KEY = "sk_live_replace_me"
STRIPE_WEBHOOK_SECRET = "whsec_replace_me"
DEFAULT_PRODUCT_ID = "BUSY_ALL"
DB_PATH = "busytrader.db"

stripe.api_key = STRIPE_SECRET_KEY

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

    conn.commit()
    conn.close()

init_db()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.isoformat()

def generate_license_key() -> str:
    parts = [secrets.token_hex(2).upper(), secrets.token_hex(2).upper(), secrets.token_hex(2).upper(), secrets.token_hex(2).upper()]
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
                (stripe_customer_id, email)
            )
    else:
        cur.execute(
            "INSERT INTO customers (email, stripe_customer_id, created_at) VALUES (?, ?, ?)",
            (email, stripe_customer_id, now)
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
        (email, product_id)
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

def update_license_status_by_subscription(subscription_id: str, status: str, expires_at: Optional[datetime] = None):
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

def log_validation(license_key: str, account_number: str, platform: str, product_id: str, ip: str, result: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO validation_log (license_key, account_number, platform, product_id, ip_address, result, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (license_key, account_number, platform, product_id, ip, result, iso(utc_now())))
    conn.commit()
    conn.close()

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "OK"

@app.post("/bind-account", response_class=PlainTextResponse)
async def bind_account(request: Request):
    data = await request.json()
    license_key = str(data.get("license_key", "")).strip()
    account_number = str(data.get("account_number", "")).strip()

    if not license_key or not account_number:
        raise HTTPException(status_code=400, detail="license_key and account_number required")

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE licenses SET allowed_account = ?, updated_at = ? WHERE license_key = ?",
                (account_number, iso(utc_now()), license_key))
    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed == 0:
        return PlainTextResponse("ERROR|NOT_FOUND", status_code=404)

    return "OK|BOUND"

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

@app.post("/stripe-webhook", response_class=PlainTextResponse)
async def stripe_webhook(request: Request, stripe_signature: str = Header(None, alias="Stripe-Signature")):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        customer_email = data.get("customer_details", {}).get("email") or data.get("customer_email")
        stripe_customer_id = data.get("customer")
        subscription_id = data.get("subscription")

        if customer_email and subscription_id:
            ensure_customer(customer_email, stripe_customer_id)

            subscription = stripe.Subscription.retrieve(subscription_id)
            current_period_end = datetime.fromtimestamp(subscription["current_period_end"], tz=timezone.utc)

            license_key = create_or_update_license(
                email=customer_email,
                product_id=DEFAULT_PRODUCT_ID,
                expires_at=current_period_end,
                subscription_id=subscription_id,
                status="active",
                platform="BOTH"
            )

            print(f"Created/updated license for {customer_email}: {license_key}")

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        subscription_id = data["id"]
        status = data["status"]
        current_period_end = datetime.fromtimestamp(data["current_period_end"], tz=timezone.utc)

        mapped_status = "active" if status in ("active", "trialing", "past_due") else "cancelled"
        update_license_status_by_subscription(subscription_id, mapped_status, current_period_end)

    elif event_type == "customer.subscription.deleted":
        subscription_id = data["id"]
        update_license_status_by_subscription(subscription_id, "cancelled")

elif event_type == "invoice.payment_succeeded":
    subscription_id = data.get("subscription")
    if subscription_id:
        subscription = stripe.Subscription.retrieve(subscription_id)
        current_period_end = datetime.fromtimestamp(subscription["current_period_end"], tz=timezone.utc)
        update_license_status_by_subscription(subscription_id, "active", current_period_end)
    elif event_type == "invoice.payment_failed":
        subscription_id = data.get("subscription")
        if subscription_id:
            update_license_status_by_subscription(subscription_id, "cancelled")

    return "OK"