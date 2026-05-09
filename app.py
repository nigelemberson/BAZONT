import os
import sqlite3
import threading
import time
import secrets
import html
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for, abort, send_from_directory, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import requests
except ImportError:  # Render/local install guard; requirements.txt includes requests.
    requests = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('BAZONT_DATA_DIR', Path.home() / 'BAZONT_data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get('BAZONT_DB_PATH', DATA_DIR / 'bazont.db'))
VERSION_FILE = BASE_DIR / 'version.txt'
DEFAULT_VERSION = 'Bazont16E.zip'
def get_version():
    if VERSION_FILE.exists():
        value = VERSION_FILE.read_text(encoding='utf-8').strip()
        if value:
            return value
    return DEFAULT_VERSION

PH_TZ = timezone(timedelta(hours=8))
MAX_ITEM_PRICE = 10000.0
MAX_WEIGHT = 15.0
MAX_DIM = 50.0
ALLOWED_DELIVERED_STATUSES = {'DELIVERED'}
TRACKING_DEADLINE_DAYS = 3
RULE_LOOP_SECONDS = 20
TRACKING_CHECK_SECONDS = 60  # 1 minute (test mode)
AFTERSHIP_API_KEY = os.environ.get('AFTERSHIP_API_KEY', '').strip()
AFTERSHIP_BASE_URL = 'https://api.aftership.com/v4/trackings'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
RESEND_FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', 'Bazont <noreply@bazont.com>').strip()
RESEND_API_URL = 'https://api.resend.com/emails'
COURIER_SLUGS = {
    'lbc': 'lbc-express',
    'lbc express': 'lbc-express',
    'lbc-express': 'lbc-express',
    'j&t': 'jtexpress-ph',
    'j&t express': 'jtexpress-ph',
    'jnt': 'jtexpress-ph',
    'jtexpress': 'jtexpress-ph',
    'jtexpress-ph': 'jtexpress-ph',
    'flash': 'flash-express',
    'flash express': 'flash-express',
    'flash-express': 'flash-express',
    'ninjavan': 'ninjavan-ph',
    'ninja van': 'ninjavan-ph',
    'ninjavan-ph': 'ninjavan-ph',
}

FEE_RATE = 0.03
MIN_PLATFORM_FEE = 100.0
BUYER_FEE_RATE = FEE_RATE / 2
SELLER_FEE_RATE = FEE_RATE / 2
PAYMENT_METHODS = ['GCash', 'Maya', 'Bank transfer']
PAYOUT_METHODS = ['GCash', 'Bank transfer']


def money(value):
    return round(float(value), 2)


def normalise_courier_slug(courier_name):
    value = (courier_name or '').strip().lower()
    if not value:
        return ''
    return COURIER_SLUGS.get(value, value.replace(' ', '-'))


def tracking_next_check_iso():
    return (now_ph() + timedelta(seconds=TRACKING_CHECK_SECONDS)).isoformat()


def aftership_headers():
    return {
        'aftership-api-key': AFTERSHIP_API_KEY,
        'Content-Type': 'application/json',
    }


def aftership_enabled():
    return bool(AFTERSHIP_API_KEY) and requests is not None


def aftership_create_tracking(tracking_number, courier_slug):
    if not aftership_enabled():
        return False, 'AfterShip API key is not configured.', None
    payload = {'tracking': {'tracking_number': tracking_number, 'slug': courier_slug}}
    try:
        response = requests.post(AFTERSHIP_BASE_URL, json=payload, headers=aftership_headers(), timeout=20)
        data = response.json() if response.content else {}
    except Exception as exc:
        return False, f'AfterShip create request failed: {exc}', None
    if response.status_code in (200, 201):
        tracking = data.get('data', {}).get('tracking', {})
        return True, 'Tracking accepted by AfterShip.', tracking
    message = data.get('meta', {}).get('message') or data.get('message') or f'AfterShip HTTP {response.status_code}'
    return False, str(message), None


def aftership_get_tracking_status(tracking_number, courier_slug):
    if not aftership_enabled():
        return False, 'AfterShip API key is not configured.', None
    url = f'{AFTERSHIP_BASE_URL}/{courier_slug}/{tracking_number}'
    try:
        response = requests.get(url, headers=aftership_headers(), timeout=20)
        data = response.json() if response.content else {}
    except Exception as exc:
        return False, f'AfterShip status request failed: {exc}', None
    if response.status_code == 200:
        tracking = data.get('data', {}).get('tracking', {})
        return True, 'Tracking status read from AfterShip.', tracking
    message = data.get('meta', {}).get('message') or data.get('message') or f'AfterShip HTTP {response.status_code}'
    return False, str(message), None


def tx_financials(tx):
    total_amount = money(tx['total_amount'])
    item_price = money(tx['item_price'])
    shipping_price = money(tx['shipping_price'])
    total_fee = money(max(total_amount * FEE_RATE, MIN_PLATFORM_FEE))
    buyer_fee = money(total_fee / 2)
    seller_fee = money(total_fee / 2)
    buyer_pays = money(total_amount + buyer_fee)
    seller_receives = money(total_amount - seller_fee)
    return {
        'item_price': item_price,
        'shipping_price': shipping_price,
        'transaction_total': total_amount,
        'buyer_fee': buyer_fee,
        'seller_fee': seller_fee,
        'total_fee': total_fee,
        'buyer_pays': buyer_pays,
        'seller_receives': seller_receives,
        'platform_holds': total_amount,
    }


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.config['EMAIL_OUTBOX_DIR'] = BASE_DIR / 'outbox'
app.config['EMAIL_OUTBOX_DIR'].mkdir(exist_ok=True)

LOGIN_REQUIRED = True
TEST_USER_EMAIL = 'test@bazont.local'
TEST_USER_ROLE = 'buyer'


def bootstrap_runtime():
    # Prepare persistent directories and database for both local runs and WSGI imports.
    app.config['EMAIL_OUTBOX_DIR'].mkdir(exist_ok=True)
    init_db()


def now_ph():
    return datetime.now(PH_TZ)


def now_iso():
    return now_ph().isoformat()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def get_db():
    if 'db' not in g:
        g.db = db_connect()
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    conn = db_connect()
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('buyer', 'seller')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            buyer_user_id INTEGER NOT NULL,
            seller_user_id INTEGER,
            seller_email TEXT NOT NULL,
            item_description TEXT NOT NULL,
            item_price REAL NOT NULL,
            shipping_price REAL NOT NULL,
            total_amount REAL NOT NULL,
            weight_kg REAL NOT NULL,
            length_cm REAL NOT NULL,
            width_cm REAL NOT NULL,
            height_cm REAL NOT NULL,
            invite_token TEXT NOT NULL UNIQUE,
            invite_sent_at TEXT NOT NULL,
            payment_received_at TEXT,
            tracking_number TEXT,
            courier_name TEXT,
            tracking_submitted_at TEXT,
            courier_slug TEXT,
            pickup_date TEXT,
            pickup_time TEXT,
            tracking_api_status TEXT,
            tracking_api_message TEXT,
            aftership_tracking_id TEXT,
            tracking_last_checked_at TEXT,
            tracking_next_check_at TEXT,
            status TEXT NOT NULL,
            hold_status TEXT NOT NULL,
            released_at TEXT,
            refunded_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(buyer_user_id) REFERENCES users(id),
            FOREIGN KEY(seller_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS courier_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            tracking_number TEXT NOT NULL,
            status TEXT NOT NULL,
            event_time TEXT NOT NULL,
            source TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER,
            actor_type TEXT NOT NULL,
            actor_ref TEXT NOT NULL,
            action TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        );
        '''
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    migrations = {
        'courier_slug': 'TEXT',
        'pickup_date': 'TEXT',
        'pickup_time': 'TEXT',
        'tracking_api_status': 'TEXT',
        'tracking_api_message': 'TEXT',
        'aftership_tracking_id': 'TEXT',
        'tracking_last_checked_at': 'TEXT',
        'tracking_next_check_at': 'TEXT',
    }
    for column, col_type in migrations.items():
        if column not in cols:
            conn.execute(f'ALTER TABLE transactions ADD COLUMN {column} {col_type}')
    conn.commit()
    conn.close()


VALID_TRANSITIONS = {
    'INVITED': {'PAID', 'REFUNDED', 'CANCELLED'},
    'PAID': {'TRACKING_SUBMITTED', 'REFUNDED', 'RELEASED'},
    'TRACKING_SUBMITTED': {'RELEASED', 'REFUNDED'},
    'RELEASED': set(),
    'REFUNDED': set(),
    'CANCELLED': set(),
}


def log_audit(conn, transaction_id, actor_type, actor_ref, action, from_state=None, to_state=None, details=''):
    conn.execute(
        '''INSERT INTO audit_log (transaction_id, actor_type, actor_ref, action, from_state, to_state, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (transaction_id, actor_type, actor_ref, action, from_state, to_state, details, now_iso())
    )



def set_status(conn, tx, new_status, actor_type, actor_ref, action, details=''):
    current = tx['status']
    if current == new_status:
        return
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f'Invalid state transition: {current} -> {new_status}')

    hold_status = tx['hold_status']
    released_at = tx['released_at']
    refunded_at = tx['refunded_at']
    if new_status == 'PAID':
        hold_status = 'FUNDED'
    elif new_status == 'TRACKING_SUBMITTED':
        hold_status = 'FUNDED'
    elif new_status == 'RELEASED':
        hold_status = 'RELEASED'
        released_at = now_iso()
    elif new_status == 'REFUNDED':
        hold_status = 'REFUNDED'
    elif new_status == 'CANCELLED':
        hold_status = 'CANCELLED'

    conn.execute(
        '''UPDATE transactions
           SET status = ?, hold_status = ?, released_at = COALESCE(?, released_at), refunded_at = COALESCE(?, refunded_at), updated_at = ?
           WHERE id = ?''',
        (new_status, hold_status, released_at, refunded_at, now_iso(), tx['id'])
    )
    log_audit(conn, tx['id'], actor_type, actor_ref, action, current, new_status, details)



def refresh_tx(conn, tx_id):
    return conn.execute('SELECT * FROM transactions WHERE id = ?', (tx_id,)).fetchone()



def current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    return conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


@app.context_processor
def inject_globals():
    return {
        'current_user': current_user(),
        'VERSION': get_version(),
        'MAX_ITEM_PRICE': MAX_ITEM_PRICE,
        'MAX_WEIGHT': MAX_WEIGHT,
        'MAX_DIM': MAX_DIM,
        'TRACKING_DEADLINE_DAYS': TRACKING_DEADLINE_DAYS,
        'now_ph': now_ph,
        'FEE_RATE': FEE_RATE,
        'BUYER_FEE_RATE': BUYER_FEE_RATE,
        'SELLER_FEE_RATE': SELLER_FEE_RATE,
        'PAYMENT_METHODS': PAYMENT_METHODS,
        'PAYOUT_METHODS': PAYOUT_METHODS,
        'tx_financials': tx_financials,
    }


bootstrap_runtime()


@app.before_request
def run_rules_on_request():
    process_rules()



def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not LOGIN_REQUIRED:
                ensure_test_login()
                return fn(*args, **kwargs)
            user = current_user()
            if user is None:
                flash('Please log in first.', 'error')
                return redirect(url_for('login', next=request.path))
            if role and user['role'] != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco



def seller_invite_link(tx):
    return url_for('seller_join', token=tx['invite_token'], _external=True)


def build_seller_invite_subject(tx):
    return f"Join BAZONT transaction {tx['public_id']}"


def build_seller_invite_plain_text(tx):
    invite_link = seller_invite_link(tx)
    return f"""Hello,

You are invited to join a BAZONT transaction as the seller.

Transaction ID: {tx['public_id']}
Description of the article: {tx['item_description']}
Total amount held by Bazont: PHP {tx['total_amount']:.2f}

Accept Invitation:
{invite_link}

Seller rule:
Tracking must be uploaded within {TRACKING_DEADLINE_DAYS} days after buyer payment. If tracking is not uploaded in time, the transaction is cancelled and the buyer is refunded.

Payment release rule:
Bazont releases payment only after the courier confirms DELIVERED.

Thank you,
The BAZONT Team
"""


def build_seller_invite_html(tx):
    invite_link = seller_invite_link(tx)
    public_id = html.escape(str(tx['public_id']))
    item_description = html.escape(str(tx['item_description']))
    total_amount = f"PHP {tx['total_amount']:.2f}"
    safe_invite_link = html.escape(invite_link, quote=True)
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,Helvetica,sans-serif;color:#172033;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7fb;padding:28px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:94%;background:#ffffff;border-radius:18px;overflow:hidden;border:1px solid #dbe4f0;box-shadow:0 10px 26px rgba(15,23,42,0.10);">
            <tr>
              <td style="background:#0f172a;padding:22px 28px;color:#ffffff;">
                <div style="font-size:22px;font-weight:900;letter-spacing:0.8px;">BAZONT</div>
                <div style="font-size:13px;color:#bfdbfe;margin-top:4px;font-weight:700;">Safe Transactions</div>
              </td>
            </tr>
            <tr>
              <td style="padding:28px;">
                <h1 style="margin:0 0 12px;font-size:24px;line-height:1.25;color:#0f172a;">You have a Bazont transaction invitation</h1>
                <p style="margin:0 0 20px;font-size:16px;line-height:1.55;color:#334155;">Hello, you are invited to join a BAZONT transaction as the seller.</p>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dbe4f0;border-radius:14px;background:#f8fafc;margin:0 0 22px;">
                  <tr><td style="padding:18px 20px;">
                    <div style="font-size:13px;color:#64748b;font-weight:800;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:10px;">Transaction summary</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                      <tr><td style="padding:7px 0;color:#64748b;font-size:14px;font-weight:700;">Transaction ID</td><td style="padding:7px 0;color:#0f172a;font-size:14px;font-weight:900;text-align:right;">{public_id}</td></tr>
                      <tr><td style="padding:7px 0;color:#64748b;font-size:14px;font-weight:700;">Description of the article</td><td style="padding:7px 0;color:#0f172a;font-size:14px;font-weight:900;text-align:right;">{item_description}</td></tr>
                      <tr><td style="padding:7px 0;color:#64748b;font-size:14px;font-weight:700;">Total amount held by Bazont</td><td style="padding:7px 0;color:#0f172a;font-size:14px;font-weight:900;text-align:right;">{total_amount}</td></tr>
                    </table>
                  </td></tr>
                </table>

                <div style="text-align:center;margin:24px 0 22px;">
                  <a href="{safe_invite_link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-size:16px;font-weight:900;padding:14px 28px;border-radius:999px;">Accept Invitation</a>
                </div>

                <p style="margin:0 0 8px;font-size:14px;line-height:1.5;color:#475569;">If the button above does not work, copy and paste this link into your browser:</p>
                <p style="margin:0 0 20px;font-size:13px;line-height:1.45;color:#2563eb;word-break:break-all;">{safe_invite_link}</p>

                <div style="border-top:1px solid #e2e8f0;padding-top:16px;margin-top:18px;font-size:14px;line-height:1.55;color:#475569;">
                  <strong>Seller rule:</strong> Tracking must be uploaded within {TRACKING_DEADLINE_DAYS} days after buyer payment. If tracking is not uploaded in time, the transaction is cancelled and the buyer is refunded.<br><br>
                  <strong>Payment release rule:</strong> Bazont releases payment only after the courier confirms DELIVERED.
                </div>

                <p style="margin:24px 0 0;font-size:15px;color:#334155;">Thank you,<br><strong>The BAZONT Team</strong></p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def build_seller_invite_content(tx):
    return f"TO: {tx['seller_email']}\nSUBJECT: {build_seller_invite_subject(tx)}\n\n{build_seller_invite_plain_text(tx)}"


def send_seller_invite(tx):
    plain_text = build_seller_invite_plain_text(tx)
    html = build_seller_invite_html(tx)
    subject = build_seller_invite_subject(tx)

    # Always keep a local proof copy for testing and audit.
    outbox_file = app.config['EMAIL_OUTBOX_DIR'] / f"invite_{tx['public_id']}.txt"
    outbox_file.write_text(build_seller_invite_content(tx), encoding='utf-8')
    html_outbox_file = app.config['EMAIL_OUTBOX_DIR'] / f"invite_{tx['public_id']}.html"
    html_outbox_file.write_text(html, encoding='utf-8')

    if not RESEND_API_KEY:
        return False, 'Resend API key is not configured. Email preview was saved to the local outbox only.'
    if requests is None:
        return False, 'Python requests package is not available, so Bazont could not contact Resend.'

    payload = {
        'from': RESEND_FROM_EMAIL,
        'to': [tx['seller_email']],
        'subject': subject,
        'html': html,
        'text': plain_text,
    }
    headers = {
        'Authorization': f'Bearer {RESEND_API_KEY}',
        'Content-Type': 'application/json',
    }
    try:
        response = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=20)
        data = response.json() if response.content else {}
    except Exception as exc:
        return False, f'Resend email request failed: {exc}'

    if 200 <= response.status_code < 300:
        resend_id = data.get('id', 'sent') if isinstance(data, dict) else 'sent'
        return True, f'Bazont email sent to seller. Resend ID: {resend_id}'

    if isinstance(data, dict):
        detail = data.get('message') or data.get('error') or str(data)
    else:
        detail = response.text
    return False, f'Resend HTTP {response.status_code}: {detail}'




def get_or_create_user(conn, email, role, password='demo1234'):
    email = email.strip().lower()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    if user is None:
        conn.execute(
            'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (email, generate_password_hash(password), role, now_iso())
        )
        conn.commit()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    return user


def ensure_demo_users(conn):
    buyer = get_or_create_user(conn, 'buyer_demo@bazont.local', 'buyer')
    seller = get_or_create_user(conn, 'seller_demo@bazont.local', 'seller')
    return buyer, seller


def assign_seller_to_transaction(conn, tx, seller, actor_type='system', actor_ref='demo-shortcut', details='Seller linked by demo shortcut.'):
    if seller['role'] != 'seller':
        raise ValueError('Assigned user must be a seller.')
    if tx['seller_user_id'] and tx['seller_user_id'] != seller['id']:
        raise ValueError('This transaction is already assigned to another seller.')
    conn.execute('UPDATE transactions SET seller_user_id = ?, updated_at = ? WHERE id = ?', (seller['id'], now_iso(), tx['id']))
    log_audit(conn, tx['id'], actor_type, actor_ref, 'SELLER_JOINED', None, None, details)


def ensure_test_login():
    # TEMP TEST MODE — bypass login for local testing
    if LOGIN_REQUIRED:
        return
    if 'user_id' in session:
        return
    conn = get_db()
    user = get_or_create_user(conn, TEST_USER_EMAIL, TEST_USER_ROLE)
    session['user_id'] = user['id']
    session['user'] = {
        'email': TEST_USER_EMAIL,
        'role': TEST_USER_ROLE
    }

def check_due_tracking():
    if not aftership_enabled():
        return
    conn = db_connect()
    try:
        rows = conn.execute(
            """SELECT * FROM transactions
               WHERE status = 'TRACKING_SUBMITTED'
                 AND tracking_number IS NOT NULL
                 AND courier_slug IS NOT NULL
                 AND (tracking_next_check_at IS NULL OR tracking_next_check_at <= ?)""",
            (now_iso(),)
        ).fetchall()
        changed = False
        for tx in rows:
            ok, message, tracking = aftership_get_tracking_status(tx['tracking_number'], tx['courier_slug'])
            checked_at = now_iso()
            next_at = tracking_next_check_iso()
            tag = None
            checkpoint_note = message
            if ok and tracking:
                tag = (tracking.get('tag') or tracking.get('subtag') or '').strip()
                checkpoint = tracking.get('checkpoints', [])[-1] if tracking.get('checkpoints') else {}
                checkpoint_note = checkpoint.get('message') or checkpoint.get('checkpoint_time') or message
                conn.execute(
                    """UPDATE transactions
                       SET tracking_api_status = ?, tracking_api_message = ?, tracking_last_checked_at = ?,
                           tracking_next_check_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (tag or 'UNKNOWN', checkpoint_note, checked_at, next_at, checked_at, tx['id'])
                )
                conn.execute(
                    'INSERT INTO courier_events (transaction_id, tracking_number, status, event_time, source, note) VALUES (?, ?, ?, ?, ?, ?)',
                    (tx['id'], tx['tracking_number'], tag or 'UNKNOWN', checked_at, 'aftership', checkpoint_note)
                )
                log_audit(conn, tx['id'], 'system', 'aftership', 'COURIER_STATUS_CHECKED', tx['status'], tx['status'], f'{tag or "UNKNOWN"}: {checkpoint_note}')
                changed = True
            else:
                conn.execute(
                    """UPDATE transactions
                       SET tracking_api_status = ?, tracking_api_message = ?, tracking_last_checked_at = ?,
                           tracking_next_check_at = ?, updated_at = ?
                       WHERE id = ?""",
                    ('CHECK_FAILED', message, checked_at, next_at, checked_at, tx['id'])
                )
                log_audit(conn, tx['id'], 'system', 'aftership', 'COURIER_STATUS_CHECK_FAILED', tx['status'], tx['status'], message)
                changed = True
            if tag and tag.upper() in ALLOWED_DELIVERED_STATUSES:
                latest_tx = refresh_tx(conn, tx['id'])
                try:
                    set_status(conn, latest_tx, 'RELEASED', 'system', 'aftership', 'AUTO_RELEASE_DELIVERED', 'AfterShip reported DELIVERED.')
                except ValueError:
                    pass
        if changed:
            conn.commit()
    finally:
        conn.close()


def process_rules():
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status IN ('INVITED', 'PAID', 'TRACKING_SUBMITTED')"
        ).fetchall()
        current_time = now_ph()
        changed = False
        for tx in rows:
            tx_created = datetime.fromisoformat(tx['created_at'])
            payment_received_at = datetime.fromisoformat(tx['payment_received_at']) if tx['payment_received_at'] else None
            tracking_submitted_at = datetime.fromisoformat(tx['tracking_submitted_at']) if tx['tracking_submitted_at'] else None

            if tx['status'] == 'INVITED' and payment_received_at:
                try:
                    set_status(conn, tx, 'PAID', 'system', 'rule-engine', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx = refresh_tx(conn, tx['id'])
                    changed = True
                except ValueError:
                    pass

            if tx['status'] == 'PAID' and payment_received_at and current_time >= payment_received_at + timedelta(days=TRACKING_DEADLINE_DAYS):
                if not tx['tracking_number']:
                    try:
                        set_status(conn, tx, 'REFUNDED', 'system', 'rule-engine', 'AUTO_REFUND_NO_TRACKING', f'No tracking uploaded within {TRACKING_DEADLINE_DAYS} days of payment.')
                        tx = refresh_tx(conn, tx['id'])
                        changed = True
                    except ValueError:
                        pass

            latest_event = conn.execute(
                '''SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY event_time DESC, id DESC LIMIT 1''',
                (tx['id'],)
            ).fetchone()
            if latest_event and latest_event['status'].upper() in ALLOWED_DELIVERED_STATUSES:
                if tx['status'] in ('PAID', 'TRACKING_SUBMITTED'):
                    try:
                        set_status(conn, tx, 'RELEASED', 'system', 'rule-engine', 'AUTO_RELEASE_DELIVERED', f"Courier status {latest_event['status']} at {latest_event['event_time']}")
                        changed = True
                    except ValueError:
                        pass

            if tx['status'] == 'TRACKING_SUBMITTED' and latest_event and latest_event['status'].upper() in ALLOWED_DELIVERED_STATUSES:
                pass

        if changed:
            conn.commit()
    finally:
        conn.close()



def background_rule_loop():
    while True:
        try:
            check_due_tracking()
            process_rules()
        except Exception:
            pass
        time.sleep(RULE_LOOP_SECONDS)


def _send_root_file(filename):
    return send_from_directory(BASE_DIR, filename)


@app.route('/')
def gateway_home():
    return _send_root_file('page0.html')


@app.route('/page1')
@app.route('/page1_intro')
def page1_intro():
    return render_template('page1_intro.html')


@app.route('/page0')
@app.route('/page0.html')
def page0_welcome():
    return _send_root_file('page0.html')


@app.route('/style.css')
def gateway_style():
    return _send_root_file('style.css')


@app.route('/intro')
@app.route('/intro.html')
def intro_page():
    return _send_root_file('intro.html')


@app.route('/live.html')
def gateway_live_placeholder():
    return redirect(url_for('live_home'))


@app.route('/animation')
@app.route('/animation/')
def animation_index():
    return send_from_directory(BASE_DIR / 'animation', 'index.html')


@app.route('/animation/<path:filename>')
def animation_files(filename):
    return send_from_directory(BASE_DIR / 'animation', filename)


@app.route('/forms')
@app.route('/forms/')
def forms_index():
    return send_from_directory(BASE_DIR / 'forms', 'register.html')


@app.route('/forms/<path:filename>')
def forms_files(filename):
    return send_from_directory(BASE_DIR / 'forms', filename)


@app.route('/app')
@app.route('/app/')
def live_home():
    return render_template('index.html')


@app.route('/forms-api/session')
def forms_api_session():
    user = current_user()
    return jsonify({
        'logged_in': bool(user),
        'email': user['email'] if user else '',
        'role': user['role'] if user else '',
    })


@app.route('/forms-api/register', methods=['POST'])
def forms_api_register():
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', ''))
    role = str(data.get('role', '')).strip().lower()

    if role not in ('buyer', 'seller'):
        return jsonify({'ok': False, 'field': 'role', 'message': 'Select Buyer or Seller before continuing.'}), 400
    if not email:
        return jsonify({'ok': False, 'field': 'email', 'message': 'Email address is required.'}), 400
    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'ok': False, 'field': 'email', 'message': 'Enter a valid email address.'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'field': 'password', 'message': 'Password must be at least 8 characters.'}), 400
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        return jsonify({'ok': False, 'field': 'password', 'message': 'Password must include at least 1 letter and 1 number.'}), 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing:
        return jsonify({'ok': False, 'code': 'exists', 'message': 'Email already registered. Please log in.'}), 409

    conn.execute(
        'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
        (email, generate_password_hash(password), role, now_iso())
    )
    conn.commit()
    return jsonify({'ok': True})


@app.route('/forms-api/login', methods=['POST'])
def forms_api_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', ''))

    if not email or not password:
        return jsonify({'ok': False, 'message': 'Enter your email address and password.'}), 400

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'ok': False, 'message': 'Invalid email or password.'}), 401

    session['user_id'] = user['id']
    return jsonify({'ok': True, 'email': user['email'], 'role': user['role']})


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        role = request.form['role']
        if role not in ('buyer', 'seller'):
            flash('Unable to create account. Please try again.', 'error')
            return redirect(url_for('register'))
        conn = get_db()
        existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            session.pop('_flashes', None)
            return redirect(url_for('login'))
        conn.execute(
            'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (email, generate_password_hash(password), role, now_iso())
        )
        conn.commit()
        session['pending_login_email'] = email
        session['pending_login_password'] = password
        flash('Registration complete. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next', '').strip()
    fresh_login = request.args.get('fresh', '').strip()
    if request.method == 'GET' and fresh_login == '1':
        session.clear()
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        if not user or not check_password_hash(user['password_hash'], password):
            flash('Invalid email or password.', 'error')
            if next_url:
                return redirect(url_for('login', next=next_url))
            return redirect(url_for('login'))
        session['user_id'] = user['id']
        session.pop('pending_login_email', None)
        session.pop('pending_login_password', None)
        flash('Logged in successfully.', 'success')
        return redirect(url_for('role_select'))
    return render_template(
        'login.html',
        is_login_page=True,
        prefill_email=session.get('pending_login_email', ''),
        prefill_password=session.get('pending_login_password', '')
    )


@app.route('/test-login', methods=['POST'])
def test_login():
    # TEMPORARY TEST LOGIN ONLY — REMOVE BEFORE PRODUCTION
    conn = get_db()
    user = get_or_create_user(conn, TEST_USER_EMAIL, TEST_USER_ROLE)
    session['user_id'] = user['id']
    session['user'] = {
        'email': TEST_USER_EMAIL,
        'role': TEST_USER_ROLE
    }
    return redirect(url_for('role_select'))


@app.route('/role-select')
@login_required()
def role_select():
    return render_template('role_select.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('gateway_home'))


@app.route('/buyer/dashboard')
@login_required(role='buyer')
def buyer_dashboard():
    user = current_user()
    conn = get_db()
    transactions = conn.execute(
        'SELECT * FROM transactions WHERE buyer_user_id = ? ORDER BY id DESC',
        (user['id'],)
    ).fetchall()
    return render_template('buyer_dashboard.html', transactions=transactions)


@app.route('/buyer/transactions')
@login_required(role='buyer')
def buyer_transactions():
    user = current_user()
    conn = get_db()
    transactions = conn.execute(
        'SELECT * FROM transactions WHERE buyer_user_id = ? ORDER BY id DESC',
        (user['id'],)
    ).fetchall()
    return render_template('buyer_transactions.html', transactions=transactions)


@app.route('/buyer/transactions/new', methods=['GET', 'POST'])
@login_required(role='buyer')
def new_transaction():
    user = current_user()
    form_data = {
        'item_description': '',
        'seller_email': '',
        'item_price': '',
        'shipping_price': '',
        'weight_kg': '',
        'length_cm': '',
        'width_cm': '',
        'height_cm': '',
    }
    if request.method == 'POST':
        form_data = {k: request.form.get(k, '').strip() for k in form_data}
        errors = []
        try:
            item_description = form_data['item_description']
            seller_email = form_data['seller_email'].lower()
            item_price = float(form_data['item_price'])
            shipping_price = float(form_data['shipping_price'])
            weight_kg = float(form_data['weight_kg'])
            length_cm = float(form_data['length_cm'])
            width_cm = float(form_data['width_cm'])
            height_cm = float(form_data['height_cm'])
        except ValueError:
            errors.append('Please enter valid numbers in all price, weight, and size fields.')
        else:
            total_amount = round(item_price + shipping_price, 2)
            if any(v < 0 for v in (item_price, shipping_price, weight_kg, length_cm, width_cm, height_cm)):
                errors.append('Values cannot be negative.')
            if item_price > MAX_ITEM_PRICE:
                errors.append(f'Item price exceeds PHP {MAX_ITEM_PRICE:.2f}. Please reduce item price only.')
            if weight_kg > MAX_WEIGHT:
                errors.append(f'Weight exceeds {MAX_WEIGHT} kg.')
            if any(v > MAX_DIM for v in (length_cm, width_cm, height_cm)):
                errors.append(f'Each dimension must be <= {MAX_DIM:.0f} cm.')
            if not item_description:
                errors.append('Item description is required.')
            if user['email'] == seller_email:
                errors.append('Buyer and seller email must be different.')

        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('new_transaction.html', form_data=form_data)

        conn = get_db()
        public_id = 'TX-' + secrets.token_hex(4).upper()
        invite_token = secrets.token_urlsafe(24)
        now = now_iso()
        conn.execute(
            '''INSERT INTO transactions (
                public_id, buyer_user_id, seller_email, item_description, item_price, shipping_price, total_amount,
                weight_kg, length_cm, width_cm, height_cm, invite_token, invite_sent_at,
                status, hold_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INVITED', 'NOT_FUNDED', ?, ?)''',
            (public_id, user['id'], seller_email, item_description, item_price, shipping_price, total_amount,
             weight_kg, length_cm, width_cm, height_cm, invite_token, now, now, now)
        )
        tx_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        log_audit(conn, tx_id, 'buyer', user['email'], 'TRANSACTION_CREATED', None, 'INVITED', 'Buyer created transaction.')
        tx = conn.execute('SELECT * FROM transactions WHERE id = ?', (tx_id,)).fetchone()
        conn.commit()
        flash('Transaction created. The buyer must now complete payment before the seller invitation is sent.', 'success')
        return redirect(url_for('buyer_actions', public_id=public_id))

    return render_template('new_transaction.html', form_data=form_data)


@app.route('/buyer/transactions/latest/invitation-preview')
@login_required(role='buyer')
def latest_invitation_preview():
    # Build 13L: flow lock. Page 21 helper must not bypass payment.
    # Correct order: 21 Create -> 22 Pay -> 23 Invite -> 24 Courier.
    user = current_user()
    conn = get_db()
    tx = conn.execute(
        'SELECT * FROM transactions WHERE buyer_user_id = ? ORDER BY id DESC LIMIT 1',
        (user['id'],)
    ).fetchone()
    if not tx:
        flash('Create a transaction first.', 'error')
        return redirect(url_for('new_transaction'))
    return redirect(url_for('buyer_actions', public_id=tx['public_id']))


@app.route('/buyer/transactions/<public_id>/invitation-preview', methods=['GET', 'POST'])
@login_required(role='buyer')
def invitation_preview(public_id):
    user = current_user()
    conn = get_db()
    tx = conn.execute(
        'SELECT * FROM transactions WHERE public_id = ? AND buyer_user_id = ?',
        (public_id, user['id'])
    ).fetchone()
    if not tx:
        abort(404)

    if not tx['payment_received_at']:
        flash('Buyer payment must be recorded before the seller invitation is available.', 'error')
        return redirect(url_for('buyer_actions', public_id=public_id))

    if request.method == 'POST':
        ok, message = send_seller_invite(tx)
        actor_ref = 'resend' if ok else 'email-outbox'
        action = 'SELLER_INVITE_SENT' if ok else 'SELLER_INVITE_PREVIEW_CREATED'
        log_audit(conn, tx['id'], 'system', actor_ref, action, None, None, message)
        conn.commit()
        flash(message, 'success' if ok else 'error')
        return redirect(url_for('invitation_preview', public_id=public_id))

    invite_content = build_seller_invite_content(tx)
    return render_template('invitation_preview.html', tx=tx, invite_content=invite_content, financials=tx_financials(tx))


@app.route('/buyer/transactions/<public_id>/invitation-email-preview')
@login_required(role='buyer')
def invitation_email_preview(public_id):
    user = current_user()
    conn = get_db()
    tx = conn.execute(
        'SELECT * FROM transactions WHERE public_id = ? AND buyer_user_id = ?',
        (public_id, user['id'])
    ).fetchone()
    if not tx:
        abort(404)

    if not tx['payment_received_at']:
        flash('Buyer payment must be recorded before the seller invitation is available.', 'error')
        return redirect(url_for('buyer_actions', public_id=public_id))

    return build_seller_invite_html(tx), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/seller/join/<token>', methods=['GET', 'POST'])
def seller_join(token):
    conn = get_db()
    tx = conn.execute('SELECT * FROM transactions WHERE invite_token = ?', (token,)).fetchone()
    if not tx:
        abort(404)
    if tx['status'] == 'CANCELLED':
        flash('This transaction has been cancelled.', 'error')
        return redirect(url_for('login'))
    user = current_user()
    if user and user['role'] == 'buyer':
        flash('Seller join page is not available while viewing as buyer.', 'error')
        return redirect(url_for('courier_logs', public_id=tx['public_id']))
    if request.method == 'POST':
        seller_email = tx['seller_email'].lower()
        password = request.form['password']
        seller = conn.execute('SELECT * FROM users WHERE email = ?', (seller_email,)).fetchone()
        if seller is None:
            conn.execute(
                'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
                (seller_email, generate_password_hash(password), 'seller', now_iso())
            )
            seller = conn.execute('SELECT * FROM users WHERE email = ?', (seller_email,)).fetchone()
        elif seller['role'] != 'seller':
            flash('That email exists with a different role.', 'error')
            return redirect(request.url)
        elif not check_password_hash(seller['password_hash'], password):
            flash('Existing seller account found. Password does not match.', 'error')
            return redirect(request.url)

        if tx['seller_user_id'] and tx['seller_user_id'] != seller['id']:
            flash('This transaction is already assigned.', 'error')
            return redirect(url_for('login'))

        assign_seller_to_transaction(conn, tx, seller, 'seller', seller_email, 'Seller joined via email link.')
        conn.commit()
        session['user_id'] = seller['id']
        flash('Seller account linked to transaction.', 'success')
        return redirect(url_for('courier_logs', public_id=tx['public_id']))
    return render_template('seller_join.html', tx=tx)


@app.route('/transactions/<public_id>', methods=['GET', 'POST'])
@login_required()
def transaction_detail(public_id):
    legacy_view = request.args.get('view')
    if legacy_view == 'actions':
        return redirect(url_for('buyer_actions', public_id=public_id))
    if legacy_view == 'logs':
        return redirect(url_for('courier_logs', public_id=public_id))
    user = current_user()
    conn = get_db()
    tx = conn.execute('SELECT * FROM transactions WHERE public_id = ?', (public_id,)).fetchone()
    if not tx:
        abort(404)
    if user['id'] not in {tx['buyer_user_id'], tx['seller_user_id']}:
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action')
        tx = refresh_tx(conn, tx['id'])
        if action == 'pay' and user['id'] == tx['buyer_user_id']:
            test_card_number = request.form.get('test_card_number', '').strip()
            test_card_expiry = request.form.get('test_card_expiry', '').strip()
            test_card_cvv = request.form.get('test_card_cvv', '').strip()
            if tx['payment_received_at']:
                flash('Payment already recorded.', 'error')
            elif not (test_card_number and test_card_expiry and test_card_cvv):
                flash('Test card details are required.', 'error')
            elif test_card_number != '5555 5555 5555 4444':
                flash('Test card declined.', 'error')
            else:
                paid_at = now_iso()
                conn.execute(
                    'UPDATE transactions SET payment_received_at = ?, updated_at = ? WHERE id = ?',
                    (paid_at, paid_at, tx['id'])
                )
                log_audit(conn, tx['id'], 'buyer', user['email'], 'FULL_PAYMENT_RECEIVED', tx['status'], tx['status'], f"Buyer paid PHP {tx_financials(tx)['buyer_pays']:.2f}. Platform holds PHP {tx_financials(tx)['platform_holds']:.2f}.")
                tx_after_payment = refresh_tx(conn, tx['id'])
                if tx_after_payment['status'] == 'INVITED':
                    set_status(conn, tx_after_payment, 'PAID', 'system', 'payment-test', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx_after_payment = refresh_tx(conn, tx['id'])
                send_seller_invite(tx_after_payment)
                log_audit(conn, tx['id'], 'system', 'email-outbox', 'SELLER_INVITE_CREATED_AFTER_PAYMENT', None, None, f"Funded invite prepared for {tx['seller_email']}")
                conn.commit()
                flash('Full payment recorded and funded seller invitation prepared.', 'success')
                process_rules()
            return redirect(url_for('invitation_preview', public_id=public_id))

        if action == 'cancel' and user['id'] == tx['buyer_user_id']:
            if tx['status'] != 'INVITED' or tx['hold_status'] != 'NOT_FUNDED' or tx['payment_received_at']:
                flash('Cancel is allowed only for unfunded invite-stage transactions.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            set_status(conn, tx, 'CANCELLED', 'buyer', user['email'], 'TRANSACTION_CANCELLED', 'Buyer cancelled unfunded invite-stage transaction.')
            conn.commit()
            flash('Transaction cancelled.', 'success')
            return redirect(url_for('role_select'))

        if action == 'submit_tracking' and user['id'] == tx['seller_user_id']:
            tracking_number = request.form['tracking_number'].strip().upper()
            courier_name = request.form['courier_name'].strip()
            if not tracking_number or not courier_name:
                flash('Courier and tracking number are required.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            if not tx['payment_received_at']:
                flash('Buyer payment must be recorded before seller tracking can be submitted.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            if tx['status'] not in ('PAID', 'TRACKING_SUBMITTED'):
                flash('Tracking cannot be submitted in the current state.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            conn.execute(
                '''UPDATE transactions
                   SET tracking_number = ?, courier_name = ?, tracking_submitted_at = ?, updated_at = ?
                   WHERE id = ?''',
                (tracking_number, courier_name, now_iso(), now_iso(), tx['id'])
            )
            tx = refresh_tx(conn, tx['id'])
            if tx['status'] == 'PAID':
                set_status(conn, tx, 'TRACKING_SUBMITTED', 'seller', user['email'], 'TRACKING_SUBMITTED', f'{courier_name} / {tracking_number}')
            else:
                log_audit(conn, tx['id'], 'seller', user['email'], 'TRACKING_UPDATED', tx['status'], tx['status'], f'{courier_name} / {tracking_number}')
            conn.commit()
            flash('Tracking submitted.', 'success')
            return redirect(url_for('transaction_detail', public_id=public_id))


        flash('This action is not available for your current role or transaction stage.', 'error')
        return redirect(url_for('transaction_detail', public_id=public_id))

    audit = conn.execute('SELECT * FROM audit_log WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    courier_events = conn.execute('SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    invite_link = url_for('seller_join', token=tx['invite_token'], _external=True) if user['id'] == tx['buyer_user_id'] else None
    return render_template('transaction_detail.html', tx=tx, audit=audit, courier_events=courier_events, invite_link=invite_link)


def _get_authorized_transaction(public_id):
    user = current_user()
    conn = get_db()
    tx = conn.execute('SELECT * FROM transactions WHERE public_id = ?', (public_id,)).fetchone()
    if not tx:
        abort(404)
    if user['id'] not in {tx['buyer_user_id'], tx['seller_user_id']}:
        abort(403)
    return user, conn, tx


@app.route('/transactions/<public_id>/buyer-actions', methods=['GET', 'POST'])
@login_required()
def buyer_actions(public_id):
    user, conn, tx = _get_authorized_transaction(public_id)

    if request.method == 'POST':
        action = request.form.get('action')
        tx = refresh_tx(conn, tx['id'])
        if action == 'pay' and user['id'] == tx['buyer_user_id']:
            test_card_number = request.form.get('test_card_number', '').strip()
            test_card_expiry = request.form.get('test_card_expiry', '').strip()
            test_card_cvv = request.form.get('test_card_cvv', '').strip()
            if tx['payment_received_at']:
                flash('Payment already recorded.', 'error')
            elif not (test_card_number and test_card_expiry and test_card_cvv):
                flash('Test card details are required.', 'error')
            elif test_card_number != '5555 5555 5555 4444':
                flash('Test card declined.', 'error')
            else:
                paid_at = now_iso()
                conn.execute(
                    'UPDATE transactions SET payment_received_at = ?, updated_at = ? WHERE id = ?',
                    (paid_at, paid_at, tx['id'])
                )
                log_audit(conn, tx['id'], 'buyer', user['email'], 'FULL_PAYMENT_RECEIVED', tx['status'], tx['status'], f"Buyer paid PHP {tx_financials(tx)['buyer_pays']:.2f}. Platform holds PHP {tx_financials(tx)['platform_holds']:.2f}.")
                tx_after_payment = refresh_tx(conn, tx['id'])
                if tx_after_payment['status'] == 'INVITED':
                    set_status(conn, tx_after_payment, 'PAID', 'system', 'payment-test', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx_after_payment = refresh_tx(conn, tx['id'])
                send_seller_invite(tx_after_payment)
                log_audit(conn, tx['id'], 'system', 'email-outbox', 'SELLER_INVITE_CREATED_AFTER_PAYMENT', None, None, f"Funded invite prepared for {tx['seller_email']}")
                conn.commit()
                flash('Full payment recorded and funded seller invitation prepared.', 'success')
                process_rules()
            return redirect(url_for('invitation_preview', public_id=public_id))

        if action == 'cancel' and user['id'] == tx['buyer_user_id']:
            if tx['status'] != 'INVITED' or tx['hold_status'] != 'NOT_FUNDED' or tx['payment_received_at']:
                flash('Cancel is allowed only for unfunded invite-stage transactions.', 'error')
                return redirect(url_for('buyer_actions', public_id=public_id))
            set_status(conn, tx, 'CANCELLED', 'buyer', user['email'], 'TRANSACTION_CANCELLED', 'Buyer cancelled unfunded invite-stage transaction.')
            conn.commit()
            flash('Transaction cancelled.', 'success')
            return redirect(url_for('role_select'))

        flash('This action is not available for your current role or transaction stage.', 'error')
        return redirect(url_for('buyer_actions', public_id=public_id))

    return render_template('buyer_actions.html', tx=tx)


@app.route('/transactions/<public_id>/courier', methods=['GET', 'POST'])
@login_required()
def courier_logs(public_id):
    user, conn, tx = _get_authorized_transaction(public_id)

    if request.method == 'POST':
        if user['id'] != tx['seller_user_id']:
            abort(403)
        tracking_number = request.form.get('tracking_number', '').strip().upper()
        courier_name = request.form.get('courier_name', '').strip()
        pickup_date = request.form.get('pickup_date', '').strip()
        pickup_time = request.form.get('pickup_time', '').strip()
        courier_slug = normalise_courier_slug(courier_name)
        if not tracking_number or not courier_name:
            flash('Courier name and tracking number are required.', 'error')
            return redirect(url_for('courier_logs', public_id=public_id))
        if not tx['payment_received_at']:
            flash('Buyer payment must be recorded before seller tracking can be submitted.', 'error')
            return redirect(url_for('courier_logs', public_id=public_id))
        if tx['status'] not in ('PAID', 'TRACKING_SUBMITTED'):
            flash('Tracking cannot be submitted in the current transaction state.', 'error')
            return redirect(url_for('courier_logs', public_id=public_id))

        submitted_at = now_iso()
        api_status = 'VERIFYING_TRACKING'
        api_message = 'Tracking saved. AfterShip verification pending.'
        aftership_id = None

        ok, message, tracking = aftership_create_tracking(tracking_number, courier_slug)
        if ok and tracking:
            api_status = (tracking.get('tag') or 'TRACKING_ACCEPTED').strip()
            api_message = message
            aftership_id = str(tracking.get('id') or '')
        else:
            api_status = 'API_NOT_CONFIRMED'
            api_message = message

        conn.execute(
            """UPDATE transactions
               SET tracking_number = ?, courier_name = ?, courier_slug = ?, pickup_date = ?, pickup_time = ?,
                   tracking_submitted_at = ?, tracking_api_status = ?, tracking_api_message = ?,
                   aftership_tracking_id = ?, tracking_last_checked_at = ?, tracking_next_check_at = ?, updated_at = ?
               WHERE id = ?""",
            (tracking_number, courier_name, courier_slug, pickup_date, pickup_time, submitted_at, api_status,
             api_message, aftership_id, submitted_at, tracking_next_check_iso(), submitted_at, tx['id'])
        )
        tx = refresh_tx(conn, tx['id'])
        if tx['status'] == 'PAID':
            set_status(conn, tx, 'TRACKING_SUBMITTED', 'seller', user['email'], 'TRACKING_SUBMITTED', f'{courier_name} / {tracking_number}. {api_message}')
        else:
            log_audit(conn, tx['id'], 'seller', user['email'], 'TRACKING_UPDATED', tx['status'], tx['status'], f'{courier_name} / {tracking_number}. {api_message}')
        conn.execute(
            'INSERT INTO courier_events (transaction_id, tracking_number, status, event_time, source, note) VALUES (?, ?, ?, ?, ?, ?)',
            (tx['id'], tracking_number, api_status, submitted_at, 'seller/aftership', api_message)
        )
        conn.commit()
        flash('Tracking submitted. Bazont will check the courier every 1 minute in test mode.', 'success')
        process_rules()
        return redirect(url_for('courier_status', public_id=public_id))

    audit = conn.execute('SELECT * FROM audit_log WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    courier_events = conn.execute('SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    return render_template('courier_logs.html', tx=tx, audit=audit, courier_events=courier_events, aftership_enabled=aftership_enabled())


@app.route('/transactions/<public_id>/status')
@login_required()
def courier_status(public_id):
    user, conn, tx = _get_authorized_transaction(public_id)
    courier_events = conn.execute(
        'SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY id DESC',
        (tx['id'],)
    ).fetchall()
    last_event = courier_events[0] if courier_events else None
    return render_template('courier_status.html', tx=tx, courier_events=courier_events, last_event=last_event)


@app.route('/seller/dashboard')
@login_required(role='seller')
def seller_dashboard():
    user = current_user()
    conn = get_db()
    transactions = conn.execute(
        '''SELECT * FROM transactions
           WHERE seller_user_id = ?
           ORDER BY id DESC''',
        (user['id'],)
    ).fetchall()
    return render_template('seller_dashboard.html', transactions=transactions)


@app.route('/__version')
def version_route():
    return {'version': get_version()}


@app.route("/faq")
def faq():
    return render_template("faq.html")


if __name__ == '__main__':
    init_db()
    thread = threading.Thread(target=background_rule_loop, daemon=True)
    thread.start()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)

