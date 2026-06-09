"""
Beauty Shop POS - M-Pesa Callback Server
This is the ONLY component that needs to be deployed.
Handles M-Pesa STK Push and callbacks from Safaricom.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
from datetime import datetime, timedelta
import requests
import json
import base64
from decouple import config
import threading
import time
import logging
import traceback
import signal
import sys
from contextlib import contextmanager

db_lock = threading.Lock()

def execute_db(query, params=None, fetch=False):
    """Execute a database query safely"""
    with db_lock:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            if fetch:
                result = cursor.fetchall()
                conn.commit()
                conn.close()
                return result
            else:
                conn.commit()
                conn.close()
                return cursor.lastrowid
        except Exception as e:
            conn.rollback()
            conn.close()
            raise e

def signal_handler(sig, frame):
    print('\nShutting down server gracefully...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests from POS apps

# Configuration
DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'divine_shop_mpesa.db')
DATABASE_TIMEOUT = 30  # Wait up to 30 seconds for database lock
MPESA_CONSUMER_KEY = config('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = config('MPESA_CONSUMER_SECRET')
MPESA_PASSKEY = config('MPESA_PASSKEY')
MPESA_SHORTCODE = config('MPESA_SHORTCODE', default='174379')
MPESA_ENVIRONMENT = config('MPESA_ENVIRONMENT', default='sandbox')
DATABASE_TIMEOUT = 30

# ========== Database Setup ==========
def init_db():
    """Initialize database for M-Pesa transactions"""
    conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
    cursor = conn.cursor()
    
    # M-Pesa transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mpesa_transactions (
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
        )
    ''')
    
    # Access tokens cache
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS access_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Shop registration (optional - for multiple shops)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS registered_shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT UNIQUE,
            shop_name TEXT,
            contact_phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# ========== M-Pesa API Helper ==========
class MpesaAPI:
    """Helper class for M-Pesa API operations"""
    
    def __init__(self):
        self.base_url = (
            'https://sandbox.safaricom.co.ke'
            if MPESA_ENVIRONMENT == 'sandbox'
            else 'https://api.safaricom.co.ke'
        )
    
    def get_access_token(self):
        """Get OAuth access token with caching"""
        # Check cached token
        try:
            rows = execute_db(
                'SELECT token FROM access_tokens WHERE expires_at > ? ORDER BY id DESC LIMIT 1',
                (datetime.now(),),
                fetch=True
            )
            if rows and len(rows) > 0:
                logger.info("Using cached access token")
                return rows[0]['token']
        except Exception as e:
            logger.error(f"Error checking cached token: {e}")
        
        # Get new token
        logger.info("Fetching new access token from Safaricom")
        auth_string = base64.b64encode(
            f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
        ).decode()
        
        response = requests.get(
            f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
            headers={'Authorization': f'Basic {auth_string}'},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            token = data['access_token']
            expires_in = int(data.get('expires_in', 3599))
            
            try:
                expires_at = datetime.now() + timedelta(seconds=expires_in - 60)
                execute_db(
                    'INSERT INTO access_tokens (token, expires_at) VALUES (?, ?)',
                    (token, expires_at)
                )
                logger.info(f"New token cached")
            except Exception as e:
                logger.error(f"Failed to cache token: {e}")
            
            return token
        else:
            raise Exception(f"Failed to get access token: {response.text}")
    
    def format_phone_number(self, phone):
        """Format phone number to 2547XXXXXXXX"""
        phone = phone.strip().replace('+', '').replace(' ', '')
        if phone.startswith('0'):
            return '254' + phone[1:]
        elif phone.startswith('254'):
            return phone
        else:
            return '254' + phone
    
    def stk_push(self, phone_number, amount, account_ref, description="Beauty Shop Payment"):
        """Initiate STK Push to customer's phone"""
        try:
            token = self.get_access_token()
            logger.info(f"Got access token: {token[:20]}...")  # Log first 20 chars
            
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password = base64.b64encode(
                f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
            ).decode()
            
            # Format phone
            phone_number = self.format_phone_number(phone_number)
            amount = int(amount)  # M-Pesa requires integer amount
            
            payload = {
                "BusinessShortCode": MPESA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount,
                "PartyA": phone_number,
                "PartyB": MPESA_SHORTCODE,
                "PhoneNumber": phone_number,
                "CallBackURL": f"https://gtechlabs-ke.web.app/api/mpesa/callback",
                "AccountReference": account_ref[:12] if account_ref else "BeautyShop",
                "TransactionDesc": description[:13]
            }
            
            logger.info(f"STK Push Payload: {json.dumps(payload, indent=2)}")
            logger.info(f"API URL: {self.base_url}/mpesa/stkpush/v1/processrequest")
            
            response = requests.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={'Authorization': f'Bearer {token}'},
                timeout=30
            )
            
            # Log the full response
            logger.info(f"Response Status: {response.status_code}")
            logger.info(f"Response Body: {response.text}")
            
            result = response.json()
            
            return {
                'success': result.get('ResponseCode') == '0',
                'checkout_request_id': result.get('CheckoutRequestID'),
                'merchant_request_id': result.get('MerchantRequestID'),
                'response_code': result.get('ResponseCode'),
                'response_description': result.get('ResponseDescription'),
                'customer_message': result.get('CustomerMessage')
            }
            
        except Exception as e:
            logger.error(f"STK Push error: {str(e)}")
            logger.error(f"Full error: {traceback.format_exc()}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def query_status(self, checkout_request_id):
        """Query STK Push status"""
        try:
            token = self.get_access_token()
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password = base64.b64encode(
                f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
            ).decode()
            
            payload = {
                "BusinessShortCode": MPESA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id
            }
            
            response = requests.post(
                f"{self.base_url}/mpesa/stkpushquery/v1/query",
                json=payload,
                headers={'Authorization': f'Bearer {token}'},
                timeout=10
            )
            
            return response.json()
            
        except Exception as e:
            logger.error(f"Status query error: {str(e)}")
            return {'error': str(e)}

# Initialize M-Pesa API
mpesa_api = MpesaAPI()

# ========== API Routes ==========
@app.route('/')
def index():
    """Health check"""
    return jsonify({
        'service': 'Beauty Shop POS - M-Pesa Callback Server',
        'status': 'running',
        'environment': MPESA_ENVIRONMENT,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/mpesa/stkpush', methods=['POST'])
def stk_push():
    """Initiate M-Pesa STK Push"""
    try:
        data = request.json
        
        phone_number = data.get('phone_number')
        amount = data.get('amount')
        shop_id = data.get('shop_id', 'default')
        account_ref = data.get('account_reference', f'Beauty{datetime.now().strftime("%H%M%S")}')
        description = data.get('description', 'Beauty Shop Payment')
        
        if not phone_number:
            return jsonify({'error': 'Phone number is required'}), 400
        if not amount or float(amount) <= 0:
            return jsonify({'error': 'Valid amount is required'}), 400
        
        # Initiate STK Push
        result = mpesa_api.stk_push(phone_number, amount, account_ref, description)
        
        if result['success']:
            # Save transaction
            try:
                execute_db('''
                    INSERT INTO mpesa_transactions 
                    (checkout_request_id, merchant_request_id, shop_id, phone_number, 
                     amount, account_reference, transaction_desc, status, transaction_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ''', (
                    result['checkout_request_id'],
                    result['merchant_request_id'],
                    shop_id,
                    phone_number,
                    amount,
                    account_ref,
                    description,
                    datetime.now()
                ))
            except Exception as e:
                logger.error(f"Failed to save transaction: {e}")
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"STK Push endpoint error: {str(e)}")
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """
    M-Pesa callback endpoint
    Called by: Safaricom M-Pesa servers automatically
    """
    try:
        callback_data = request.json
        logger.info(f"Callback received: {callback_data}")
        
        stk_callback = callback_data.get('Body', {}).get('stkCallback', {})
        
        checkout_request_id = stk_callback.get('CheckoutRequestID')
        result_code = stk_callback.get('ResultCode')
        result_desc = stk_callback.get('ResultDesc')
        
        if not checkout_request_id:
            return jsonify({'ResultCode': 1, 'ResultDesc': 'Missing CheckoutRequestID'})
        
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        cursor = conn.cursor()
        
        if result_code == 0:
            # Payment successful
            metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
            mpesa_ref = None
            amount = None
            
            for item in metadata:
                if item['Name'] == 'MpesaReceiptNumber':
                    mpesa_ref = item.get('Value')
                elif item['Name'] == 'Amount':
                    amount = item.get('Value')
            
            cursor.execute('''
                UPDATE mpesa_transactions 
                SET status = 'completed',
                    result_code = ?,
                    result_description = ?,
                    mpesa_receipt_number = ?,
                    callback_received_at = ?
                WHERE checkout_request_id = ?
            ''', (result_code, result_desc, mpesa_ref, datetime.now(), checkout_request_id))
            
            logger.info(f"Payment completed: {checkout_request_id} - {mpesa_ref}")
        else:
            # Payment failed
            cursor.execute('''
                UPDATE mpesa_transactions 
                SET status = 'failed',
                    result_code = ?,
                    result_description = ?,
                    callback_received_at = ?
                WHERE checkout_request_id = ?
            ''', (result_code, result_desc, datetime.now(), checkout_request_id))
            
            logger.info(f"Payment failed: {checkout_request_id} - {result_desc}")
        
        conn.commit()
        conn.close()
        
        # Always return success to M-Pesa
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Success'})
        
    except Exception as e:
        logger.error(f"Callback error: {str(e)}")
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Internal error'})

@app.route('/api/mpesa/status/<checkout_request_id>', methods=['GET'])
def check_status(checkout_request_id):
    """Check transaction status"""
    try:
        # Check database
        rows = execute_db(
            'SELECT * FROM mpesa_transactions WHERE checkout_request_id = ?',
            (checkout_request_id,),
            fetch=True
        )
        
        if rows and len(rows) > 0:
            transaction = dict(rows[0])
            if transaction.get('callback_received_at') and transaction.get('status') != 'pending':
                return jsonify({
                    'status': transaction['status'],
                    'result_code': transaction.get('result_code'),
                    'mpesa_receipt_number': transaction.get('mpesa_receipt_number'),
                    'amount': transaction.get('amount'),
                })
        
        # Query M-Pesa directly
        status_result = mpesa_api.query_status(checkout_request_id)
        result_code = status_result.get('ResultCode')
        result_desc = status_result.get('ResultDesc', '')
        
        if result_code is not None:
            if str(result_code) == '0':
                execute_db('''
                    UPDATE mpesa_transactions 
                    SET status = 'completed', result_code = ?, result_description = ?,
                        mpesa_receipt_number = COALESCE(mpesa_receipt_number, ?),
                        callback_received_at = COALESCE(callback_received_at, ?)
                    WHERE checkout_request_id = ?
                ''', (int(result_code), result_desc, 
                      f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                      datetime.now(), checkout_request_id))
                
                return jsonify({
                    'status': 'completed',
                    'result_code': 0,
                    'result_description': result_desc,
                })
            elif str(result_code) in ['1', '1032']:
                status = 'cancelled' if str(result_code) == '1032' else 'failed'
                execute_db('''
                    UPDATE mpesa_transactions 
                    SET status = ?, result_code = ?, result_description = ?,
                        callback_received_at = COALESCE(callback_received_at, ?)
                    WHERE checkout_request_id = ?
                ''', (status, int(result_code), result_desc, datetime.now(), checkout_request_id))
                
                return jsonify({
                    'status': status,
                    'result_code': int(result_code),
                    'result_description': result_desc,
                })
        
        return jsonify({'status': 'pending'})
        
    except Exception as e:
        logger.error(f"Status check error: {str(e)}")
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/mpesa/pending/<shop_id>', methods=['GET'])
def get_pending_transactions(shop_id):
    """
    Get pending transactions for a shop
    Used by POS to check for completed payments they might have missed
    """
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM mpesa_transactions WHERE shop_id = ? AND synced_to_shop = FALSE AND status = "completed"',
            (shop_id,)
        )
        results = cursor.fetchall()
        conn.close()
        
        if results:
            columns = ['id', 'checkout_request_id', 'merchant_request_id', 'shop_id',
                      'phone_number', 'amount', 'account_reference', 'transaction_desc',
                      'status', 'result_code', 'result_description', 'mpesa_receipt_number',
                      'transaction_date', 'created_at', 'callback_received_at', 'synced_to_shop']
            transactions = [dict(zip(columns, row)) for row in results]
            return jsonify(transactions)
        
        return jsonify([])
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mpesa/mark_synced', methods=['POST'])
def mark_as_synced():
    """
    Mark transaction as synced to POS
    """
    try:
        data = request.json
        checkout_request_id = data.get('checkout_request_id')
        
        if not checkout_request_id:
            return jsonify({'error': 'Missing checkout_request_id'}), 400
        
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE mpesa_transactions SET synced_to_shop = TRUE WHERE checkout_request_id = ?',
            (checkout_request_id,)
        )
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/register-shop', methods=['POST'])
def register_shop():
    """
    Register a shop (optional - for multi-shop support)
    """
    try:
        data = request.json
        shop_id = data.get('shop_id')
        shop_name = data.get('shop_name', 'Beauty Shop')
        contact_phone = data.get('contact_phone')
        
        if not shop_id:
            return jsonify({'error': 'shop_id is required'}), 400
        
        conn = sqlite3.connect(DATABASE_PATH, timeout=DATABASE_TIMEOUT)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO registered_shops (shop_id, shop_name, contact_phone)
            VALUES (?, ?, ?)
        ''', (shop_id, shop_name, contact_phone))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'shop_id': shop_id})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Initialize database on startup
init_db()

if __name__ == '__main__':
    import socket
    
    # Check if port is already in use
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', 5000))
    if result == 0:
        print("Port 5000 is already in use!")
        print("Please stop the existing server first:")
        print("  fuser -k 5000/tcp")
        sys.exit(1)
    sock.close()
    
    print("Starting M-Pesa Callback Server...")
    print("Server: http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)