"""
Run this from D:\trandia\backend to diagnose the MongoDB SSL issue.
Usage: python test_mongo.py
"""
import socket, ssl, sys, certifi

HOST = 'ac-8u7rlni-shard-00-00.bxqpxpn.mongodb.net'
PORT = 27017

print("=" * 60)
print(f"Python   : {sys.version}")
print(f"SSL      : {ssl.OPENSSL_VERSION}")
print(f"certifi  : {certifi.__version__}")
print("=" * 60)

# ── Test 1: Raw TCP ──────────────────────────────────────────
print("\n[1] Raw TCP connection ...")
try:
    s = socket.create_connection((HOST, PORT), timeout=10)
    print("    ✅ TCP OK")
    s.close()
except Exception as e:
    print(f"    ❌ TCP FAILED — {e}")
    print("    → Port 27017 is being blocked (Firewall/ISP)")
    sys.exit(1)

# ── Test 2: TLS with CERT_NONE (no verification) ────────────
print("\n[2] TLS handshake (no cert check) ...")
try:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode   = ssl.CERT_NONE
    with socket.create_connection((HOST, PORT), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as s:
            print(f"    ✅ TLS OK  — version: {s.version()}")
except Exception as e:
    print(f"    ❌ TLS FAILED — {e}")
    print("    → Antivirus / Windows Schannel is intercepting TLS")

# ── Test 3: TLS with certifi CA ──────────────────────────────
print("\n[3] TLS handshake (certifi CA) ...")
try:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.check_hostname = False
    ctx.verify_mode   = ssl.CERT_NONE
    with socket.create_connection((HOST, PORT), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as s:
            print(f"    ✅ TLS+certifi OK — version: {s.version()}")
except Exception as e:
    print(f"    ❌ TLS+certifi FAILED — {e}")

# ── Test 4: Full pymongo ping ────────────────────────────────
print("\n[4] pymongo ping ...")
try:
    from pymongo import MongoClient
    import os, dotenv
    dotenv.load_dotenv()
    url = os.getenv("MONGODB_URL")
    c = MongoClient(url, serverSelectionTimeoutMS=10000,
                    tlsAllowInvalidCertificates=True,
                    tlsAllowInvalidHostnames=True)
    c.admin.command("ping")
    print("    ✅ pymongo ping OK — MongoDB is reachable!")
    c.close()
except Exception as e:
    print(f"    ❌ pymongo ping FAILED — {e}")

print("\n" + "=" * 60)
