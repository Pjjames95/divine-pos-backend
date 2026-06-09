"""
Central API Server for Divine Beauty & Cosmetics POS
Handles all data + M-Pesa operations for both desktop and mobile apps
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
import uuid
import bcrypt
import json
import base64
import threading
import requests
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'beauty_shop_central.db')
db_lock = threading.Lock()

# M-Pesa credentials
MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET')
MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY')
MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE', '174379')
MPESA_ENVIRONMENT = os.environ.get('MPESA_ENVIRONMENT', 'sandbox')

# In-memory token cache
_cached_token = None
_token_expires_at = None
_token_lock = threading.Lock()

# ========== Database Setup ==========
def init_db():
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
            pin_hash TEXT, full_name TEXT, role TEXT DEFAULT 'cashier',
            phone TEXT, is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_login TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT, brand TEXT, sku TEXT UNIQUE,
            barcode TEXT, category TEXT, price REAL, cost_price REAL,
            quantity INTEGER DEFAULT 0, min_stock_level INTEGER DEFAULT 5,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY, receipt_number TEXT UNIQUE,
            total_amount REAL, payment_method TEXT,
            payment_status TEXT DEFAULT 'completed',
            mpesa_checkout_request_id TEXT, mpesa_receipt_number TEXT,
            cash_tendered REAL, change_amount REAL,
            cashier_id TEXT, customer_phone TEXT,
            is_void INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS sale_items (
            id TEXT PRIMARY KEY, sale_id TEXT, product_id TEXT,
            product_name TEXT, quantity INTEGER,
            unit_price REAL, total_price REAL)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Create admin user if not exists
        cursor.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if cursor.fetchone()[0] == 0:
            pw = bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt())
            pin = bcrypt.hashpw('1234'.encode(), bcrypt.gensalt())
            cursor.execute('''INSERT INTO users (id, username, password_hash, pin_hash, full_name, role)
                VALUES (?, ?, ?, ?, ?, ?)''', (str(uuid.uuid4()), 'admin', pw, pin, 'Administrator', 'admin'))
        
        # Default settings
        cursor.execute("SELECT COUNT(*) FROM settings")
        if cursor.fetchone()[0] == 0:
            defaults = [('shop_name', 'Divine Beauty & Cosmetics Shop'), ('shop_address', 'Nairobi, Kenya'),
                       ('shop_phone', '0700000000'), ('tax_rate', '16'), ('currency', 'KES')]
            for k, v in defaults:
                cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
        
        conn.commit()
        conn.close()

def execute_query(query, params=None, fetch=False):
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            if params: cursor.execute(query, params)
            else: cursor.execute(query)
            if fetch: result = [dict(row) for row in cursor.fetchall()]
            else: result = cursor.lastrowid
            conn.commit()
            conn.close()
            return result
        except Exception as e:
            conn.rollback()
            conn.close()
            raise e

# ========== M-Pesa API Helper ==========
class MpesaAPI:
    def __init__(self):
        self.base_url = 'https://sandbox.safaricom.co.ke' if MPESA_ENVIRONMENT == 'sandbox' else 'https://api.safaricom.co.ke'
    
    def get_access_token(self):
        global _cached_token, _token_expires_at
        with _token_lock:
            if _cached_token and _token_expires_at and datetime.now() < _token_expires_at:
                return _cached_token
        
        auth_string = base64.b64encode(f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()).decode()
        response = requests.get(f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
            headers={'Authorization': f'Basic {auth_string}'}, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            token = data['access_token']
            expires_in = int(data.get('expires_in', 3599))
            with _token_lock:
                _cached_token = token
                _token_expires_at = datetime.now() + timedelta(seconds=expires_in - 60)
            return token
        raise Exception(f"Failed to get access token: {response.text}")
    
    def format_phone(self, phone):
        phone = phone.strip().replace('+', '').replace(' ', '')
        if phone.startswith('0'): return '254' + phone[1:]
        return phone
    
    def stk_push(self, phone_number, amount, account_ref, description="Payment"):
        token = self.get_access_token()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()).decode()
        
        payload = {
            "BusinessShortCode": MPESA_SHORTCODE, "Password": password, "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline", "Amount": int(amount),
            "PartyA": self.format_phone(phone_number), "PartyB": MPESA_SHORTCODE,
            "PhoneNumber": self.format_phone(phone_number),
            "CallBackURL": f"{request.host_url}api/mpesa/callback",
            "AccountReference": (account_ref or "Payment")[:12], "TransactionDesc": description[:13]
        }
        
        response = requests.post(f"{self.base_url}/mpesa/stkpush/v1/processrequest",
            json=payload, headers={'Authorization': f'Bearer {token}'}, timeout=30)
        result = response.json()
        
        return {
            'success': result.get('ResponseCode') == '0',
            'checkout_request_id': result.get('CheckoutRequestID'),
            'merchant_request_id': result.get('MerchantRequestID'),
            'response_code': result.get('ResponseCode'),
            'response_description': result.get('ResponseDescription'),
            'customer_message': result.get('CustomerMessage')
        }
    
    def query_status(self, checkout_request_id):
        token = self.get_access_token()
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()).decode()
        
        response = requests.post(f"{self.base_url}/mpesa/stkpushquery/v1/query",
            json={"BusinessShortCode": MPESA_SHORTCODE, "Password": password,
                  "Timestamp": timestamp, "CheckoutRequestID": checkout_request_id},
            headers={'Authorization': f'Bearer {token}'}, timeout=10)
        return response.json()

mpesa_api = MpesaAPI()

# ========== Root ==========
@app.route('/')
def index():
    return jsonify({'status': 'running', 'service': 'Divine Beauty POS API', 'timestamp': datetime.now().isoformat()})

# ========== AUTH ENDPOINTS ==========
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    users = execute_query("SELECT * FROM users WHERE username=? AND is_active=1", (data.get('username', ''),), fetch=True)
    if users and bcrypt.checkpw(data.get('password', '').encode(), users[0]['password_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        u = users[0]
        return jsonify({'success': True, 'user': {'id': u['id'], 'username': u['username'], 'fullName': u['full_name'], 'role': u['role'], 'phone': u['phone']}})
    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/auth/login-pin', methods=['POST'])
def login_pin():
    data = request.json
    users = execute_query("SELECT * FROM users WHERE username=? AND is_active=1", (data.get('username', ''),), fetch=True)
    if users and users[0]['pin_hash'] and bcrypt.checkpw(data.get('pin', '').encode(), users[0]['pin_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        u = users[0]
        return jsonify({'success': True, 'user': {'id': u['id'], 'username': u['username'], 'fullName': u['full_name'], 'role': u['role'], 'phone': u['phone']}})
    return jsonify({'success': False, 'error': 'Invalid PIN'}), 401

# ========== PRODUCTS ENDPOINTS ==========
@app.route('/api/products', methods=['GET'])
def get_products():
    search = request.args.get('search', '')
    category = request.args.get('category', '')
    query = "SELECT * FROM products WHERE is_active=1"
    params = []
    if search:
        query += " AND (name LIKE ? OR brand LIKE ? OR barcode LIKE ?)"
        params.extend([f'%{search}%'] * 3)
    if category:
        query += " AND category=?"
        params.append(category)
    query += " ORDER BY name"
    return jsonify(execute_query(query, params, fetch=True))

@app.route('/api/products', methods=['POST'])
def add_product():
    data = request.json
    pid = str(uuid.uuid4())
    execute_query('''INSERT INTO products (id, name, brand, sku, barcode, category, price, cost_price, quantity, min_stock_level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (pid, data['name'], data.get('brand'), data.get('sku'), data.get('barcode'),
         data.get('category'), data['price'], data.get('cost_price', 0), data.get('quantity', 0), data.get('min_stock_level', 5)))
    return jsonify({'success': True, 'id': pid})

@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    data = request.json
    execute_query('''UPDATE products SET name=?, brand=?, category=?, price=?, cost_price=?, quantity=?, min_stock_level=?, updated_at=? WHERE id=?''',
        (data['name'], data.get('brand'), data.get('category'), data['price'], data.get('cost_price', 0),
         data.get('quantity', 0), data.get('min_stock_level', 5), datetime.now(), product_id))
    return jsonify({'success': True})

# ========== SALES ENDPOINTS ==========
@app.route('/api/sales', methods=['POST'])
def create_sale():
    data = request.json
    sale_id = str(uuid.uuid4())
    receipt = f"GBS-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    execute_query('''INSERT INTO sales (id, receipt_number, total_amount, payment_method, payment_status,
        cash_tendered, change_amount, cashier_id, customer_phone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (sale_id, receipt, data['total_amount'], data['payment_method'], data.get('payment_status', 'completed'),
         data.get('cash_tendered'), data.get('change_amount'), data.get('cashier_id'), data.get('customer_phone')))
    
    for item in data.get('items', []):
        execute_query('''INSERT INTO sale_items (id, sale_id, product_id, product_name, quantity, unit_price, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (str(uuid.uuid4()), sale_id, item['product_id'], item['product_name'], item['quantity'], item['unit_price'], item['total_price']))
        execute_query("UPDATE products SET quantity=quantity-?, updated_at=? WHERE id=?", (item['quantity'], datetime.now(), item['product_id']))
    
    return jsonify({'success': True, 'sale_id': sale_id, 'receipt_number': receipt})

@app.route('/api/sales', methods=['GET'])
def get_sales():
    date = request.args.get('date', None)
    try:
        with db_lock:
            conn = sqlite3.connect(DATABASE_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if date:
                cursor.execute("SELECT * FROM sales WHERE created_at BETWEEN ? AND ? AND is_void=0 ORDER BY created_at DESC",
                    (f"{date} 00:00:00", f"{date} 23:59:59"))
            else:
                cursor.execute("SELECT * FROM sales WHERE is_void=0 ORDER BY created_at DESC LIMIT 200")
            
            sales = []
            for row in cursor.fetchall():
                sale = dict(row)
                cursor.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale['id'],))
                sale['items'] = [dict(item) for item in cursor.fetchall()]
                sales.append(sale)
            conn.close()
            return jsonify(sales)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== M-PESA ENDPOINTS ==========
@app.route('/api/mpesa/stkpush', methods=['POST'])
def mpesa_stk_push():
    try:
        data = request.json
        phone = data.get('phone_number')
        amount = data.get('amount')
        ref = data.get('account_reference', f'Divine{datetime.now().strftime("%H%M%S")}')
        desc = data.get('description', 'Beauty Shop Payment')
        if not phone or not amount:
            return jsonify({'error': 'Phone and amount required', 'success': False}), 400
        result = mpesa_api.stk_push(phone, amount, ref, desc)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/mpesa/status/<checkout_request_id>', methods=['GET'])
def mpesa_check_status(checkout_request_id):
    try:
        result = mpesa_api.query_status(checkout_request_id)
        rc = result.get('ResultCode')
        rd = result.get('ResultDesc', '')
        if str(rc) == '0': return jsonify({'status': 'completed', 'result_code': 0, 'result_description': rd})
        elif str(rc) == '1032': return jsonify({'status': 'cancelled', 'result_code': 1032, 'result_description': 'Cancelled by user'})
        elif rc is not None: return jsonify({'status': 'failed', 'result_code': int(rc), 'result_description': rd})
        return jsonify({'status': 'pending'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/mpesa/callback', methods=['POST'])
def mpesa_callback():
    try:
        data = request.json
        print(f"M-Pesa Callback: {json.dumps(data)}")
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Success'})
    except:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Error'})

# ========== USERS ENDPOINTS ==========
@app.route('/api/users', methods=['GET'])
def get_users():
    return jsonify(execute_query("SELECT id, username, full_name, role, phone, is_active, last_login FROM users", fetch=True))

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    pw = bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt())
    pin = bcrypt.hashpw(data.get('pin', '0000').encode(), bcrypt.gensalt()) if data.get('pin') else None
    execute_query('''INSERT INTO users (id, username, password_hash, pin_hash, full_name, role, phone)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (str(uuid.uuid4()), data['username'], pw, pin, data['full_name'], data.get('role', 'cashier'), data.get('phone')))
    return jsonify({'success': True})

@app.route('/api/users/<user_id>/password', methods=['PUT'])
def change_password(user_id):
    pw = bcrypt.hashpw(request.json['password'].encode(), bcrypt.gensalt())
    execute_query("UPDATE users SET password_hash=? WHERE id=?", (pw, user_id))
    return jsonify({'success': True})

# ========== SETTINGS ENDPOINTS ==========
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify({s['key']: s['value'] for s in execute_query("SELECT * FROM settings", fetch=True)})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    data = request.json
    for k, v in data.items():
        execute_query("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (k, v, datetime.now()))
    return jsonify({'success': True})

# ========== SYNC ENDPOINT ==========
@app.route('/api/sync', methods=['POST'])
def sync_data():
    data = request.json
    last_sync = data.get('last_sync', '2000-01-01 00:00:00')
    return jsonify({
        'products': execute_query("SELECT * FROM products WHERE updated_at > ?", (last_sync,), fetch=True),
        'sales': execute_query("SELECT * FROM sales WHERE created_at > ?", (last_sync,), fetch=True),
        'users': execute_query("SELECT id, username, full_name, role, phone, is_active FROM users", fetch=True),
        'settings': {s['key']: s['value'] for s in execute_query("SELECT * FROM settings", fetch=True)},
        'sync_time': datetime.now().isoformat()
    })

# Initialize database
init_db()

if __name__ == '__main__':
    print("=" * 50)
    print("Divine Beauty POS - Central API Server")
    print("Server: http://0.0.0.0:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
