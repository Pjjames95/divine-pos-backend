"""
Beauty Shop POS - Central API Server
Handles all data operations (products, sales, users, settings)
and M-Pesa STK Push / callback processing.
"""

import os
import sys
import uuid
import json
import base64
import signal
import logging
import threading
import traceback
import sqlite3
import time
from datetime import datetime, timedelta

import bcrypt
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from decouple import config
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== Logging ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== App & Config ==========
app = Flask(__name__)
CORS(app)

DATABASE_PATH      = os.path.join(os.path.dirname(__file__), 'beauty_shop_central.db')
DATABASE_TIMEOUT   = 30
db_lock            = threading.Lock()

MPESA_CONSUMER_KEY    = config('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = config('MPESA_CONSUMER_SECRET')
MPESA_PASSKEY         = config('MPESA_PASSKEY')
MPESA_SHORTCODE       = config('MPESA_SHORTCODE', default='174379')
MPESA_ENVIRONMENT     = config('MPESA_ENVIRONMENT', default='sandbox')
CALLBACK_BASE_URL     = config('CALLBACK_BASE_URL', default='https://divine-pos-backend.onrender.com')

# ========== Graceful Shutdown ==========
def signal_handler(sig, frame):
    print('\nShutting down server gracefully...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ========== Database Helpers ==========
def execute_query(query, params=None, fetch=False):
    """Execute a database query safely with lock and timeout."""
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            if fetch:
                result = [dict(row) for row in cursor.fetchall()]
            else:
                result = cursor.lastrowid

            conn.commit()
            return result
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()


def init_db():
    """Create all tables and seed default data."""
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        cursor = conn.cursor()

        # ----- Core POS Tables -----
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            pin_hash TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'cashier',
            phone TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            brand TEXT,
            sku TEXT UNIQUE,
            barcode TEXT,
            category TEXT,
            price REAL,
            cost_price REAL,
            quantity INTEGER DEFAULT 0,
            min_stock_level INTEGER DEFAULT 5,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY,
            receipt_number TEXT UNIQUE,
            total_amount REAL,
            payment_method TEXT,
            payment_status TEXT DEFAULT 'completed',
            mpesa_checkout_request_id TEXT,
            mpesa_receipt_number TEXT,
            cash_tendered REAL,
            change_amount REAL,
            cashier_id TEXT,
            customer_phone TEXT,
            is_void INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS sale_items (
            id TEXT PRIMARY KEY,
            sale_id TEXT,
            product_id TEXT,
            product_name TEXT,
            quantity INTEGER,
            unit_price REAL,
            total_price REAL
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ----- M-Pesa Tables -----
        cursor.execute('''CREATE TABLE IF NOT EXISTS mpesa_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkout_request_id TEXT UNIQUE,
            merchant_request_id TEXT,
            shop_id TEXT,
            phone_number TEXT,
            amount REAL,
            account_reference TEXT,
            transaction_desc TEXT,
            status TEXT DEFAULT 'pending',
            result_code INTEGER,
            result_description TEXT,
            mpesa_receipt_number TEXT,
            transaction_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            callback_received_at TIMESTAMP,
            synced_to_shop BOOLEAN DEFAULT FALSE
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS access_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS registered_shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT UNIQUE,
            shop_name TEXT,
            contact_phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ----- Seed Default Admin -----
        cursor.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if cursor.fetchone()[0] == 0:
            pw  = bcrypt.hashpw(b'admin123', bcrypt.gensalt())
            pin = bcrypt.hashpw(b'1234',     bcrypt.gensalt())
            cursor.execute(
                '''INSERT INTO users (id, username, password_hash, pin_hash, full_name, role)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (str(uuid.uuid4()), 'admin', pw, pin, 'Administrator', 'admin')
            )

        # ----- Seed Sample Products -----
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            products = [
                ('Matte Lipstick - Ruby Red',  'MAC Cosmetics', '1001', 'Makeup',    2500, 1800,  50),
                ('Hydrating Face Cream',        'Nivea',         '1002', 'Skincare',  1200,  800, 100),
                ('Argan Oil Hair Treatment',    'Garnier',       '1003', 'Hair Care', 1800, 1200,  75),
                ('Gel Nail Polish Set',         'NYX',           '1004', 'Nails',     3500, 2500,  30),
                ('Rose Garden Perfume',         'Revlon',        '1005', 'Fragrances',4500, 3200,  40),
                ('Shea Butter Body Lotion',     'Dove',          '1006', 'Bath & Body', 950,  650, 120),
                ('Makeup Brush Set',            'Maybelline',    '1007', 'Tools',     2800, 2000,  25),
                ('Vitamin C Serum',             'Olay',          '1008', 'Skincare',  3200, 2200,  45),
            ]
            for p in products:
                cursor.execute(
                    '''INSERT INTO products
                       (id, name, brand, barcode, category, price, cost_price, quantity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (str(uuid.uuid4()), *p)
                )

        # ----- Seed Default Settings -----
        cursor.execute("SELECT COUNT(*) FROM settings")
        if cursor.fetchone()[0] == 0:
            for k, v in [
                ('shop_name',    'Divine Beauty & Cosmetics Shop'),
                ('shop_address', 'Nairobi, Kenya'),
                ('shop_phone',   '0700000000'),
                ('tax_rate',     '16'),
                ('currency',     'KES'),
            ]:
                cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))

        conn.commit()
        conn.close()
    logger.info("Database initialised successfully")


# ========== M-Pesa Helper ==========
class MpesaAPI:
    def __init__(self):
        self.base_url = (
            'https://sandbox.safaricom.co.ke'
            if MPESA_ENVIRONMENT == 'sandbox'
            else 'https://api.safaricom.co.ke'
        )

        self.session = requests.Session()
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1)))
        self.session.headers.update({'Connection': 'close'})

    def get_access_token(self):
        """Return a valid OAuth token, using the DB cache when possible."""
        try:
            rows = execute_query(
                'SELECT token FROM access_tokens WHERE expires_at > ? ORDER BY id DESC LIMIT 1',
                (datetime.now(),), fetch=True
            )
            if rows:
                logger.info("Using cached access token")
                return rows[0]['token']
        except Exception as e:
            logger.error(f"Error checking cached token: {e}")

        logger.info("Fetching new access token from Safaricom")
        auth_string = base64.b64encode(
            f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
        ).decode()
        response = self.session.get(
            f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
            headers={'Authorization': f'Basic {auth_string}'},
            timeout=10
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get access token: {response.text}")

        data       = response.json()
        token      = data['access_token']
        expires_in = int(data.get('expires_in', 3599))
        try:
            execute_query(
                'INSERT INTO access_tokens (token, expires_at) VALUES (?, ?)',
                (token, datetime.now() + timedelta(seconds=expires_in - 60))
            )
        except Exception as e:
            logger.error(f"Failed to cache token: {e}")
        return token

    @staticmethod
    def format_phone(phone):
        """Normalise phone to 2547XXXXXXXX."""
        phone = phone.strip().replace('+', '').replace(' ', '')
        if phone.startswith('0'):
            return '254' + phone[1:]
        if not phone.startswith('254'):
            return '254' + phone
        return phone

    def stk_push(self, phone_number, amount, account_ref, description="Beauty Shop Payment"):
        try:
            token     = self.get_access_token()
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password  = base64.b64encode(
                f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
            ).decode()

            payload = {
                "BusinessShortCode": MPESA_SHORTCODE,
                "Password":          password,
                "Timestamp":         timestamp,
                "TransactionType":   "CustomerPayBillOnline",
                "Amount":            int(amount),
                "PartyA":            self.format_phone(phone_number),
                "PartyB":            MPESA_SHORTCODE,
                "PhoneNumber":       self.format_phone(phone_number),
                "CallBackURL":       f"{CALLBACK_BASE_URL}/api/mpesa/callback",
                "AccountReference":  (account_ref or "BeautyShop")[:12],
                "TransactionDesc":   description[:13],
            }
            logger.info(f"STK Push Payload: {json.dumps(payload, indent=2)}")

            response = self.session.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={'Authorization': f'Bearer {token}'},
                timeout=30
            )
            logger.info(f"STK Response [{response.status_code}]: {response.text}")
            result = response.json()
            return {
                'success':              result.get('ResponseCode') == '0',
                'checkout_request_id':  result.get('CheckoutRequestID'),
                'merchant_request_id':  result.get('MerchantRequestID'),
                'response_code':        result.get('ResponseCode'),
                'response_description': result.get('ResponseDescription'),
                'customer_message':     result.get('CustomerMessage'),
            }
        except Exception as e:
            logger.error(f"STK Push error: {traceback.format_exc()}")
            return {'success': False, 'error': str(e)}

    def query_status(self, checkout_request_id):
        try:
            token     = self.get_access_token()
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password  = base64.b64encode(
                f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
            ).decode()
            response = self.session.post(
                f"{self.base_url}/mpesa/stkpushquery/v1/query",
                json={
                    "BusinessShortCode": MPESA_SHORTCODE,
                    "Password":          password,
                    "Timestamp":         timestamp,
                    "CheckoutRequestID": checkout_request_id,
                },
                headers={'Authorization': f'Bearer {token}'},
                timeout=10
            )
            return response.json()
        except Exception as e:
            logger.error(f"Status query error: {e}")
            return {'error': str(e)}


mpesa_api = MpesaAPI()


# ========== Health Check ==========
@app.route('/')
def index():
    return jsonify({
        'service':     'Beauty Shop POS - Central API & M-Pesa Server',
        'status':      'running',
        'environment': MPESA_ENVIRONMENT,
        'timestamp':   datetime.now().isoformat(),
    })


# ========== Auth Endpoints ==========
@app.route('/api/auth/login', methods=['POST'])
def login():
    data     = request.json
    username = data.get('username', '')
    password = data.get('password', '')

    users = execute_query(
        "SELECT * FROM users WHERE username=? AND is_active=1", (username,), fetch=True
    )
    if users and bcrypt.checkpw(password.encode(), users[0]['password_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        u = users[0]
        return jsonify({'success': True, 'user': {
            'id': u['id'], 'username': u['username'],
            'fullName': u['full_name'], 'role': u['role'], 'phone': u['phone'],
        }})
    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401


@app.route('/api/auth/login-pin', methods=['POST'])
def login_pin():
    data     = request.json
    username = data.get('username', '')
    pin      = data.get('pin', '')

    users = execute_query(
        "SELECT * FROM users WHERE username=? AND is_active=1", (username,), fetch=True
    )
    if users and users[0]['pin_hash'] and bcrypt.checkpw(pin.encode(), users[0]['pin_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        u = users[0]
        return jsonify({'success': True, 'user': {
            'id': u['id'], 'username': u['username'],
            'fullName': u['full_name'], 'role': u['role'], 'phone': u['phone'],
        }})
    return jsonify({'success': False, 'error': 'Invalid PIN'}), 401


# ========== Product Endpoints ==========
@app.route('/api/products', methods=['GET'])
def get_products():
    search   = request.args.get('search', '')
    category = request.args.get('category', '')
    query    = "SELECT * FROM products WHERE is_active=1"
    params   = []
    if search:
        query += " AND (name LIKE ? OR brand LIKE ? OR barcode LIKE ?)"
        params.extend([f'%{search}%'] * 3)
    if category:
        query += " AND category=?"
        params.append(category)
    query += " ORDER BY name"
    return jsonify(execute_query(query, params or None, fetch=True))


@app.route('/api/products', methods=['POST'])
def add_product():
    data       = request.json
    product_id = str(uuid.uuid4())
    execute_query(
        '''INSERT INTO products
           (id, name, brand, sku, barcode, category, price, cost_price, quantity, min_stock_level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (product_id, data['name'], data.get('brand'), data.get('sku'), data.get('barcode'),
         data.get('category'), data['price'], data.get('cost_price', 0),
         data.get('quantity', 0), data.get('min_stock_level', 5))
    )
    return jsonify({'success': True, 'id': product_id})


@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    data = request.json
    execute_query(
        '''UPDATE products
           SET name=?, brand=?, category=?, price=?, cost_price=?,
               quantity=?, min_stock_level=?, updated_at=?
           WHERE id=?''',
        (data['name'], data.get('brand'), data.get('category'), data['price'],
         data.get('cost_price', 0), data.get('quantity', 0),
         data.get('min_stock_level', 5), datetime.now(), product_id)
    )
    return jsonify({'success': True})


# ========== Sales Endpoints ==========
@app.route('/api/sales', methods=['POST'])
def create_sale():
    data    = request.json
    sale_id = str(uuid.uuid4())
    receipt = f"GBS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    execute_query(
        '''INSERT INTO sales
           (id, receipt_number, total_amount, payment_method, payment_status,
            cash_tendered, change_amount, cashier_id, customer_phone)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (sale_id, receipt, data['total_amount'], data['payment_method'],
         data.get('payment_status', 'completed'), data.get('cash_tendered'),
         data.get('change_amount'), data.get('cashier_id'), data.get('customer_phone'))
    )

    for item in data.get('items', []):
        execute_query(
            '''INSERT INTO sale_items
               (id, sale_id, product_id, product_name, quantity, unit_price, total_price)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (str(uuid.uuid4()), sale_id, item['product_id'], item['product_name'],
             item['quantity'], item['unit_price'], item['total_price'])
        )
        execute_query(
            "UPDATE products SET quantity=quantity-?, updated_at=? WHERE id=?",
            (item['quantity'], datetime.now(), item['product_id'])
        )

    return jsonify({'success': True, 'sale_id': sale_id, 'receipt_number': receipt})


@app.route('/api/sales', methods=['GET'])
def get_sales():
    date = request.args.get('date')
    try:
        if date:
            sales_raw = execute_query(
                "SELECT * FROM sales WHERE created_at BETWEEN ? AND ? AND is_void=0 ORDER BY created_at DESC",
                (f"{date} 00:00:00", f"{date} 23:59:59"), fetch=True
            )
        else:
            sales_raw = execute_query(
                "SELECT * FROM sales WHERE is_void=0 ORDER BY created_at DESC LIMIT 200",
                fetch=True
            )
        for sale in sales_raw:
            sale['items'] = execute_query(
                "SELECT * FROM sale_items WHERE sale_id=?", (sale['id'],), fetch=True
            )
        return jsonify(sales_raw)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reports/daily', methods=['GET'])
def daily_report():
    date    = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    summary = execute_query(
        '''SELECT COUNT(*) as transactions,
                  COALESCE(SUM(total_amount), 0) as total_sales,
                  COALESCE(SUM(CASE WHEN payment_method='cash'  THEN total_amount ELSE 0 END), 0) as cash_sales,
                  COALESCE(SUM(CASE WHEN payment_method='mpesa' THEN total_amount ELSE 0 END), 0) as mpesa_sales
           FROM sales
           WHERE created_at BETWEEN ? AND ? AND is_void=0 AND payment_status='completed' ''',
        (f"{date} 00:00:00", f"{date} 23:59:59"), fetch=True
    )
    return jsonify({'date': date, 'summary': summary[0] if summary else {}})


# ========== User Endpoints ==========
@app.route('/api/users', methods=['GET'])
def get_users():
    return jsonify(execute_query(
        "SELECT id, username, full_name, role, phone, is_active, last_login FROM users",
        fetch=True
    ))


@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    pw   = bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt())
    pin  = bcrypt.hashpw(data['pin'].encode(), bcrypt.gensalt()) if data.get('pin') else None
    execute_query(
        '''INSERT INTO users (id, username, password_hash, pin_hash, full_name, role, phone)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (str(uuid.uuid4()), data['username'], pw, pin,
         data['full_name'], data.get('role', 'cashier'), data.get('phone'))
    )
    return jsonify({'success': True})


@app.route('/api/users/<user_id>/password', methods=['PUT'])
def change_password(user_id):
    data = request.json
    pw   = bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt())
    execute_query("UPDATE users SET password_hash=? WHERE id=?", (pw, user_id))
    return jsonify({'success': True})


# ========== Settings Endpoints ==========
@app.route('/api/settings', methods=['GET'])
def get_settings():
    rows = execute_query("SELECT * FROM settings", fetch=True)
    return jsonify({r['key']: r['value'] for r in rows})


@app.route('/api/settings', methods=['PUT'])
def update_settings():
    for key, value in request.json.items():
        execute_query(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now())
        )
    return jsonify({'success': True})


# ========== Sync Endpoint ==========
@app.route('/api/sync', methods=['POST'])
def sync_data():
    last_sync = request.json.get('last_sync', '2000-01-01 00:00:00')
    products  = execute_query("SELECT * FROM products WHERE updated_at > ?", (last_sync,), fetch=True)
    sales     = execute_query("SELECT * FROM sales WHERE created_at > ?",   (last_sync,), fetch=True)
    users     = execute_query(
        "SELECT id, username, full_name, role, phone, is_active FROM users", fetch=True
    )
    settings  = execute_query("SELECT * FROM settings", fetch=True)
    return jsonify({
        'products':  products,
        'sales':     sales,
        'users':     users,
        'settings':  {s['key']: s['value'] for s in settings},
        'sync_time': datetime.now().isoformat(),
    })


# ========== M-Pesa Endpoints ==========
@app.route('/api/mpesa/stkpush', methods=['POST'])
def stk_push():
    try:
        data        = request.json
        phone       = data.get('phone_number')
        amount      = data.get('amount')
        shop_id     = data.get('shop_id', 'default')
        account_ref = data.get('account_reference', f'Beauty{datetime.now().strftime("%H%M%S")}')
        description = data.get('description', 'Beauty Shop Payment')

        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400
        if not amount or float(amount) <= 0:
            return jsonify({'error': 'Valid amount is required'}), 400

        result = mpesa_api.stk_push(phone, amount, account_ref, description)

        if result['success']:
            try:
                execute_query(
                    '''INSERT INTO mpesa_transactions
                       (checkout_request_id, merchant_request_id, shop_id, phone_number,
                        amount, account_reference, transaction_desc, status, transaction_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
                    (result['checkout_request_id'], result['merchant_request_id'],
                     shop_id, phone, amount, account_ref, description, datetime.now())
                )
            except Exception as e:
                logger.error(f"Failed to save transaction: {e}")

        return jsonify(result)
    except Exception as e:
        logger.error(f"STK Push endpoint error: {e}")
        return jsonify({'error': str(e), 'success': False}), 500


@app.route('/api/mpesa/callback', methods=['POST'])
def mpesa_callback():
    try:
        stk_callback       = request.json.get('Body', {}).get('stkCallback', {})
        checkout_request_id = stk_callback.get('CheckoutRequestID')
        result_code        = stk_callback.get('ResultCode')
        result_desc        = stk_callback.get('ResultDesc')
        logger.info(f"Callback received for {checkout_request_id}: code={result_code}")

        if not checkout_request_id:
            return jsonify({'ResultCode': 1, 'ResultDesc': 'Missing CheckoutRequestID'})

        if result_code == 0:
            metadata   = stk_callback.get('CallbackMetadata', {}).get('Item', [])
            mpesa_ref  = next((i.get('Value') for i in metadata if i['Name'] == 'MpesaReceiptNumber'), None)
            execute_query(
                '''UPDATE mpesa_transactions
                   SET status='completed', result_code=?, result_description=?,
                       mpesa_receipt_number=?, callback_received_at=?
                   WHERE checkout_request_id=?''',
                (result_code, result_desc, mpesa_ref, datetime.now(), checkout_request_id)
            )
            logger.info(f"Payment completed: {checkout_request_id} — {mpesa_ref}")
        else:
            execute_query(
                '''UPDATE mpesa_transactions
                   SET status='failed', result_code=?, result_description=?,
                       callback_received_at=?
                   WHERE checkout_request_id=?''',
                (result_code, result_desc, datetime.now(), checkout_request_id)
            )
            logger.info(f"Payment failed: {checkout_request_id} — {result_desc}")

        return jsonify({'ResultCode': 0, 'ResultDesc': 'Success'})
    except Exception as e:
        logger.error(f"Callback error: {e}")
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Internal error'})


@app.route('/api/mpesa/status/<checkout_request_id>', methods=['GET'])
def check_status(checkout_request_id):
    try:
        rows = execute_query(
            'SELECT * FROM mpesa_transactions WHERE checkout_request_id=?',
            (checkout_request_id,), fetch=True
        )
        if rows:
            t = rows[0]
            if t.get('callback_received_at') and t.get('status') != 'pending':
                return jsonify({
                    'status':               t['status'],
                    'result_code':          t.get('result_code'),
                    'mpesa_receipt_number': t.get('mpesa_receipt_number'),
                    'amount':               t.get('amount'),
                })

        status_result = mpesa_api.query_status(checkout_request_id)
        result_code   = status_result.get('ResultCode')
        result_desc   = status_result.get('ResultDesc', '')

        if result_code is not None:
            rc = str(result_code)
            if rc == '0':
                execute_query(
                    '''UPDATE mpesa_transactions
                       SET status='completed', result_code=?, result_description=?,
                           mpesa_receipt_number=COALESCE(mpesa_receipt_number, ?),
                           callback_received_at=COALESCE(callback_received_at, ?)
                       WHERE checkout_request_id=?''',
                    (int(result_code), result_desc,
                     f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                     datetime.now(), checkout_request_id)
                )
                return jsonify({'status': 'completed', 'result_code': 0, 'result_description': result_desc})
            elif rc in ('1', '1032'):
                status = 'cancelled' if rc == '1032' else 'failed'
                execute_query(
                    '''UPDATE mpesa_transactions
                       SET status=?, result_code=?, result_description=?,
                           callback_received_at=COALESCE(callback_received_at, ?)
                       WHERE checkout_request_id=?''',
                    (status, int(result_code), result_desc, datetime.now(), checkout_request_id)
                )
                return jsonify({'status': status, 'result_code': int(result_code), 'result_description': result_desc})

        return jsonify({'status': 'pending'})
    except Exception as e:
        logger.error(f"Status check error: {e}")
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/api/mpesa/pending/<shop_id>', methods=['GET'])
def get_pending_transactions(shop_id):
    try:
        rows = execute_query(
            '''SELECT * FROM mpesa_transactions
               WHERE shop_id=? AND synced_to_shop=FALSE AND status='completed' ''',
            (shop_id,), fetch=True
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mpesa/mark_synced', methods=['POST'])
def mark_as_synced():
    checkout_request_id = request.json.get('checkout_request_id')
    if not checkout_request_id:
        return jsonify({'error': 'Missing checkout_request_id'}), 400
    execute_query(
        'UPDATE mpesa_transactions SET synced_to_shop=TRUE WHERE checkout_request_id=?',
        (checkout_request_id,)
    )
    return jsonify({'success': True})


@app.route('/api/register-shop', methods=['POST'])
def register_shop():
    try:
        data         = request.json
        shop_id      = data.get('shop_id')
        shop_name    = data.get('shop_name', 'Beauty Shop')
        contact_phone = data.get('contact_phone')
        if not shop_id:
            return jsonify({'error': 'shop_id is required'}), 400
        execute_query(
            'INSERT OR REPLACE INTO registered_shops (shop_id, shop_name, contact_phone) VALUES (?, ?, ?)',
            (shop_id, shop_name, contact_phone)
        )
        return jsonify({'success': True, 'shop_id': shop_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ========== Startup ==========
init_db()

if __name__ == '__main__':
    import socket
    sock   = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    in_use = sock.connect_ex(('127.0.0.1', 5000)) == 0
    sock.close()
    if in_use:
        print("Port 5000 is already in use! Stop the existing server first: fuser -k 5000/tcp")
        sys.exit(1)

    print("=" * 60)
    print("Beauty Shop POS — Central API & M-Pesa Server")
    print(f"Callback base URL : {CALLBACK_BASE_URL}")
    print(f"M-Pesa environment: {MPESA_ENVIRONMENT}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
