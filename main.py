from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse
import os
import sqlite3
import stripe
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

app = FastAPI()

stripe.api_key = "sk_test_replace_me"
STRIPE_WEBHOOK_SECRET = "whsec_5d8ebc0dae85f66dac0250422240d35d74266ad41aabc69c1efd21eaa3e7bd4a"

DB_PATH = os.path.join(os.path.dirname(__file__), "busytrader.db")
DEFAULT_PRODUCT_ID = "BUSY_ALL"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        license_key TEXT NOT NULL UNIQUE,
        product_id TEXT NOT NULL,
        status TEXT NOT NULL,
        allowed_account TEXT,
        platform TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        stripe_subscription_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("PRAGMA table_info(licenses)")
    existing_columns = [row[1] for row in cur.fetchall()]

    columns_to_add = {
        "allowed_account": "TEXT",
        "platform": "TEXT NOT NULL DEFAULT 'BOTH'",
        "stripe_subscription_id": "TEXT",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }

    for col_name, col_type in columns_to_add.items():
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE licenses ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()


init_db()


def generate_license() -> str:
    return "BT-" + secrets.token_hex(6).upper()

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    print("WEBHOOK HIT")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload,
            stripe_signature,
            STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("Webhook error:", e)
        raise HTTPException(status_code=400)

    event_type = event["type"]
    data = event["data"]["object"]

    print("EVENT:", event_type)

    if event_type == "checkout.session.completed":
        email = "test@example.com"
        if "customer_email" in data and data["customer_email"]:
            email = data["customer_email"]

        license_key = generate_license()
        expires = datetime.now(timezone.utc) + timedelta(days=30)

        conn = db()
        cur = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()

        cur.execute("""
        INSERT INTO licenses (
            email,
            license_key,
            product_id,
            status,
            allowed_account,
            platform,
            expires_at,
            stripe_subscription_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email,
            license_key,
            DEFAULT_PRODUCT_ID,
            "active",
            None,
            "BOTH",
            expires.isoformat(),
            None,
            now,
            now,
        ))

        conn.commit()
        conn.close()

        print("LICENSE CREATED:", license_key)

    elif event_type == "payment_intent.succeeded":
        print("Payment OK")

    else:
        print("Unhandled:", event_type)

    return "OK"

@app.get("/validate", response_class=PlainTextResponse)
async def validate(
    key: str = "",
    account: str = "",
    broker: str = "",
    product_id: str = DEFAULT_PRODUCT_ID
):
    license_key = str(key).strip()
    account_number = str(account).strip()
    platform = str(broker).strip().upper()
    product_id = str(product_id).strip()

    # TEMP TEST LICENSE
    if license_key == "TEST123":
        return "VALID"

    if not license_key or not account_number:
        return PlainTextResponse("ERROR|MISSING_FIELDS", status_code=400)

    return PlainTextResponse("ERROR|INVALID_LICENSE", status_code=403)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT email, license_key, product_id, status, expires_at, allowed_account, platform "
        "FROM licenses WHERE license_key = ?",
        (license_key,)
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        return "ERROR|NOT_FOUND"

    if row["product_id"] != product_id:
        conn.close()
        return "ERROR|WRONG_PRODUCT"

    if row["status"] != "active":
        conn.close()
        return f"ERROR|{row['status'].upper()}"

    expiry = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expiry:
        conn.close()
        return "ERROR|EXPIRED"

    if row["platform"] not in ("BOTH", platform):
        conn.close()
        return "ERROR|WRONG_PLATFORM"

    if not row["allowed_account"]:
        cur.execute(
            "UPDATE licenses SET allowed_account = ?, updated_at = ? WHERE license_key = ?",
            (account_number, datetime.now(timezone.utc).isoformat(), license_key)
        )
        conn.commit()
    elif row["allowed_account"] != account_number:
        conn.close()
        return "ERROR|WRONG_ACCOUNT"

    conn.close()
    return f"OK|{row['expires_at']}|VALID"
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "OK"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
