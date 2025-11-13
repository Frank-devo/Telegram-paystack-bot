#!/usr/bin/env python3
"""
Telegram + Paystack Voucher Bot (Flask)
Simplified and corrected version of the original app.py.

Environment variables required:
  BOT_TOKEN
  PAYSTACK_SECRET_KEY
Optional:
  PREFERRED_BANK (e.g. 'fidelity-bank')
  PORT
"""
import os
import json
import time
import hmac
import hashlib
import threading
import sqlite3
from typing import Optional

import requests
from flask import Flask, request, jsonify
from telegram import Bot, ReplyKeyboardMarkup

# Configuration (from env)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
PREFERRED_BANK = os.environ.get("PREFERRED_BANK", "").strip()  # empty => Paystack default

if not BOT_TOKEN or not PAYSTACK_SECRET_KEY:
    raise Exception("Set BOT_TOKEN and PAYSTACK_SECRET_KEY in environment")

app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

DB_PATH = "botdata.db"
VOUCHERS_FILE = "vouchers.json"  # file with pre-generated codes

# Simple conversation state (in-memory). For production use persistent state.
CONV = {}  # chat_id -> state dict

# Plans
PLANS = {"Daily": 500, "Weekly": 2500, "Bi-weekly": 5500, "Monthly": 8000}

# ---- Database helpers ----
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            plan TEXT,
            customer_id TEXT,
            dedicated_account TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan TEXT,
            code TEXT UNIQUE,
            used INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def load_vouchers_into_db():
    if not os.path.exists(VOUCHERS_FILE := VOUCHERS_FILE):
        print("vouchers.json not found; create it with your pre-generated codes")
        return
    with open(VOUCHERS_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception as e:
            print("Failed to parse vouchers.json:", e)
            return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for plan, codes in data.items():
        for code in codes:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO vouchers (plan, code, used) VALUES (?, ?, 0)",
                    (plan, code),
                )
            except Exception as e:
                print("Error inserting voucher:", e)
    conn.commit()
    conn.close()
    print("Vouchers loaded/merged into DB.")


# ---- Telegram helpers ----
def send_message(chat_id: int, text: str, reply_markup=None):
    try:
        bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        print("Failed to send message:", e)


# ---- Paystack helpers ----
PAYSTACK_BASE = "https://api.paystack.co"


def paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}


def create_paystack_customer(email: str, first_name: str, last_name: str, phone: Optional[str] = None) -> Optional[str]:
    url = PAYSTACK_BASE + "/customer"
    payload = {"email": email, "first_name": first_name, "last_name": last_name}
    if phone:
        payload["phone"] = phone
    resp = requests.post(url, json=payload, headers=paystack_headers(), timeout=20)
    if resp.ok:
        j = resp.json()
        return j.get("data", {}).get("id")
    else:
        print("create_paystack_customer failed:", resp.text)
    return None


def create_dedicated_account(customer_id: str, preferred_bank: Optional[str] = None) -> Optional[dict]:
    url = PAYSTACK_BASE + "/dedicated_account"
    payload = {"customer": customer_id}
    if preferred_bank:
        payload["preferred_bank"] = preferred_bank
    resp = requests.post(url, json=payload, headers=paystack_headers(), timeout=20)
    if resp.ok:
        return resp.json().get("data")
    else:
        print("create_dedicated_account failed:", resp.text)
    return None


# ---- Voucher assignment ----
def assign_voucher_for_plan(plan: str, chat_id: int) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code FROM vouchers WHERE plan=? AND used=0 LIMIT 1", (plan,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    code = row[0]
    c.execute("UPDATE vouchers SET used=1 WHERE code=?", (code,))
    conn.commit()
    conn.close()
    # Optionally store assignment in users table or a separate table
    return code


# ---- Paystack webhook verification ----
def verify_paystack_signature(raw_body: bytes, header_signature: Optional[str]) -> bool:
    if not header_signature:
        return False
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    # Many Paystack docs use HMAC hex digest; header is the hex digest
    return hmac.compare_digest(computed, header_signature)


@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    # Paystack sends header 'x-paystack-signature'
    header_signature = request.headers.get("X-Paystack-Signature") or request.headers.get("x-paystack-signature")
    raw_body = request.get_data()  # bytes
    if not verify_paystack_signature(raw_body, header_signature):
        print("Invalid paystack signature")
        return jsonify({"status": "error", "message": "Invalid signature"}), 400

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    # Typical event key: 'event' like 'charge.success'
    event = payload.get("event")
    data = payload.get("data", {})
    # We'll handle charge.success and use metadata.chat_id and metadata.plan if present
    if event == "charge.success" or data.get("status") == "success":
        metadata = data.get("metadata", {}) or {}
        try:
            chat_id = int(metadata.get("chat_id")) if metadata.get("chat_id") else None
        except Exception:
            chat_id = None
        plan = metadata.get("plan")
        # You might also check amount matches expected plan price, etc.
        if chat_id and plan:
            code = assign_voucher_for_plan(plan, chat_id)
            if code:
                send_message(chat_id, f"Payment confirmed for {plan}. Here is your voucher code: {code}")
                return jsonify({"status": "ok"}), 200
            else:
                send_message(chat_id, f"Payment received for {plan}, but no voucher available.")
                return jsonify({"status": "ok", "note": "no voucher"}), 200
    # Not-handled event
    return jsonify({"status": "ignored"}), 200


# ---- Telegram polling and simple state machine ----
def start_polling():
    print("Starting polling...")
    offset = None
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=30)
            for u in updates:
                offset = u.update_id + 1
                if not u.message:
                    continue
                chat_id = u.message.chat.id
                text = (u.message.text or "").strip()
                first = u.message.from_user.first_name or ""
                last = u.message.from_user.last_name or ""
                # Initialize conv state
                state = CONV.get(chat_id, {"step": "idle"})
                step = state.get("step", "idle")

                if text == "/start":
                    CONV[chat_id] = {"step": "awaiting_email", "first_name": first, "last_name": last}
                    send_message(chat_id, "Welcome! Please send your email address to proceed.")
                    continue

                if step == "awaiting_email":
                    # crude email validation
                    if "@" not in text:
                        send_message(chat_id, "That doesn't look like a valid email. Please send a valid email address.")
                        continue
                    CONV[chat_id].update({"email": text, "step": "awaiting_plan"})
                    # Show plan options
                    keyboard = ReplyKeyboardMarkup([[p] for p in PLANS.keys()], one_time_keyboard=True, resize_keyboard=True)
                    send_message(chat_id, "Choose a plan:", reply_markup=keyboard)
                    continue

                if step == "awaiting_plan":
                    if text not in PLANS:
                        send_message(chat_id, "Please choose a plan from the keyboard.")
                        continue
                    # Save user in DB
                    email = CONV[chat_id].get("email")
                    first_name = CONV[chat_id].get("first_name", first)
                    last_name = CONV[chat_id].get("last_name", last)
                    plan = text
                    # Create customer and dedicated account
                    customer_id = create_paystack_customer(email, first_name, last_name)
                    dedicated = None
                    if customer_id:
                        dedicated = create_dedicated_account(customer_id, preferred_bank=PREFERRED_BANK or None)
                    # persist user
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute(
                        """
                        INSERT OR REPLACE INTO users (chat_id, first_name, last_name, email, plan, customer_id, dedicated_account)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (chat_id, first_name, last_name, email, plan, customer_id, json.dumps(dedicated) if dedicated else None),
                    )
                    conn.commit()
                    conn.close()

                    # Provide dedicated account details (if present) so user can pay
                    if dedicated:
                        account_info = f"Please pay {PLANS[plan]} (in kobo if NGN) to the dedicated account:\nBank: {dedicated.get('bank', {}).get('name')}\nAccount name: {dedicated.get('account_name')}\nAccount number: {dedicated.get('account_number')}\nWhen payment clears, you'll receive voucher automatically."
                        # We'll include metadata instructions: when creating the Paystack charge, include metadata {chat_id, plan}
                        send_message(chat_id, account_info)
                    else:
                        # If dedicated account could not be created, fallback instructions
                        send_message(chat_id, f"Could not create a dedicated account. Please contact support. Plan: {plan}")

                    CONV[chat_id]["step"] = "idle"
                    continue

                # default fallback
                if text.lower() in ("help", "/help"):
                    send_message(chat_id, "Send /start to begin the purchase flow.")
                else:
                    send_message(chat_id, "Send /start to begin.")
        except Exception as e:
            print("Polling error:", e)
            time.sleep(3)


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    init_db()
    load_vouchers_into_db()
    # start polling in background thread
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
