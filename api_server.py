# backend/api_server.py
"""
Central API Server for Beauty Shop POS
Handles all data operations for both desktop and mobile apps
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
import uuid
import bcrypt
from datetime import datetime, timedelta
import threading

app = Flask(__name__)
CORS(app)

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'beauty_shop_central.db')
db_lock = threading.Lock()

# ========== Database Setup ==========
def init_db():
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Users
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
            pin_hash TEXT, full_name TEXT, role TEXT DEFAULT 'cashier',
            phone TEXT, is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )''')
        
        # Products
        cursor.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT, brand TEXT, sku TEXT UNIQUE,
            barcode TEXT, category TEXT, price REAL, cost_price REAL,
            quantity INTEGER DEFAULT 0, min_stock_level INTEGER DEFAULT 5,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Sales
        cursor.execute('''CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY, receipt_number TEXT UNIQUE,
            total_amount REAL, payment_method TEXT,
            payment_status TEXT DEFAULT 'completed',
            mpesa_checkout_request_id TEXT, mpesa_receipt_number TEXT,
            cash_tendered REAL, change_amount REAL,
            cashier_id TEXT, customer_phone TEXT,
            is_void INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Sale Items
        cursor.execute('''CREATE TABLE IF NOT EXISTS sale_items (
            id TEXT PRIMARY KEY, sale_id TEXT, product_id TEXT,
            product_name TEXT, quantity INTEGER,
            unit_price REAL, total_price REAL
        )''')
        
        # Settings
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Create admin user if not exists
        cursor.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if cursor.fetchone()[0] == 0:
            pw = bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt())
            pin = bcrypt.hashpw('1234'.encode(), bcrypt.gensalt())
            cursor.execute('''INSERT INTO users (id, username, password_hash, pin_hash, full_name, role)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (str(uuid.uuid4()), 'admin', pw, pin, 'Administrator', 'admin'))
        
        # Seed products if empty
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            products = [
                ('Matte Lipstick - Ruby Red', 'MAC Cosmetics', '1001', 'Makeup', 2500, 1800, 50),
                ('Hydrating Face Cream', 'Nivea', '1002', 'Skincare', 1200, 800, 100),
                ('Argan Oil Hair Treatment', 'Garnier', '1003', 'Hair Care', 1800, 1200, 75),
                ('Gel Nail Polish Set', 'NYX', '1004', 'Nails', 3500, 2500, 30),
                ('Rose Garden Perfume', 'Revlon', '1005', 'Fragrances', 4500, 3200, 40),
                ('Shea Butter Body Lotion', 'Dove', '1006', 'Bath & Body', 950, 650, 120),
                ('Makeup Brush Set', 'Maybelline', '1007', 'Tools', 2800, 2000, 25),
                ('Vitamin C Serum', 'Olay', '1008', 'Skincare', 3200, 2200, 45),
            ]
            for p in products:
                cursor.execute('''INSERT INTO products (id, name, brand, barcode, category, price, cost_price, quantity)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (str(uuid.uuid4()), p[0], p[1], p[2], p[3], p[4], p[5], p[6]))
        
        # Default settings
        cursor.execute("SELECT COUNT(*) FROM settings")
        if cursor.fetchone()[0] == 0:
            defaults = [
                ('shop_name', 'Glamour Beauty Shop'),
                ('shop_address', 'Nairobi, Kenya'),
                ('shop_phone', '0700000000'),
                ('tax_rate', '16'),
                ('currency', 'KES'),
            ]
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
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            if fetch:
                result = [dict(row) for row in cursor.fetchall()]
            else:
                result = cursor.lastrowid
            conn.commit()
            conn.close()
            return result
        except Exception as e:
            conn.rollback()
            conn.close()
            raise e

# ========== AUTH ENDPOINTS ==========
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '')
    password = data.get('password', '')
    
    users = execute_query("SELECT * FROM users WHERE username=? AND is_active=1", (username,), fetch=True)
    if users and bcrypt.checkpw(password.encode(), users[0]['password_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        user = users[0]
        return jsonify({'success': True, 'user': {
            'id': user['id'], 'username': user['username'],
            'fullName': user['full_name'], 'role': user['role'],
            'phone': user['phone']
        }})
    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/auth/login-pin', methods=['POST'])
def login_pin():
    data = request.json
    username = data.get('username', '')
    pin = data.get('pin', '')
    
    users = execute_query("SELECT * FROM users WHERE username=? AND is_active=1", (username,), fetch=True)
    if users and users[0]['pin_hash'] and bcrypt.checkpw(pin.encode(), users[0]['pin_hash']):
        execute_query("UPDATE users SET last_login=? WHERE id=?", (datetime.now(), users[0]['id']))
        user = users[0]
        return jsonify({'success': True, 'user': {
            'id': user['id'], 'username': user['username'],
            'fullName': user['full_name'], 'role': user['role'],
            'phone': user['phone']
        }})
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
    
    products = execute_query(query, params, fetch=True)
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
def add_product():
    data = request.json
    product_id = str(uuid.uuid4())
    execute_query('''INSERT INTO products (id, name, brand, sku, barcode, category, price, cost_price, quantity, min_stock_level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (product_id, data['name'], data.get('brand'), data.get('sku'), data.get('barcode'),
         data.get('category'), data['price'], data.get('cost_price', 0),
         data.get('quantity', 0), data.get('min_stock_level', 5)))
    return jsonify({'success': True, 'id': product_id})

@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    data = request.json
    execute_query('''UPDATE products SET name=?, brand=?, category=?, price=?, cost_price=?,
        quantity=?, min_stock_level=?, updated_at=? WHERE id=?''',
        (data['name'], data.get('brand'), data.get('category'), data['price'],
         data.get('cost_price', 0), data.get('quantity', 0),
         data.get('min_stock_level', 5), datetime.now(), product_id))
    return jsonify({'success': True})

# ========== SALES ENDPOINTS ==========
@app.route('/api/sales', methods=['POST'])
def create_sale():
    data = request.json
    sale_id = str(uuid.uuid4())
    receipt = f"GBS-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    execute_query('''INSERT INTO sales (id, receipt_number, total_amount, payment_method,
        payment_status, cash_tendered, change_amount, cashier_id, customer_phone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (sale_id, receipt, data['total_amount'], data['payment_method'],
         data.get('payment_status', 'completed'), data.get('cash_tendered'),
         data.get('change_amount'), data.get('cashier_id'), data.get('customer_phone')))
    
    for item in data.get('items', []):
        execute_query('''INSERT INTO sale_items (id, sale_id, product_id, product_name, quantity, unit_price, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (str(uuid.uuid4()), sale_id, item['product_id'], item['product_name'],
             item['quantity'], item['unit_price'], item['total_price']))
        execute_query("UPDATE products SET quantity=quantity-?, updated_at=? WHERE id=?",
            (item['quantity'], datetime.now(), item['product_id']))
    
    return jsonify({'success': True, 'sale_id': sale_id, 'receipt_number': receipt})

@app.route('/api/sales', methods=['GET'])
def get_sales():
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"
    
    sales = execute_query(
        "SELECT * FROM sales WHERE created_at BETWEEN ? AND ? AND is_void=0 ORDER BY created_at DESC",
        (start, end), fetch=True)
    return jsonify(sales)

@app.route('/api/reports/daily', methods=['GET'])
def daily_report():
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"
    
    summary = execute_query('''SELECT COUNT(*) as transactions, COALESCE(SUM(total_amount),0) as total_sales,
        COALESCE(SUM(CASE WHEN payment_method='cash' THEN total_amount ELSE 0 END),0) as cash_sales,
        COALESCE(SUM(CASE WHEN payment_method='mpesa' THEN total_amount ELSE 0 END),0) as mpesa_sales
        FROM sales WHERE created_at BETWEEN ? AND ? AND is_void=0 AND payment_status='completed' ''',
        (start, end), fetch=True)
    
    return jsonify({'date': date, 'summary': summary[0] if summary else {}})

# ========== USERS ENDPOINTS ==========
@app.route('/api/users', methods=['GET'])
def get_users():
    users = execute_query("SELECT id, username, full_name, role, phone, is_active, last_login FROM users", fetch=True)
    return jsonify(users)

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
    data = request.json
    pw = bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt())
    execute_query("UPDATE users SET password_hash=? WHERE id=?", (pw, user_id))
    return jsonify({'success': True})

# ========== SETTINGS ENDPOINTS ==========
@app.route('/api/settings', methods=['GET'])
def get_settings():
    settings = execute_query("SELECT * FROM settings", fetch=True)
    return jsonify({s['key']: s['value'] for s in settings})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    data = request.json
    for key, value in data.items():
        execute_query("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now()))
    return jsonify({'success': True})

# ========== SYNC ENDPOINT ==========
@app.route('/api/sync', methods=['POST'])
def sync_data():
    """Sync data between apps - returns all data since last sync"""
    data = request.json
    last_sync = data.get('last_sync', '2000-01-01 00:00:00')
    
    products = execute_query("SELECT * FROM products WHERE updated_at > ?", (last_sync,), fetch=True)
    sales = execute_query("SELECT * FROM sales WHERE created_at > ?", (last_sync,), fetch=True)
    users = execute_query("SELECT id, username, full_name, role, phone, is_active FROM users", fetch=True)
    settings = execute_query("SELECT * FROM settings", fetch=True)
    
    return jsonify({
        'products': products,
        'sales': sales,
        'users': users,
        'settings': {s['key']: s['value'] for s in settings},
        'sync_time': datetime.now().isoformat()
    })

@app.route('/')
def index():
    return jsonify({'status': 'running', 'service': 'Beauty Shop POS API', 'timestamp': datetime.now().isoformat()})

init_db()

if __name__ == '__main__':
    print("=" * 50)
    print("Beauty Shop POS - Central API Server")
    print("Server: http://0.0.0.0:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)