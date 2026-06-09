"""
M-Pesa Callback Server - Production Runner
Uses Waitress for reliable multi-threaded serving
"""
from waitress import serve
from app import app

if __name__ == '__main__':
    print("=" * 50)
    print("M-Pesa Callback Server Starting...")
    print("Server: https://divine-pos-backend.onrender.com")
    print("Threads: 8 (handles concurrent requests)")
    print("=" * 50)
    
    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=8,        # Handle up to 8 concurrent requests
        connection_limit=100,
        channel_timeout=30
    )
