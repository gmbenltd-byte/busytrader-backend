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

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DEFAULT_PRODUCT_ID = os.getenv("DEFAULT_PRODUCT_ID", "BUSY_ALL")
DB_PATH = os.getenv("DB_PATH", "busytrader.db")

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def generate_license_key() -> str:
    return "BT-" + "-".join([
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
        secrets.token_hex(2).upper(),
    ])


def stripe_obj_to_dict(obj):
    if hasattr(obj, "_to_dict_recursive"):
        return obj._to_dict_recursive()
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return dict(obj)


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram skipped: not configured")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        print("Telegram alert response:", response.status_code, response.text)
    except Exception as e:
        print("Telegram error:", str(e))


def ensure_customer(email: str, stripe_customer_id: Optional[str]) -> None:
    conn = db()
    cur = conn.cursor()
    now = iso(utc_now())

    cur.execute("SELECT id FROM customers WHERE email = ?", (email,))
    row = cur.fetchone()

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


def sync_license_to_sheets(
    email: str,
    license_key: str,
    product_id: str,
    status: str,
    platform: str,
    expiry: str,
    subscription_id: Optional[str],
):
    url = os.getenv("SHEETS_WEBHOOK_URL", "")

    if not url:
        print("Sheets sync skipped: SHEETS_WEBHOOK_URL not set")
        return

    payload = {
        "email": email,
        "license_key": license_key,
        "product_id": product_id,
        "status": status,
        "platform": platform,
        "expiry": expiry,
        "subscription_id": subscription_id or "",
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        print("Sheets sync response:", response.status_code, response.text)
    except Exception as e:
        print("Sheets sync failed:", str(e))


def send_license_email(email: str, license_key: str, product_id: str, expiry: str):
    resend_api_key = os.getenv("RESEND_API_KEY", "")
    email_from = os.getenv("EMAIL_FROM", "BusyTrader <support@busytraderapp.com>")

    if not resend_api_key:
        print("Email skipped: RESEND_API_KEY not set")
        return

    vip_link = "https://t.me/+FNI5G2IXQrBmODk0"
    scanner_link = "https://buy.stripe.com/6oU6oz9jAbQzal1cwwds407"
    setup_video_link = "https://youtu.be/8lp_DvkzM7E"

    DOWNLOAD_LINKS = {
        # PDFs
        "BUSY_NAS100_PDF": "https://drive.google.com/uc?export=download&id=1FEoSPmVdiA6v-TfBqhEZVdNbobecMExZ",
        "BUSY_RISK_PDF": "PASTE_RISK_PDF_LINK_HERE",
        "BUSY_SCALPING_PDF": "PASTE_SCALPING_PDF_LINK_HERE",

        # EAs / Apps
        "BUSY_NAS100_EA": "https://drive.google.com/drive/folders/1r7fY00J7Q2wUKE4TFKdG8n1QWmKncjhw?usp=drive_link",
        "BUSY_AI_AUTOTRADER": "PASTE_AUTOTRADER_ZIP_LINK_HERE",
        "BUSY_AI_SCANNER": "PASTE_SCANNER_LINK_HERE",
        "BUSY_AI_COPILOT": "PASTE_COPILOT_LINK_HERE",

        # VIP
        "BUSY_VIP": vip_link,
    }

    download_link = DOWNLOAD_LINKS.get(product_id, "https://busytraderapp.com")

    html = f"""
    <div style="background:#0b0b0b;padding:30px;color:white;font-family:Arial;">
      <h2 style="color:gold;">BusyTrader Licence Activated</h2>

      <p>Your access is now active.</p>

      <p><strong>Product:</strong> {product_id}</p>

      <p><strong>Licence Key:</strong></p>
      <div style="background:#111;padding:15px;border:1px solid gold;border-radius:8px;font-size:18px;">
        {license_key}
      </div>

      <p><strong>Expiry:</strong> {expiry}</p>

      <br>

      <a href="{download_link}"
         style="background:gold;color:#111;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:bold;display:inline-block;margin:6px 0;">
         Download Your Product
      </a>

      <br>

      <a href="{vip_link}"
         style="background:#229ED9;color:white;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:bold;display:inline-block;margin:6px 0;">
         Join Telegram VIP
      </a>

      <br>

      <a href="{scanner_link}"
         style="background:#111;color:gold;padding:12px 18px;border:1px solid gold;border-radius:8px;text-decoration:none;font-weight:bold;display:inline-block;margin:6px 0;">
         Upgrade to AI Scanner
      </a>

      <br>

      <a href="{setup_video_link}"
         style="background:#222;color:white;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:bold;display:inline-block;margin:6px 0;">
         Watch Setup Video
      </a>

      <br><br>

      <a href="https://t.me/busytraderhq" style="color:#229ED9;">
         Join Telegram for updates
      </a>

      <hr style="border-color:#222;">

      <p style="font-size:12px;color:#888;">
        Trading involves risk. This is not financial advice.
      </p>
    </div>
    """

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": email_from,
                "to": [email],
                "subject": "Your BusyTrader Licence Key",
                "html": html,
            },
            timeout=10,
        )
        print("Email send response:", response.status_code, response.text)
    except Exception as e:
        print("Email send failed:", str(e))


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


class ValidateRequest(BaseModel):
    license_key: str
    account_number: str
    platform: str
    product_id: Optional[str] = DEFAULT_PRODUCT_ID


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
            "/admin/update-license",
        ],
    }


@app.post("/send-signal")
def send_signal(signal: Signal):
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


@app.post("/validate", response_class=PlainTextResponse)
async def validate(req: ValidateRequest, request: Request):
    ip = request.client.host if request.client else ""

    license_key = req.license_key.strip()
    account_number = req.account_number.strip()
    platform = req.platform.strip().upper()
    product_id = (req.product_id or DEFAULT_PRODUCT_ID).strip()

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
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
):
    payload = await request.body()

    print("WEBHOOK HIT")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        print("STRIPE WEBHOOK ERROR:", str(e))
        return PlainTextResponse("ERROR|INVALID_WEBHOOK", status_code=400)

    event_type = event.type
    data = event.data.object
    data_dict = stripe_obj_to_dict(data)

    print("EVENT TYPE:", event_type)

    if event_type == "checkout.session.completed":
        customer_details = data_dict.get("customer_details") or {}

        customer_email = customer_details.get("email") or data_dict.get("customer_email")
        stripe_customer_id = data_dict.get("customer")
        subscription_id = data_dict.get("subscription")

        metadata = data_dict.get("metadata") or {}
        product_id = metadata.get("product_id", DEFAULT_PRODUCT_ID)

        print("CUSTOMER EMAIL:", customer_email)
        print("SUBSCRIPTION ID:", subscription_id)
        print("PRODUCT ID:", product_id)

        if not customer_email:
            print("NO CUSTOMER EMAIL FOUND")
            return "OK"

        ensure_customer(customer_email, stripe_customer_id)

        if subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id)
            subscription_dict = stripe_obj_to_dict(subscription)

            current_period_end = datetime.fromtimestamp(
                subscription_dict["current_period_end"],
                tz=timezone.utc,
            )
        else:
            current_period_end = utc_now() + timedelta(days=30)

        license_key = create_or_update_license(
            email=customer_email,
            product_id=product_id,
            expires_at=current_period_end,
            subscription_id=subscription_id,
            status="active",
            platform="BOTH",
        )

        print("LICENSE CREATED:", license_key)

        send_telegram_alert(f"""
💰 <b>NEW PURCHASE</b>

📦 <b>Product:</b> {product_id}
👤 <b>Email:</b> {customer_email}
🔑 <b>License:</b> {license_key}
⏳ <b>Expiry:</b> {iso(current_period_end)}
""")

        if product_id == "BUSY_VIP":
            vip_link = "https://t.me/+FNI5G2IXQrBmODk0"

            send_telegram_alert(f"""
🔥 <b>NEW VIP MEMBER</b>

👤 <b>Email:</b> {customer_email}
🔑 <b>License:</b> {license_key}
📲 <b>VIP invite:</b> {vip_link}
""")

        sync_license_to_sheets(
            customer_email,
            license_key,
            product_id,
            "active",
            "BOTH",
            iso(current_period_end),
            subscription_id,
        )

        send_license_email(
            customer_email,
            license_key,
            product_id,
            iso(current_period_end),
        )

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        subscription_id = data_dict.get("id")
        status = data_dict.get("status")
        period_end = data_dict.get("current_period_end")

        print("SUBSCRIPTION UPDATE:", subscription_id, status)

        if subscription_id and status and period_end:
            current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
            mapped_status = "active" if status in ("active", "trialing", "past_due") else "cancelled"

            update_license_status_by_subscription(
                subscription_id,
                mapped_status,
                current_period_end,
            )

    elif event_type == "customer.subscription.deleted":
        subscription_id = data_dict.get("id")

        print("SUBSCRIPTION DELETED:", subscription_id)

        send_telegram_alert(f"""
❌ <b>SUBSCRIPTION CANCELLED</b>

🧾 <b>Subscription:</b> {subscription_id}
""")

        if subscription_id:
            update_license_status_by_subscription(subscription_id, "cancelled")

    elif event_type == "invoice.payment_failed":
        subscription_id = data_dict.get("subscription")

        print("PAYMENT FAILED:", subscription_id)

        send_telegram_alert(f"""
⚠️ <b>PAYMENT FAILED</b>

🧾 <b>Subscription:</b> {subscription_id}
""")

        if subscription_id:
            update_license_status_by_subscription(subscription_id, "cancelled")

    return "OK"


@app.post("/admin/update-license", response_class=PlainTextResponse)
async def admin_update_license(request: Request):
    data = await request.json()

    admin_key = str(data.get("admin_key", "")).strip()
    license_key = str(data.get("license_key", "")).strip()
    status = str(data.get("status", "")).strip().lower()

    real_admin_key = os.getenv("ADMIN_KEY", "replace_me")

    if admin_key != real_admin_key:
        return PlainTextResponse("ERROR|UNAUTHORIZED", status_code=401)

    if status not in ("active", "cancelled", "blocked", "expired"):
        return PlainTextResponse("ERROR|INVALID_STATUS", status_code=400)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE licenses
        SET status = ?, updated_at = ?
        WHERE license_key = ?
    """, (status, iso(utc_now()), license_key))

    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed == 0:
        return PlainTextResponse("ERROR|NOT_FOUND", status_code=404)

    return f"OK|{license_key}|{status.upper()}"
