import os
import sqlite3
import threading
import time
import secrets
import html
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for, abort, send_from_directory, jsonify, has_request_context
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
DEFAULT_VERSION = 'Bazont24V.zip'
DEMO_EMAILS = {'buyer_demo@bazont.local', 'seller_demo@bazont.local'}
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
COURIER_TEST_MODE = os.environ.get('BAZONT_COURIER_TEST_MODE', '1').strip().lower() not in ('0', 'false', 'no')
COURIER_TEST_SEQUENCE = ['TRACKING_UPLOADED', 'TRACKING_ACCEPTED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED']
AFTERSHIP_API_KEY = os.environ.get('AFTERSHIP_API_KEY', '').strip()
AFTERSHIP_BASE_URL = 'https://api.aftership.com/v4/trackings'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
RESEND_FROM_EMAIL = os.environ.get(
    'RESEND_FROM_EMAIL',
    'Bazont <noreply@bazont.com>'
).strip()
RESEND_API_URL = 'https://api.resend.com/emails'
# Bazont23P: public invite links must not use localhost/127.0.0.1.
# Set BAZONT_PUBLIC_BASE_URL on Render if the live URL differs.
PUBLIC_BASE_URL = os.environ.get('BAZONT_PUBLIC_BASE_URL', 'https://bazont.com').strip().rstrip('/')

LOCAL_RESEND_KEY_FILE = 'resend_api_key.txt'
if not AFTERSHIP_API_KEY and os.path.exists(LOCAL_RESEND_KEY_FILE):
    try:
        with open(LOCAL_RESEND_KEY_FILE, 'r', encoding='utf-8') as f:
            AFTERSHIP_API_KEY = f.read().strip()
    except Exception:
        pass

if not RESEND_API_KEY and os.path.exists(LOCAL_RESEND_KEY_FILE):
    try:
        with open(LOCAL_RESEND_KEY_FILE, 'r', encoding='utf-8') as f:
            RESEND_API_KEY = f.read().strip()
    except Exception:
        pass


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


def courier_test_next_status(current_status):
    current = (current_status or 'TRACKING_UPLOADED').strip().upper()
    if current in ('API_NOT_CONFIRMED', 'VERIFYING_TRACKING', 'UNKNOWN', 'CHECK_FAILED'):
        current = 'TRACKING_UPLOADED'
    if current not in COURIER_TEST_SEQUENCE:
        return COURIER_TEST_SEQUENCE[0]
    idx = COURIER_TEST_SEQUENCE.index(current)
    if idx >= len(COURIER_TEST_SEQUENCE) - 1:
        return COURIER_TEST_SEQUENCE[-1]
    return COURIER_TEST_SEQUENCE[idx + 1]


def courier_status_label(status):
    labels = {
        'TRACKING_UPLOADED': 'Tracking uploaded',
        'TRACKING_ACCEPTED': 'Accepted by courier',
        'IN_TRANSIT': 'In transit',
        'OUT_FOR_DELIVERY': 'Out for delivery',
        'DELIVERED': 'Delivered',
    }
    return labels.get((status or '').upper(), status or 'Tracking submitted')


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


def transaction_review_flag(tx, latest_delivery_status=None):
    """Return the operational refund/release eligibility flag for Back Office only."""
    status = canonical_status(tx['status'])
    latest_delivery = (latest_delivery_status or tx['tracking_api_status'] or '').strip().upper()
    if status == TX_RELEASED or tx['released_at']:
        return 'RELEASED'
    if status == TX_REFUNDED or tx['refunded_at']:
        return 'REFUNDED'
    if latest_delivery in ALLOWED_DELIVERED_STATUSES or status == TX_DELIVERED:
        return 'RELEASE DUE / DELIVERY CONFIRMED'
    paid_at_value = tx['payment_received_at']
    if paid_at_value and not tx['tracking_number']:
        try:
            paid_at = datetime.fromisoformat(paid_at_value)
            if now_ph() >= paid_at + timedelta(days=TRACKING_DEADLINE_DAYS):
                return 'REFUND DUE / TRACKING DEADLINE MISSED'
        except ValueError:
            return 'REVIEW_REQUIRED'
    if status == TX_REVIEW_REQUIRED:
        return 'REVIEW_REQUIRED'
    return 'OK / MONITORING'

def admin_payment_status(tx):
    if tx['released_at'] or canonical_status(tx['status']) == TX_RELEASED:
        return 'RELEASED'
    if tx['refunded_at'] or canonical_status(tx['status']) == TX_REFUNDED:
        return 'REFUNDED'
    if tx['payment_received_at'] or tx['hold_status'] == 'FUNDED':
        return 'FUNDED'
    return 'NOT_FUNDED'


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['EMAIL_OUTBOX_DIR'] = BASE_DIR / 'outbox'
app.config['EMAIL_OUTBOX_DIR'].mkdir(exist_ok=True)

LOGIN_REQUIRED = True
TEST_USER_EMAIL = 'test@bazont.local'
TEST_USER_ROLE = 'buyer'
DEV_RESET_VERSION = 'Bazont22W'
DEV_RESET_MARKER = DATA_DIR / f'.{DEV_RESET_VERSION}_dev_data_reset_done'


def bootstrap_runtime():
    # Prepare persistent directories and database for both local runs and WSGI imports.
    app.config['EMAIL_OUTBOX_DIR'].mkdir(exist_ok=True)
    init_db()
    run_one_time_development_reset()


def now_ph():
    return datetime.now(PH_TZ)


def now_iso():
    return now_ph().isoformat()


def now_ph_display():
    return now_ph().strftime('%Y-%m-%d %H:%M:%S PHST')


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

    # Bazont23R: normalize older state names to the current transaction state engine.
    conn.execute("UPDATE transactions SET status = 'FUNDED' WHERE status IN ('PAID', 'FUNDED')")
    conn.execute("UPDATE transactions SET status = 'INVITED' WHERE status = 'INVITED'")
    conn.execute("UPDATE transactions SET status = 'SELLER_JOINED' WHERE status = 'SELLER_JOINED'")
    conn.execute("UPDATE transactions SET status = 'TRACKING_UPLOADED' WHERE status = 'TRACKING_UPLOADED'")
    conn.execute("UPDATE transactions SET status = 'RELEASED' WHERE status = 'RELEASED'")
    conn.execute("UPDATE transactions SET status = 'REFUNDED' WHERE status = 'REFUNDED'")
    purge_legacy_demo_transaction_residue(conn)
    conn.commit()
    conn.close()


def run_one_time_development_reset():
    """Bazont23V: production persistence guard.

    Older builds used this hook to wipe local demo transactions once. That is
    now forbidden because registered accounts and transactions must survive
    browser refresh, logout/login, and app restart. Keep the function as a
    harmless compatibility hook only.
    """
    try:
        DEV_RESET_MARKER.write_text('persistence-preserved-' + now_iso(), encoding='utf-8')
    except OSError:
        pass
    return


def purge_legacy_demo_transaction_residue(conn):
    """Remove stale seeded/demo/audit transactions from the persistent store.

    Public pages must show only transactions created by real registered users.
    Audit inspection can still create temporary audit records later through
    /audit/p/<page_no>, but old seeded/demo rows must not leak into Page 22 or
    Page 19 in the normal buyer flow.
    """
    demo_emails = tuple(DEMO_EMAILS | {'seller.audit@bazont.local'})
    placeholders = ','.join('?' for _ in demo_emails)
    conn.execute(f"""
        DELETE FROM transactions
        WHERE public_id = 'TX-B72BAD32'
           OR item_description LIKE 'Audit access transaction%'
           OR seller_email IN ({placeholders})
           OR buyer_user_id IN (SELECT id FROM users WHERE email IN ({placeholders}))
           OR seller_user_id IN (SELECT id FROM users WHERE email IN ({placeholders}))
    """, demo_emails + demo_emails + demo_emails)
    conn.execute(f"DELETE FROM users WHERE email IN ({placeholders})", demo_emails)


TX_CREATED = 'CREATED'
TX_FUNDED = 'FUNDED'
TX_INVITED = 'INVITED'
TX_SELLER_JOINED = 'SELLER_JOINED'
TX_TRACKING_PENDING = 'TRACKING_PENDING'
TX_TRACKING_UPLOADED = 'TRACKING_UPLOADED'
TX_IN_TRANSIT = 'IN_TRANSIT'
TX_DELIVERED = 'DELIVERED'
TX_RELEASED = 'RELEASED'
TX_REFUNDED = 'REFUNDED'
TX_CANCELLED = 'CANCELLED'
TX_REVIEW_REQUIRED = 'REVIEW_REQUIRED'

# Backward-compatible constant names used by older templates/routes.
TX_FUNDED = TX_FUNDED
TX_INVITED = TX_INVITED
TX_SELLER_JOINED = TX_SELLER_JOINED
TX_TRACKING_UPLOADED = TX_TRACKING_UPLOADED
TX_RELEASED = TX_RELEASED
TX_REFUNDED = TX_REFUNDED

TX_TERMINAL_STATES = {TX_RELEASED, TX_REFUNDED, TX_CANCELLED}
TRANSACTION_STATUS_STATES = (
    TX_CREATED, TX_FUNDED, TX_INVITED, TX_SELLER_JOINED, TX_TRACKING_PENDING,
    TX_TRACKING_UPLOADED, TX_IN_TRANSIT, TX_DELIVERED, TX_RELEASED,
    TX_REFUNDED, TX_CANCELLED, TX_REVIEW_REQUIRED,
)

LEGACY_STATUS_MAP = {
    'PAID': TX_FUNDED,
    'FUNDED': TX_FUNDED,
    'INVITED': TX_INVITED,
    'SELLER_JOINED': TX_SELLER_JOINED,
    'TRACKING_UPLOADED': TX_TRACKING_UPLOADED,
    'RELEASED': TX_RELEASED,
    'REFUNDED': TX_REFUNDED,
}

VALID_TRANSITIONS = {
    TX_CREATED: {TX_FUNDED, TX_CANCELLED, TX_REVIEW_REQUIRED},
    TX_FUNDED: {TX_INVITED, TX_SELLER_JOINED, TX_TRACKING_PENDING, TX_TRACKING_UPLOADED, TX_DELIVERED, TX_RELEASED, TX_REFUNDED, TX_REVIEW_REQUIRED},
    TX_INVITED: {TX_SELLER_JOINED, TX_TRACKING_PENDING, TX_TRACKING_UPLOADED, TX_DELIVERED, TX_RELEASED, TX_REFUNDED, TX_REVIEW_REQUIRED},
    TX_SELLER_JOINED: {TX_TRACKING_PENDING, TX_TRACKING_UPLOADED, TX_DELIVERED, TX_RELEASED, TX_REFUNDED, TX_REVIEW_REQUIRED},
    TX_TRACKING_PENDING: {TX_TRACKING_UPLOADED, TX_REFUNDED, TX_REVIEW_REQUIRED},
    TX_TRACKING_UPLOADED: {TX_IN_TRANSIT, TX_DELIVERED, TX_RELEASED, TX_REFUNDED, TX_REVIEW_REQUIRED},
    TX_IN_TRANSIT: {TX_DELIVERED, TX_RELEASED, TX_REVIEW_REQUIRED},
    TX_DELIVERED: {TX_RELEASED, TX_REVIEW_REQUIRED},
    TX_RELEASED: set(),
    TX_REFUNDED: set(),
    TX_CANCELLED: set(),
    TX_REVIEW_REQUIRED: {TX_REFUNDED, TX_RELEASED, TX_CANCELLED},
}

def canonical_status(status):
    return LEGACY_STATUS_MAP.get(status, status)


def log_audit(conn, transaction_id, actor_type, actor_ref, action, from_state=None, to_state=None, details=''):
    conn.execute(
        '''INSERT INTO audit_log (transaction_id, actor_type, actor_ref, action, from_state, to_state, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (transaction_id, actor_type, actor_ref, action, from_state, to_state, details, now_iso())
    )



def set_status(conn, tx, new_status, actor_type, actor_ref, action, details=''):
    current = canonical_status(tx['status'])
    if current == new_status:
        return
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f'Invalid state transition: {current} -> {new_status}')

    hold_status = tx['hold_status']
    released_at = tx['released_at']
    refunded_at = tx['refunded_at']
    if new_status == TX_FUNDED:
        hold_status = 'FUNDED'
    elif new_status in (TX_TRACKING_PENDING, TX_TRACKING_UPLOADED, TX_IN_TRANSIT, TX_DELIVERED, TX_REVIEW_REQUIRED):
        hold_status = 'FUNDED'
    elif new_status == TX_RELEASED:
        hold_status = 'RELEASED'
        released_at = now_iso()
    elif new_status == TX_REFUNDED:
        hold_status = 'REFUNDED'
    elif new_status == TX_CANCELLED:
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



def establish_authenticated_session(user):
    """Create one complete server-side auth state; never leave stale display data."""
    session.clear()
    session['user_id'] = user['id']
    session['auth_email'] = user['email']
    session['auth_role'] = user['role']
    session['auth_ok'] = True
    session.pop('selected_role', None)


def clear_authenticated_session():
    """Remove every auth/display key while preserving later flash messages."""
    for key in ('user_id', 'user', 'auth_email', 'auth_role', 'auth_ok', 'selected_role'):
        session.pop(key, None)
    session['auth_logged_out'] = True


def audit_session_allows_current_endpoint():
    """Allow demo identities only for the one protected page entered from /audit/p/<page_no>."""
    if session.get('audit_access_mode') is not True:
        return False
    allowed_endpoint = session.get('audit_allowed_endpoint')
    if not allowed_endpoint:
        return False
    return request.endpoint == allowed_endpoint


def current_user():
    user_id = session.get('user_id')
    if session.get('auth_logged_out'):
        # A page that says "Logged out." must never also render a user pill or Logout.
        clear_authenticated_session()
        return None
    if not user_id or session.get('auth_ok') is not True:
        clear_authenticated_session()
        return None
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user is None:
        clear_authenticated_session()
        return None
    if user['email'] in DEMO_EMAILS:
        # Bazont24K: demo/audit users remain blocked in normal public flow.
        # They are valid only for the exact endpoint reached from /audit/p/<page_no>.
        if not audit_session_allows_current_endpoint():
            clear_authenticated_session()
            return None
    if session.get('auth_email') != user['email'] or session.get('auth_role') != user['role']:
        clear_authenticated_session()
        return None
    return user


def current_display_role():
    role = session.get('selected_role')
    if role in ('buyer', 'seller'):
        return role
    return ''


@app.context_processor
def inject_globals():
    return {
        'current_user': current_user(),
        'current_display_role': current_display_role(),
        'VERSION': (get_version() if get_version().endswith('.zip') else get_version() + '.zip'),
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
        'MASTER_PAGE_MAP': globals().get('MASTER_PAGE_MAP', []),
    }


bootstrap_runtime()


@app.before_request
def keep_session_until_logout():
    session.permanent = True


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
    """Return the seller invitation URL for the real email/link mode.

    Bazont24V: keep buyer-preview mode non-navigating, but make the real
    seller invitation use the actual running app host when generated during
    a request. This prevents local/test invitations from pointing to a
    placeholder public domain that can return Not Found, while still allowing
    Render/live deployments to force a public base URL with
    BAZONT_PUBLIC_BASE_URL.
    """
    token = tx['invite_token']
    if os.environ.get('BAZONT_PUBLIC_BASE_URL', '').strip():
        return f"{PUBLIC_BASE_URL}{url_for('seller_join', token=token)}"
    if has_request_context():
        return url_for('seller_join', token=token, _external=True)
    return f"{PUBLIC_BASE_URL}{url_for('seller_join', token=token)}"


@app.context_processor
def inject_public_invite_helpers():
    return {'seller_invite_link': seller_invite_link, 'PUBLIC_BASE_URL': PUBLIC_BASE_URL}


def build_seller_invite_subject(tx):
    return f"Join BAZONT transaction {tx['public_id']}"


def build_seller_invite_plain_text(tx):
    invite_link = seller_invite_link(tx)
    return f"""You have a Bazont Transaction Invitation.

You are invited to join a BAZONT transaction as the seller of:

Transaction ID: {tx['public_id']}
Description of the article: {tx['item_description']}
Total amount held by Bazont: PHP {tx['total_amount']:.2f}

Accept invitation:
{invite_link}

Seller rule: Tracking number from a reputable courier must be uploaded within 3 days. If tracking is not uploaded in time, the transaction is auto cancelled and the buyer is refunded.

Payment release rule: Bazont releases payment after the courier confirms your item has been DELIVERED.

Thank you,
The BAZONT Team
"""



def _html_fallback_link_block(invite_link, safe_invite_link):
    # Gmail can collapse raw localhost/debug links behind "Show quoted text".
    # Keep the visual email clean; for live public links, retain a short fallback line.
    lowered = invite_link.lower()
    if '127.0.0.1' in lowered or 'localhost' in lowered:
        return ''
    return f"""
                <p style="margin:0 0 8px;font-size:14px;line-height:1.5;color:#475569;">If the button above does not work, paste this secure invitation link into your browser:</p>
                <p style="margin:0 0 20px;font-size:13px;line-height:1.45;color:#2563eb;word-break:break-all;"><a href="{safe_invite_link}" style="color:#2563eb;text-decoration:none;">{safe_invite_link}</a></p>
"""

def build_seller_invite_html(tx, preview_mode=False):
    invite_link = seller_invite_link(tx)
    public_id = html.escape(str(tx['public_id']))
    item_description = html.escape(str(tx['item_description']))
    total_amount = f"PHP {tx['total_amount']:.2f}"
    safe_invite_link = html.escape(invite_link, quote=True)
    fallback_link_block = _html_fallback_link_block(invite_link, safe_invite_link)
    if preview_mode:
        accept_button_html = '''<button type="button" onclick="var msg=document.getElementById('bazont-preview-accept-note'); if(msg){ msg.style.display='inline-block'; msg.setAttribute('aria-hidden','false'); window.clearTimeout(window.bazontPreviewAcceptTimer); window.bazontPreviewAcceptTimer=window.setTimeout(function(){ msg.style.display='none'; msg.setAttribute('aria-hidden','true'); }, 4200); } return false;" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-size:16px;font-weight:900;padding:13px 28px;border-radius:999px;border:0;cursor:pointer;box-shadow:0 8px 18px rgba(37,99,235,0.24);">Accept Invitation</button>
                  <div id="bazont-preview-accept-note" role="status" aria-live="polite" aria-hidden="true" style="display:none;margin-top:12px;background:#16a34a;color:#ffffff;font-size:13px;line-height:1.45;font-weight:900;padding:10px 14px;border-radius:14px;box-shadow:0 10px 22px rgba(22,163,74,0.22);">This is the button the seller will click after you send the invitation.</div>'''
        fallback_link_block = ''
    else:
        accept_button_html = f'<a href="{safe_invite_link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-size:16px;font-weight:900;padding:13px 28px;border-radius:999px;">Accept Invitation</a>'
    return f"""<!doctype html>
<html style="height:100%;overflow:hidden;">
  <body style="margin:0;padding:0;background:#f4f7fb;height:100%;overflow:hidden;font-family:Arial,Helvetica,sans-serif;color:#172033;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7fb;padding:20px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:94%;background:#ffffff;border-radius:18px;border:1px solid #dbe4f0;box-shadow:0 10px 26px rgba(15,23,42,0.10);max-height:96vh;">
            <tr>
              <td style="background:#0f172a;padding:18px 28px;color:#ffffff;border-radius:18px 18px 0 0;">
                <div style="font-size:21px;font-weight:900;letter-spacing:0.8px;">BAZONT</div>
                <div style="font-size:13px;color:#bfdbfe;margin-top:4px;font-weight:700;">Safe Transactions</div>
              </td>
            </tr>
            <tr>
              <td style="padding:26px 28px 28px;">
                <h1 style="margin:0 0 12px;font-size:23px;line-height:1.22;color:#0f172a;">You have a Bazont Transaction Invitation.</h1>
                <p style="margin:0 0 18px;font-size:16px;line-height:1.48;color:#334155;">You are invited to join a BAZONT transaction as the seller of:</p>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dbe4f0;border-radius:14px;background:#f8fafc;margin:0 0 18px;">
                  <tr><td style="padding:16px 20px;">
                    <div style="font-size:13px;color:#64748b;font-weight:800;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:10px;">Transaction summary</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                      <tr><td style="padding:6px 0;color:#64748b;font-size:14px;font-weight:700;">Transaction ID</td><td style="padding:6px 0;color:#0f172a;font-size:14px;font-weight:900;text-align:right;">{public_id}</td></tr>
                      <tr><td style="padding:6px 0;color:#64748b;font-size:14px;font-weight:700;">Description of the article</td><td style="padding:6px 0;color:#0f172a;font-size:14px;font-weight:900;text-align:right;">{item_description}</td></tr>
                      <tr><td style="padding:6px 0;color:#64748b;font-size:14px;font-weight:700;">Total amount held by Bazont</td><td style="padding:6px 0;color:#16a34a;font-size:14px;font-weight:900;text-align:right;">{total_amount}</td></tr>
                    </table>
                  </td></tr>
                </table>

                <div style="text-align:center;margin:20px 0 18px;">
                  {accept_button_html}
                </div>

                {fallback_link_block}

                <div style="border-top:1px solid #e2e8f0;padding-top:12px;margin-top:14px;font-size:14px;line-height:1.42;color:#475569;">
                  <strong>Seller rule:</strong> Tracking number from a reputable courier must be uploaded within {TRACKING_DEADLINE_DAYS} days. If tracking is not uploaded in time, the transaction is auto cancelled and the buyer is refunded.<br><br>
                  <strong>Payment release rule:</strong> Bazont releases payment after the courier confirms your item has been DELIVERED.
                </div>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px;">
                  <tr>
                    <td style="font-size:15px;line-height:1.35;color:#334155;padding:0 0 4px;">
                      Thank you,<br><strong>The BAZONT Team</strong>
                    </td>
                  </tr>
                </table>
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
        response = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=8)
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
    tx_now = refresh_tx(conn, tx['id'])
    if tx_now['status'] in (TX_INVITED, TX_FUNDED):
        set_status(conn, tx_now, TX_SELLER_JOINED, actor_type, actor_ref, 'SELLER_JOINED', details)
    else:
        log_audit(conn, tx['id'], actor_type, actor_ref, 'SELLER_JOINED', None, None, details)


def ensure_test_login():
    # TEMP TEST MODE — bypass login for local testing
    if LOGIN_REQUIRED:
        return
    if 'user_id' in session:
        return
    conn = get_db()
    user = get_or_create_user(conn, TEST_USER_EMAIL, TEST_USER_ROLE)
    establish_authenticated_session(user)


# Bazont23P: temporary audit-only transaction helpers.
# These do not change the normal buyer/seller workflow; they only prevent dead
# audit links by resolving dynamic transaction pages to a real inspection record.
def get_or_create_audit_transaction(conn, buyer=None, paid=False, assign_seller=False, tracking=False, audit_key="default"):
    """Create a stable inspection transaction without using production state transitions.

    This is used only by /audit/p/<page_no>.  It deliberately avoids set_status()
    because an audit page may need to preview Page 17 or Page 25 directly from
    Home, even when a previous audit run left the same demo transaction in a
    different state.  Production routes and production auth remain unchanged.
    """
    if buyer is None:
        buyer, _seller = ensure_demo_users(conn)
    seller = get_or_create_user(conn, "seller.audit@bazont.local", "seller")
    description = f"Audit access transaction {audit_key}"
    tx = conn.execute(
        "SELECT * FROM transactions WHERE item_description = ? AND buyer_user_id = ? ORDER BY id DESC LIMIT 1",
        (description, buyer["id"])
    ).fetchone()
    if tx is None:
        public_id = "AUDIT-" + secrets.token_hex(4).upper()
        invite_token = secrets.token_urlsafe(24)
        now = now_iso()
        conn.execute(
            """INSERT INTO transactions (
                public_id, buyer_user_id, seller_email, item_description, item_price, shipping_price, total_amount,
                weight_kg, length_cm, width_cm, height_cm, invite_token, invite_sent_at,
                status, hold_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', 'NOT_FUNDED', ?, ?)""",
            (public_id, buyer["id"], "seller.audit@bazont.local", description, 8500.0, 300.0, 8800.0,
             1.0, 15.0, 15.0, 15.0, invite_token, now, now, now)
        )
        tx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_audit(conn, tx_id, "system", "page-audit", "AUDIT_TRANSACTION_CREATED", None, TX_CREATED, "Temporary Page Access / Audit transaction.")
        conn.commit()
        tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()

    now = now_iso()
    target_status = TX_CREATED
    hold_status = "NOT_FUNDED"
    payment_received_at = None
    seller_user_id = None
    tracking_number = None
    courier_name = None
    courier_slug = None
    tracking_submitted_at = None
    tracking_api_status = None
    tracking_api_message = None
    tracking_last_checked_at = None
    tracking_next_check_at = None

    if paid:
        target_status = TX_FUNDED
        hold_status = "FUNDED"
        payment_received_at = now
    if assign_seller:
        target_status = TX_SELLER_JOINED
        hold_status = "FUNDED"
        payment_received_at = payment_received_at or now
        seller_user_id = seller["id"]
    if tracking:
        target_status = TX_TRACKING_UPLOADED
        hold_status = "FUNDED"
        payment_received_at = payment_received_at or now
        seller_user_id = seller["id"]
        tracking_number = "AUDIT123456"
        courier_name = "LBC Express"
        courier_slug = "lbc-express"
        tracking_submitted_at = now
        tracking_api_status = "TRACKING_UPLOADED"
        tracking_api_message = "Temporary Page Access / Audit tracking state."
        tracking_last_checked_at = now
        tracking_next_check_at = tracking_next_check_iso()

    conn.execute(
        """UPDATE transactions
           SET payment_received_at = ?, seller_user_id = ?, tracking_number = ?, courier_name = ?, courier_slug = ?,
               tracking_submitted_at = ?, tracking_api_status = ?, tracking_api_message = ?, tracking_last_checked_at = ?,
               tracking_next_check_at = ?, status = ?, hold_status = ?, updated_at = ?
           WHERE id = ?""",
        (payment_received_at, seller_user_id, tracking_number, courier_name, courier_slug,
         tracking_submitted_at, tracking_api_status, tracking_api_message, tracking_last_checked_at,
         tracking_next_check_at, target_status, hold_status, now, tx["id"])
    )
    if tracking:
        existing_event = conn.execute(
            "SELECT id FROM courier_events WHERE transaction_id = ? AND source = ? LIMIT 1",
            (tx["id"], "page-audit")
        ).fetchone()
        if existing_event is None:
            conn.execute(
                "INSERT INTO courier_events (transaction_id, tracking_number, status, event_time, source, note) VALUES (?, ?, ?, ?, ?, ?)",
                (tx["id"], "AUDIT123456", "TRACKING_UPLOADED", now, "page-audit", "Temporary Page Access / Audit courier event.")
            )
    log_audit(conn, tx["id"], "system", "page-audit", "AUDIT_STATE_PREPARED", tx["status"], target_status, f"Temporary Page Access / Audit state for {audit_key}.")
    conn.commit()
    return refresh_tx(conn, tx["id"])

def check_due_tracking():
    conn = db_connect()
    try:
        rows = conn.execute(
            """SELECT * FROM transactions
               WHERE status IN ('TRACKING_UPLOADED', 'IN_TRANSIT', 'DELIVERED')
                 AND tracking_number IS NOT NULL
                 AND (tracking_next_check_at IS NULL OR tracking_next_check_at <= ?)
                 AND hold_status NOT IN ('RELEASED', 'REFUNDED', 'CANCELLED')""",
            (now_iso(),)
        ).fetchall()
        changed = False
        for tx in rows:
            checked_at = now_iso()
            next_at = tracking_next_check_iso()

            # Bazont23S: final courier transition repair.
            # The simulator now promotes courier states into the transaction record and
            # then releases the simulated held payment when DELIVERED is reached.
            if COURIER_TEST_MODE:
                current_api_status = (tx['tracking_api_status'] or 'TRACKING_UPLOADED').strip().upper()
                next_status = courier_test_next_status(current_api_status)
                note = f"TEST MODE: {courier_status_label(next_status)}. Auto-advanced by Bazont setup monitor."
                conn.execute(
                    """UPDATE transactions
                       SET tracking_api_status = ?, tracking_api_message = ?, tracking_last_checked_at = ?,
                           tracking_next_check_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (next_status, note, checked_at, next_at, checked_at, tx['id'])
                )
                conn.execute(
                    'INSERT INTO courier_events (transaction_id, tracking_number, status, event_time, source, note) VALUES (?, ?, ?, ?, ?, ?)',
                    (tx['id'], tx['tracking_number'], next_status, checked_at, 'bazont-test-monitor', note)
                )
                log_audit(conn, tx['id'], 'system', 'bazont-test-monitor', 'COURIER_TEST_STATUS_ADVANCED', tx['status'], tx['status'], f"current={current_api_status}; next={next_status}; release_trigger=no")
                changed = True

                latest_tx = refresh_tx(conn, tx['id'])
                try:
                    if next_status == 'IN_TRANSIT' and canonical_status(latest_tx['status']) == TX_TRACKING_UPLOADED:
                        set_status(conn, latest_tx, TX_IN_TRANSIT, 'system', 'bazont-test-monitor', 'COURIER_IN_TRANSIT', 'TEST MODE courier status reached IN_TRANSIT.')
                        latest_tx = refresh_tx(conn, tx['id'])
                    elif next_status == 'DELIVERED':
                        if canonical_status(latest_tx['status']) in (TX_TRACKING_UPLOADED, TX_IN_TRANSIT):
                            set_status(conn, latest_tx, TX_DELIVERED, 'system', 'bazont-test-monitor', 'COURIER_DELIVERED', 'TEST MODE courier status reached DELIVERED.')
                            latest_tx = refresh_tx(conn, tx['id'])
                        if canonical_status(latest_tx['status']) == TX_DELIVERED and latest_tx['hold_status'] != 'RELEASED':
                            set_status(conn, latest_tx, TX_RELEASED, 'system', 'bazont-test-monitor', 'PAYMENT_RELEASED_TEST_DELIVERED', 'TEST MODE release trigger fired after courier DELIVERED.')
                            log_audit(conn, tx['id'], 'system', 'bazont-test-monitor', 'RELEASE_TRIGGER_DIAGNOSTIC', TX_DELIVERED, TX_RELEASED, f"current={current_api_status}; next={next_status}; release_trigger=yes")
                except ValueError as exc:
                    log_audit(conn, tx['id'], 'system', 'bazont-test-monitor', 'RELEASE_TRIGGER_DIAGNOSTIC', latest_tx['status'], latest_tx['status'], f"current={current_api_status}; next={next_status}; release_trigger=blocked; error={exc}")
                continue

            if not aftership_enabled():
                conn.execute(
                    """UPDATE transactions
                       SET tracking_api_status = ?, tracking_api_message = ?, tracking_last_checked_at = ?,
                           tracking_next_check_at = ?, updated_at = ?
                       WHERE id = ?""",
                    ('CHECK_PENDING', 'AfterShip API key is not configured. Waiting for real courier check.', checked_at, next_at, checked_at, tx['id'])
                )
                changed = True
                continue

            ok, message, tracking = aftership_get_tracking_status(tx['tracking_number'], tx['courier_slug'])
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
                    if canonical_status(latest_tx['status']) in (TX_TRACKING_UPLOADED, TX_IN_TRANSIT):
                        set_status(conn, latest_tx, TX_DELIVERED, 'system', 'aftership', 'COURIER_DELIVERED', 'AfterShip reported DELIVERED.')
                        latest_tx = refresh_tx(conn, tx['id'])
                    if canonical_status(latest_tx['status']) == TX_DELIVERED and latest_tx['hold_status'] != 'RELEASED':
                        set_status(conn, latest_tx, TX_RELEASED, 'system', 'aftership', 'PAYMENT_RELEASED_DELIVERY_CONFIRMED', 'Release trigger fired after AfterShip DELIVERED.')
                        log_audit(conn, tx['id'], 'system', 'aftership', 'RELEASE_TRIGGER_DIAGNOSTIC', TX_DELIVERED, TX_RELEASED, 'release_trigger=yes; source=aftership')
                except ValueError as exc:
                    log_audit(conn, tx['id'], 'system', 'aftership', 'RELEASE_TRIGGER_DIAGNOSTIC', latest_tx['status'], latest_tx['status'], f'release_trigger=blocked; error={exc}')
        if changed:
            conn.commit()
    finally:
        conn.close()


def process_rules():
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status IN ('CREATED', 'FUNDED', 'INVITED', 'SELLER_JOINED', 'TRACKING_PENDING', 'TRACKING_UPLOADED', 'IN_TRANSIT', 'DELIVERED')"
        ).fetchall()
        current_time = now_ph()
        changed = False
        for tx in rows:
            tx_created = datetime.fromisoformat(tx['created_at'])
            payment_received_at = datetime.fromisoformat(tx['payment_received_at']) if tx['payment_received_at'] else None
            tracking_submitted_at = datetime.fromisoformat(tx['tracking_submitted_at']) if tx['tracking_submitted_at'] else None

            if tx['status'] == TX_CREATED and payment_received_at:
                try:
                    set_status(conn, tx, TX_FUNDED, 'system', 'rule-engine', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx = refresh_tx(conn, tx['id'])
                    changed = True
                except ValueError:
                    pass

            if tx['status'] in (TX_FUNDED, TX_INVITED, TX_SELLER_JOINED) and payment_received_at and current_time >= payment_received_at + timedelta(days=TRACKING_DEADLINE_DAYS):
                if not tx['tracking_number']:
                    try:
                        set_status(conn, tx, TX_REVIEW_REQUIRED, 'system', 'rule-engine', 'REFUND_DUE_TRACKING_DEADLINE_MISSED', f'No tracking uploaded within {TRACKING_DEADLINE_DAYS} days of payment.')
                        tx = refresh_tx(conn, tx['id'])
                        changed = True
                    except ValueError:
                        pass

            latest_event = conn.execute(
                '''SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY event_time DESC, id DESC LIMIT 1''',
                (tx['id'],)
            ).fetchone()
            if latest_event and latest_event['status'].upper() in ALLOWED_DELIVERED_STATUSES:
                if canonical_status(tx['status']) in (TX_TRACKING_UPLOADED, TX_IN_TRANSIT, TX_DELIVERED):
                    try:
                        if canonical_status(tx['status']) != TX_DELIVERED:
                            set_status(conn, tx, TX_DELIVERED, 'system', 'rule-engine', 'COURIER_DELIVERED', f"Courier status {latest_event['status']} at {latest_event['event_time']}")
                            tx = refresh_tx(conn, tx['id'])
                        if canonical_status(tx['status']) == TX_DELIVERED and tx['hold_status'] != 'RELEASED':
                            set_status(conn, tx, TX_RELEASED, 'system', 'rule-engine', 'PAYMENT_RELEASED_DELIVERY_CONFIRMED', f"Courier status {latest_event['status']} at {latest_event['event_time']}")
                        changed = True
                    except ValueError as exc:
                        log_audit(conn, tx['id'], 'system', 'rule-engine', 'RELEASE_TRIGGER_DIAGNOSTIC', tx['status'], tx['status'], f'release_trigger=blocked; error={exc}')

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
    return render_gateway_home_page()


@app.route('/page1')
@app.route('/page1_intro')
def page1_intro():
    return render_template('page1_intro.html')


@app.route('/page0')
@app.route('/page0.html')
def page0_welcome():
    return render_gateway_home_page()


@app.route('/index')
def index_page():
    return render_index_page()


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
    # Bazont23Q v26 cleanup: /animation must no longer show the obsolete
    # demo landing page. The canonical walkthrough pages are audit pages 4-9.
    # /animation without a step now opens the real Page 4.
    raw_step = request.args.get('step')
    if not raw_step:
        return redirect('/animation/?step=1')
    try:
        step = int(raw_step)
    except (TypeError, ValueError):
        step = 1
    if step < 1 or step > 6:
        return redirect('/animation/?step=1')
    return send_from_directory(BASE_DIR / 'animation', 'index.html')


@app.route('/animation/<path:filename>')
def animation_files(filename):
    return send_from_directory(BASE_DIR / 'animation', filename)


@app.route('/forms')
@app.route('/forms/')
def forms_index():
    return redirect(url_for('register'))


@app.route('/forms/<path:filename>')
def forms_files(filename):
    return send_from_directory(BASE_DIR / 'forms', filename)


@app.route('/app')
@app.route('/app/')
def live_home():
    # Bazont23Q v26 cleanup: legacy /app created a duplicate Page 5.
    # It is no longer a canonical user page; send users to Register.
    return redirect(url_for('register'))


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
    password_hash = generate_password_hash(password)
    if existing:
        conn.execute(
            'UPDATE users SET password_hash = ?, role = ? WHERE email = ?',
            (password_hash, role, email)
        )
    else:
        conn.execute(
            'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (email, password_hash, role, now_iso())
        )
    conn.commit()
    session['pending_login_email'] = email
    session['pending_login_password'] = password
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

    establish_authenticated_session(user)
    return jsonify({'ok': True, 'email': user['email'], 'role': user['role']})


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if email in DEMO_EMAILS:
            flash('Demo accounts are disabled in the public flow.', 'error')
            return redirect(url_for('register'))
        role = request.form['role']
        if role not in ('buyer', 'seller'):
            flash('Unable to create account. Please try again.', 'error')
            return redirect(url_for('register'))
        conn = get_db()
        existing = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        password_hash = generate_password_hash(password)
        if existing:
            # Bazont23X: local/test registration must refresh the real account
            # credentials so Register -> Login always validates against the
            # same persistent users table.  No demo fallback is used.
            conn.execute(
                'UPDATE users SET password_hash = ?, role = ? WHERE email = ?',
                (password_hash, role, email)
            )
        else:
            conn.execute(
                'INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
                (email, password_hash, role, now_iso())
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
        if email in DEMO_EMAILS:
            session['pending_login_email'] = ''
            flash('Demo accounts are disabled in the public flow.', 'error')
            return redirect(url_for('login'))
        if not user or not check_password_hash(user['password_hash'], password):
            session['pending_login_email'] = email
            flash('Invalid email or password.', 'error')
            if next_url:
                return redirect(url_for('login', next=next_url))
            return redirect(url_for('login'))
        establish_authenticated_session(user)
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
    establish_authenticated_session(user)
    return redirect(url_for('role_select'))


@app.route('/role-select')
@login_required()
def role_select():
    return render_template('role_select.html')


@app.route('/logout')
def logout():
    session.clear()
    session['auth_logged_out'] = True
    flash('Logged out.', 'success')
    return redirect(url_for('gateway_home'))


@app.route('/choose-role/<role>')
@login_required()
def choose_role(role):
    role = (role or '').strip().lower()
    if role not in ('buyer', 'seller'):
        abort(404)
    user = current_user()
    conn = get_db()
    conn.execute('UPDATE users SET role = ? WHERE id = ?', (role, user['id']))
    conn.commit()
    session['auth_role'] = role
    session['selected_role'] = role
    if role == 'seller':
        return redirect(url_for('seller_dashboard'))
    return redirect(url_for('buyer_dashboard'))


@app.route('/buyer/dashboard')
@login_required(role='buyer')
def buyer_dashboard():
    user = current_user()
    conn = get_db()
    transactions = conn.execute(
        """
        SELECT * FROM transactions
        WHERE buyer_user_id = ?
          AND public_id <> 'TX-B72BAD32'
          AND item_description NOT LIKE 'Audit access transaction%'
          AND seller_email NOT IN ('buyer_demo@bazont.local', 'seller_demo@bazont.local', 'seller.audit@bazont.local')
        ORDER BY id DESC
        """,
        (user['id'],)
    ).fetchall()
    return render_template('buyer_dashboard.html', transactions=transactions)


@app.route('/buyer/transactions')
@login_required(role='buyer')
def buyer_transactions():
    user = current_user()
    conn = get_db()
    transactions = conn.execute(
        """
        SELECT * FROM transactions
        WHERE buyer_user_id = ?
          AND public_id <> 'TX-B72BAD32'
          AND item_description NOT LIKE 'Audit access transaction%'
          AND seller_email NOT IN ('buyer_demo@bazont.local', 'seller_demo@bazont.local', 'seller.audit@bazont.local')
        ORDER BY id DESC
        """,
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', 'NOT_FUNDED', ?, ?)''',
            (public_id, user['id'], seller_email, item_description, item_price, shipping_price, total_amount,
             weight_kg, length_cm, width_cm, height_cm, invite_token, now, now, now)
        )
        tx_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        log_audit(conn, tx_id, 'buyer', user['email'], 'TRANSACTION_CREATED', None, TX_CREATED, 'Buyer created transaction.')
        tx = conn.execute('SELECT * FROM transactions WHERE id = ?', (tx_id,)).fetchone()
        conn.commit()
        flash('Transaction created. The buyer must now complete payment before the seller invitation is sent.', 'success')
        return redirect(url_for('buyer_actions', public_id=public_id))

    return render_template('new_transaction.html', form_data=form_data)


@app.route('/buyer/transactions/latest/invitation-preview')
@login_required(role='buyer')
def latest_invitation_preview():
    # Bazont23X: public Next / Preview bypass removed. Transactions must be
    # created through the Page 14 form so required fields and payment flow run.
    flash('Please complete the transaction form before continuing.', 'error')
    return redirect(url_for('new_transaction'))


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
        sent_at_display = now_ph_display()
        actor_ref = 'resend' if ok else 'email-outbox'
        action = 'SELLER_INVITED' if ok else 'SELLER_INVITE_PREVIEW_CREATED'
        log_audit(conn, tx['id'], 'system', actor_ref, action, None, None, message)
        conn.commit()
        if ok:
            tx_now = refresh_tx(conn, tx['id'])
            if tx_now['status'] == TX_FUNDED:
                set_status(conn, tx_now, TX_INVITED, 'system', actor_ref, 'SELLER_INVITED_STATE', message)
                conn.commit()
            return redirect(url_for(
                'invitation_preview',
                public_id=public_id,
                email_sent='1',
                email_delivery='SENT',
                email_message=message,
                sent_at=sent_at_display,
                recipient=tx['seller_email']
            ))
        return redirect(url_for(
            'invitation_preview',
            public_id=public_id,
            email_delivery='FAILED',
            email_message=message,
            recipient=tx['seller_email']
        ))

    invitation_sent = conn.execute(
        "SELECT 1 FROM audit_log WHERE transaction_id = ? AND action = 'SELLER_INVITED' LIMIT 1",
        (tx['id'],)
    ).fetchone() is not None
    invite_content = build_seller_invite_content(tx)
    return render_template('invitation_preview.html', tx=tx, invite_content=invite_content, financials=tx_financials(tx), invitation_sent=invitation_sent)


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

    email_html = build_seller_invite_html(tx, preview_mode=True)
    if session.get('audit_access_mode'):
        badge = '<div class="page-id-badge" style="position:fixed;top:8px;left:10px;z-index:2147483647;background:rgba(15,23,42,0.92);color:#fff;padding:4px 8px;border-radius:999px;font:700 12px/1 Arial,sans-serif;letter-spacing:0.04em;pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,0.28);">20</div>'
        if '<body' in email_html:
            email_html = re.sub(r'(<body[^>]*>)', r'\1' + badge, email_html, count=1, flags=re.I)
        else:
            email_html = badge + email_html
    return email_html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/seller/join/<token>', methods=['GET', 'POST'])
def seller_join(token):
    conn = get_db()
    tx = conn.execute('SELECT * FROM transactions WHERE invite_token = ?', (token,)).fetchone()
    if not tx:
        abort(404)
    if tx['status'] == TX_CANCELLED:
        flash('This transaction has been cancelled.', 'error')
        return redirect(url_for('login'))
    user = current_user()
    if user and user['role'] == 'buyer':
        # Development-safe seller handover: the invitation link must be testable
        # in the same browser after the buyer sends it.  End the buyer session
        # and show the seller join form instead of bouncing back to courier.
        clear_authenticated_session()
        flash('Buyer session ended for seller invitation testing. Join below as the seller for this transaction.', 'success')
        user = None
    elif user and user['role'] == 'seller':
        # Bazont22W: cross-device invite acceptance.
        # If the seller is already logged in on PC2/phone and clicks the email
        # invitation, accept the invite directly when the logged-in email matches
        # the transaction seller email.  Do not ask for another password.
        seller_email = tx['seller_email'].strip().lower()
        if user['email'].strip().lower() != seller_email:
            flash('This invitation is for a different seller email. Please log out and use the invited seller account.', 'error')
            return render_template('seller_join.html', tx=tx)
        if tx['seller_user_id'] and tx['seller_user_id'] != user['id']:
            flash('This transaction is already assigned to another seller.', 'error')
            return redirect(url_for('seller_dashboard'))
        assign_seller_to_transaction(conn, tx, user, 'seller', user['email'], 'Seller accepted email invitation from an existing seller session.')
        conn.commit()
        flash('Invitation accepted. Seller account linked to transaction.', 'success')
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
        establish_authenticated_session(seller)
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
    is_seeded_or_audit_tx = (
        tx['public_id'] == 'TX-B72BAD32'
        or (tx['item_description'] or '').startswith('Audit access transaction')
        or (tx['seller_email'] or '').lower() in DEMO_EMAILS | {'seller.audit@bazont.local'}
    )
    if is_seeded_or_audit_tx and not session.get('audit_access_mode'):
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
                if tx_after_payment['status'] == TX_CREATED:
                    set_status(conn, tx_after_payment, TX_FUNDED, 'system', 'payment-test', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx_after_payment = refresh_tx(conn, tx['id'])
                log_audit(conn, tx['id'], 'system', 'payment-test', 'SELLER_INVITE_READY_AFTER_PAYMENT', None, None, f"Payment recorded. Seller invitation email is ready to send from Page 16 for {tx['seller_email']}")
                conn.commit()
                flash('Full payment recorded. Send the seller invitation from Page 16.', 'success')
                process_rules()
            return redirect(url_for('invitation_preview', public_id=public_id))

        if action == 'cancel' and user['id'] == tx['buyer_user_id']:
            if tx['status'] != TX_CREATED or tx['hold_status'] != 'NOT_FUNDED' or tx['payment_received_at']:
                flash('Cancel is allowed only for unfunded invite-stage transactions.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            set_status(conn, tx, TX_CANCELLED, 'buyer', user['email'], 'TRANSACTION_CANCELLED', 'Buyer cancelled unfunded invite-stage transaction.')
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
            if tx['status'] not in (TX_SELLER_JOINED, TX_TRACKING_UPLOADED):
                flash('Tracking cannot be submitted in the current state.', 'error')
                return redirect(url_for('transaction_detail', public_id=public_id))
            conn.execute(
                '''UPDATE transactions
                   SET tracking_number = ?, courier_name = ?, tracking_submitted_at = ?, updated_at = ?
                   WHERE id = ?''',
                (tracking_number, courier_name, now_iso(), now_iso(), tx['id'])
            )
            tx = refresh_tx(conn, tx['id'])
            if tx['status'] == TX_SELLER_JOINED:
                set_status(conn, tx, TX_TRACKING_UPLOADED, 'seller', user['email'], 'TRACKING_UPLOADED', f'{courier_name} / {tracking_number}')
            else:
                log_audit(conn, tx['id'], 'seller', user['email'], 'TRACKING_UPDATED', tx['status'], tx['status'], f'{courier_name} / {tracking_number}')
            conn.commit()
            flash('Tracking submitted.', 'success')
            return redirect(url_for('transaction_detail', public_id=public_id))


        flash('This action is not available for your current role or transaction stage.', 'error')
        return redirect(url_for('transaction_detail', public_id=public_id))

    audit = conn.execute('SELECT * FROM audit_log WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    courier_events = conn.execute('SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY id DESC', (tx['id'],)).fetchall()
    invite_link = seller_invite_link(tx) if user['id'] == tx['buyer_user_id'] else None
    return render_template('transaction_detail.html', tx=tx, audit=audit, courier_events=courier_events, invite_link=invite_link)


def _get_authorized_transaction(public_id):
    user = current_user()
    conn = get_db()
    tx = conn.execute('SELECT * FROM transactions WHERE public_id = ?', (public_id,)).fetchone()
    if not tx:
        abort(404)
    is_seeded_or_audit_tx = (
        tx['public_id'] == 'TX-B72BAD32'
        or (tx['item_description'] or '').startswith('Audit access transaction')
        or (tx['seller_email'] or '').lower() in DEMO_EMAILS | {'seller.audit@bazont.local'}
    )
    if is_seeded_or_audit_tx and not session.get('audit_access_mode'):
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
                if tx_after_payment['status'] == TX_CREATED:
                    set_status(conn, tx_after_payment, TX_FUNDED, 'system', 'payment-test', 'PAYMENT_CONFIRMED', 'Buyer paid full amount.')
                    tx_after_payment = refresh_tx(conn, tx['id'])
                log_audit(conn, tx['id'], 'system', 'payment-test', 'SELLER_INVITE_READY_AFTER_PAYMENT', None, None, f"Payment recorded. Seller invitation email is ready to send from Page 16 for {tx['seller_email']}")
                conn.commit()
                flash('Full payment recorded. Send the seller invitation from Page 16.', 'success')
                process_rules()
            return redirect(url_for('invitation_preview', public_id=public_id))

        if action == 'cancel' and user['id'] == tx['buyer_user_id']:
            if tx['status'] != TX_CREATED or tx['hold_status'] != 'NOT_FUNDED' or tx['payment_received_at']:
                flash('Cancel is allowed only for unfunded invite-stage transactions.', 'error')
                return redirect(url_for('buyer_actions', public_id=public_id))
            set_status(conn, tx, TX_CANCELLED, 'buyer', user['email'], 'TRANSACTION_CANCELLED', 'Buyer cancelled unfunded invite-stage transaction.')
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
        if tx['status'] not in (TX_SELLER_JOINED, TX_TRACKING_UPLOADED):
            flash('Tracking cannot be submitted in the current transaction state.', 'error')
            return redirect(url_for('courier_logs', public_id=public_id))

        submitted_at = now_iso()
        api_status = 'TRACKING_UPLOADED' if COURIER_TEST_MODE else 'VERIFYING_TRACKING'
        api_message = 'Tracking saved. Bazont test monitor will update every 1 minute.' if COURIER_TEST_MODE else 'Tracking saved. AfterShip verification pending.'
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
        if tx['status'] == TX_SELLER_JOINED:
            set_status(conn, tx, TX_TRACKING_UPLOADED, 'seller', user['email'], 'TRACKING_UPLOADED', f'{courier_name} / {tracking_number}. {api_message}')
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
    # Bazont23P: Page 25 is now the monitored courier-status hub.
    # Refresh courier/rule state before rendering so the page reflects current progress.
    try:
        check_due_tracking()
        process_rules()
    except Exception:
        pass

    user, conn, tx = _get_authorized_transaction(public_id)
    tx = refresh_tx(conn, tx['id'])
    courier_events = conn.execute(
        'SELECT * FROM courier_events WHERE transaction_id = ? ORDER BY id DESC',
        (tx['id'],)
    ).fetchall()
    last_event = courier_events[0] if courier_events else None

    raw_status = ((tx['tracking_api_status'] or tx['status'] or '') + '').upper()
    if tx['status'] == TX_RELEASED:
        stage_index = 6
    elif tx['status'] == TX_DELIVERED or 'DELIVERED' in raw_status:
        stage_index = 5
    elif 'OUT_FOR_DELIVERY' in raw_status or 'OUT FOR DELIVERY' in raw_status:
        stage_index = 4
    elif 'TRANSIT' in raw_status or 'IN_TRANSIT' in raw_status:
        stage_index = 3
    elif 'ACCEPTED' in raw_status or 'PICKUP' in raw_status:
        stage_index = 2
    elif tx['tracking_api_status'] and tx['tracking_api_status'] not in ('CHECK_FAILED', 'UNKNOWN'):
        stage_index = 1
    elif tx['tracking_number']:
        stage_index = 0
    else:
        stage_index = 0

    base_steps = [
        ('Tracking submitted', 'Seller tracking number has been received by Bazont.'),
        ('Tracking validated', 'Bazont has checked or is checking the courier tracking record.'),
        ('Accepted by courier', 'Courier has accepted the parcel into its network.'),
        ('In transit', 'Parcel is moving through the courier network.'),
        ('Out for delivery', 'Parcel is on the final delivery run.'),
        ('Delivered', 'Courier confirms the item has been delivered.'),
        ('Payment released', 'Bazont releases payment after courier-confirmed delivery.'),
    ]
    status_steps = []
    for idx, (title, body) in enumerate(base_steps):
        if idx < stage_index:
            state = 'done'
        elif idx == stage_index:
            state = 'active'
        else:
            state = 'pending'
        status_steps.append({'title': title, 'body': body, 'state': state})

    current_stage_label = base_steps[min(stage_index, len(base_steps)-1)][0]
    return render_template('courier_status.html', tx=tx, courier_events=courier_events, last_event=last_event,
                           status_steps=status_steps, current_stage_label=current_stage_label)


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


@app.route('/admin/back-office')
def admin_backoffice():
    """Inspection-only back-office view. Not linked into buyer/seller public flow."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            t.*,
            buyer.email AS buyer_email,
            seller.email AS joined_seller_email,
            (
                SELECT ce.status
                FROM courier_events ce
                WHERE ce.transaction_id = t.id
                ORDER BY ce.event_time DESC, ce.id DESC
                LIMIT 1
            ) AS latest_delivery_status
        FROM transactions t
        LEFT JOIN users buyer ON buyer.id = t.buyer_user_id
        LEFT JOIN users seller ON seller.id = t.seller_user_id
        ORDER BY t.created_at DESC, t.id DESC
        """
    ).fetchall()
    transactions = []
    for row in rows:
        tx = dict(row)
        tx['status'] = canonical_status(tx['status'])
        tx['payment_status'] = admin_payment_status(tx)
        tx['amount_held'] = tx_financials(tx)['platform_holds']
        tx['eligibility_flag'] = transaction_review_flag(tx, tx.get('latest_delivery_status'))
        transactions.append(tx)
    return render_template('admin_backoffice.html', transactions=transactions)



# Bazont23Q: STRICT F_B_C_v26 CANONICAL ROUTE/TEMPLATE INVENTORY + AUDIT ACCESS MODE.
# Single authoritative user-facing page map. Home audit panel and audit routes are based on this map.
MASTER_PAGE_MAP = [
    {'number':'1', 'title':'Home', 'route':'/', 'endpoint':'gateway_home', 'template':'page0.html', 'protected':False, 'audit_kind':'public'},
    {'number':'2', 'title':'Why Bazont Exists', 'route':'/page1', 'endpoint':'page1_intro', 'template':'templates/page1_intro.html', 'protected':False, 'audit_kind':'public'},
    {'number':'3', 'title':'Rules That Protect Both Sides', 'route':'/intro', 'endpoint':'intro_page', 'template':'intro.html', 'protected':False, 'audit_kind':'public'},
    {'number':'4', 'title':'Buyer Creates Transaction', 'route':'/animation/?step=1', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'5', 'title':'Seller Joins Transaction', 'route':'/animation/?step=2', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'6', 'title':'Buyer Makes Payment', 'route':'/animation/?step=3', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'7', 'title':'Seller Ships Item', 'route':'/animation/?step=4', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'8', 'title':'Courier Confirms Delivery', 'route':'/animation/?step=5', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'9', 'title':'Platform Releases Payment', 'route':'/animation/?step=6', 'endpoint':'animation_index', 'template':'animation/index.html', 'protected':False, 'audit_kind':'public'},
    {'number':'10', 'title':'Register', 'route':'/register', 'endpoint':'register', 'template':'templates/register.html', 'protected':False, 'audit_kind':'public'},
    {'number':'11', 'title':'Login', 'route':'/login', 'endpoint':'login', 'template':'templates/login.html', 'protected':False, 'audit_kind':'public'},
    {'number':'12', 'title':'Role', 'route':'/role-select', 'endpoint':'role_select', 'template':'templates/role_select.html', 'protected':True, 'audit_kind':'buyer'},
    {'number':'13', 'title':'Dashboard', 'route':'/buyer/dashboard', 'endpoint':'buyer_dashboard', 'template':'templates/buyer_dashboard.html', 'protected':True, 'audit_kind':'buyer'},
    {'number':'14', 'title':'Create', 'route':'/buyer/transactions/new', 'endpoint':'new_transaction', 'template':'templates/new_transaction.html', 'protected':True, 'audit_kind':'buyer'},
    {'number':'15', 'title':'Pay', 'route':'/transactions/<public_id>/buyer-actions', 'endpoint':'buyer_actions', 'template':'templates/buyer_actions.html', 'protected':True, 'audit_kind':'tx_buyer'},
    {'number':'16', 'title':'Invite', 'route':'/buyer/transactions/<public_id>/invitation-preview', 'endpoint':'invitation_preview', 'template':'templates/invitation_preview.html', 'protected':True, 'audit_kind':'tx_paid_buyer'},
    {'number':'17', 'title':'Courier', 'route':'/transactions/<public_id>/courier', 'endpoint':'courier_logs', 'template':'templates/courier_logs.html', 'protected':True, 'audit_kind':'tx_seller_tracking'},
    {'number':'18', 'title':'Seller', 'route':'/seller/join/<token>', 'endpoint':'seller_join', 'template':'templates/seller_join.html', 'protected':False, 'audit_kind':'seller_join'},
    {'number':'19', 'title':'Transaction Detail', 'route':'/transactions/<public_id>', 'endpoint':'transaction_detail', 'template':'templates/transaction_detail.html', 'protected':True, 'audit_kind':'tx_buyer'},
    {'number':'20', 'title':'Invitation Email Preview', 'route':'/buyer/transactions/<public_id>/invitation-email-preview', 'endpoint':'invitation_email_preview', 'template':'generated html', 'protected':True, 'audit_kind':'tx_paid_buyer'},
    {'number':'21', 'title':'Status', 'route':'/transactions/<public_id>/status', 'endpoint':'courier_status', 'template':'templates/courier_status.html', 'protected':True, 'audit_kind':'tx_buyer_tracking'},
    {'number':'22', 'title':'Buyer Transactions', 'route':'/buyer/transactions', 'endpoint':'buyer_transactions', 'template':'templates/buyer_transactions.html', 'protected':True, 'audit_kind':'buyer'},
    {'number':'23', 'title':'Seller Dashboard', 'route':'/seller/dashboard', 'endpoint':'seller_dashboard', 'template':'templates/seller_dashboard.html', 'protected':True, 'audit_kind':'seller'},
    {'number':'24', 'title':'FAQ', 'route':'/faq', 'endpoint':'faq', 'template':'templates/faq.html', 'protected':False, 'audit_kind':'public'},
    {'number':'25', 'title':'Index', 'route':'/index', 'endpoint':'index_page', 'template':'index_page.html', 'protected':False, 'audit_kind':'public'},
    {'number':'26', 'title':'Back Office', 'route':'/admin/back-office', 'endpoint':'admin_backoffice', 'template':'templates/admin_backoffice.html', 'protected':False, 'audit_kind':'public'},
]
MASTER_PAGE_BY_NUMBER = {row['number']: row for row in MASTER_PAGE_MAP}


def audit_login_as(role='buyer', allowed_endpoint=None):
    conn = get_db()
    buyer, seller = ensure_demo_users(conn)
    user = seller if role == 'seller' else buyer
    establish_authenticated_session(user)
    session['audit_access_mode'] = True
    if allowed_endpoint:
        session['audit_allowed_endpoint'] = allowed_endpoint
    return user


def audit_transaction_for(kind='tx_buyer', allowed_endpoint=None):
    conn = get_db()
    buyer, seller = ensure_demo_users(conn)
    audit_seller = get_or_create_user(conn, 'seller.audit@bazont.local', 'seller')
    role = 'seller' if 'seller' in kind else 'buyer'
    paid = kind in ('tx_paid_buyer', 'tx_seller_tracking', 'tx_buyer_tracking')
    assign_seller = kind in ('tx_seller_tracking', 'tx_buyer_tracking') or role == 'seller'
    tracking = kind in ('tx_seller_tracking', 'tx_buyer_tracking')
    tx = get_or_create_audit_transaction(conn, buyer=buyer, paid=paid, assign_seller=assign_seller, tracking=tracking, audit_key=kind)
    establish_authenticated_session(audit_seller if role == 'seller' else buyer)
    session['audit_access_mode'] = True
    if allowed_endpoint:
        session['audit_allowed_endpoint'] = allowed_endpoint
    return tx


def audit_destination_for(page):
    kind = page['audit_kind']
    endpoint = page['endpoint']
    if kind == 'public':
        return redirect(page['route'])
    if kind == 'buyer':
        audit_login_as('buyer', endpoint)
        return redirect(url_for(endpoint))
    if kind == 'seller':
        audit_login_as('seller', endpoint)
        return redirect(url_for(endpoint))
    if kind == 'seller_join':
        conn = get_db()
        buyer, _seller = ensure_demo_users(conn)
        tx = get_or_create_audit_transaction(conn, buyer=buyer, paid=True, assign_seller=False, tracking=False)
        clear_authenticated_session()
        session['audit_access_mode'] = True
        session['audit_allowed_endpoint'] = 'seller_join'
        return redirect(url_for('seller_join', token=tx['invite_token']))
    if kind.startswith('tx_'):
        tx = audit_transaction_for(kind, endpoint)
        if endpoint in ('buyer_actions', 'transaction_detail', 'courier_logs', 'courier_status', 'invitation_email_preview'):
            return redirect(url_for(endpoint, public_id=tx['public_id']))
        if endpoint == 'invitation_preview':
            return redirect(url_for('invitation_preview', public_id=tx['public_id']))
    abort(404)


@app.route('/audit/p/<page_no>')
def audit_master_page(page_no):
    page = MASTER_PAGE_BY_NUMBER.get(str(page_no))
    if not page:
        abort(404)
    return audit_destination_for(page)

# Bazont24K: audit inspection entry is deliberately limited to /audit/p/<page_no>.
@app.route('/audit/page<int:page_no>')
def audit_page_legacy(page_no):
    abort(404)

@app.route('/audit-map.json')
def audit_map_json():
    return {'pages': MASTER_PAGE_MAP, 'version': get_version()}

@app.route('/__version')
def version_route():
    return {'version': get_version()}


def build_audit_panel_html():
    status_for = lambda page: 'PROTECTED' if page.get('protected') else 'ALIVE'
    cls_for = lambda page: 'protected' if page.get('protected') else 'alive'
    rows = []
    for page in MASTER_PAGE_MAP:
        route_label = html.escape(page['route'])
        title = html.escape(page['title'])
        number = html.escape(str(page['number']))
        status = status_for(page)
        cls = cls_for(page)
        rows.append(f'<a class="audit-link {cls}" href="/audit/p/{number}">{number} {title} • {status} • {route_label}</a>')
    return '\n          '.join(rows)


def render_gateway_home_page():
    page_path = BASE_DIR / 'page0.html'
    content = page_path.read_text(encoding='utf-8')
    version = get_version()
    content = re.sub(r'Bazont2[34][A-Z]\.zip', version, content)
    return content


def render_index_page():
    page_path = BASE_DIR / 'index_page.html'
    content = page_path.read_text(encoding='utf-8')
    version = get_version()
    content = re.sub(r'Bazont2[34][A-Z]\.zip', version, content)
    content = content.replace('<!-- AUDIT_PANEL_ROWS -->', build_audit_panel_html())
    return content


@app.route("/faq")
def faq():
    return render_template("faq.html")


if __name__ == '__main__':
    init_db()
    thread = threading.Thread(target=background_rule_loop, daemon=True)
    thread.start()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)

