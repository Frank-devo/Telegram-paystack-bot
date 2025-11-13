Telegram + Paystack Voucher Bot (Flask)

Files: app.py, requirements.txt, vouchers.json, README.md

app.py

from flask import Flask, request, jsonify import os import requests import sqlite3 import threading import json import time from telegram import Bot, ReplyKeyboardMarkup

--- Configuration (from env) ---

BOT_TOKEN = os.environ.get('BOT_TOKEN') PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')

If you want to force bank provider: 'fidelity-bank' etc. Leave empty to use Paystack default

PREFERRED_BANK = os.environ.get('PREFERRED_BANK', 'fidelity-bank')

if not BOT_TOKEN or not PAYSTACK_SECRET_KEY: raise Exception('Set BOT_TOKEN and PAYSTACK_SECRET_KEY in environment')

app = Flask(name) bot = Bot(token=BOT_TOKEN)

DB_PATH = 'botdata.db' VOUCHERS_FILE = 'vouchers.json'  # you will provide this file with pre-generated codes

--- DB helpers ---

def init_db(): conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, plan TEXT, va_account TEXT)''') c.execute('''CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, paystack_ref TEXT UNIQUE, chat_id INTEGER, amount INTEGER, status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''') c.execute('''CREATE TABLE IF NOT EXISTS vouchers (code TEXT PRIMARY KEY, plan TEXT, used INTEGER DEFAULT 0, assigned_to INTEGER, assigned_at TIMESTAMP)''') conn.commit() conn.close()

def load_vouchers_into_db(): if not os.path.exists(VOUCHERS_FILE): print('vouchers.json not found; create it with your pre-generated codes') return with open(VOUCHERS_FILE,'r') as f: data = json.load(f) conn = sqlite3.connect(DB_PATH) c = conn.cursor() for plan, codes in data.items(): for code in codes: try: c.execute('INSERT OR IGNORE INTO vouchers(code, plan, used) VALUES (?,?,0)', (code, plan)) except Exception as e: print('err', e) conn.commit() conn.close()

--- Simple in-memory conversation state ---

CONV = {}  # chat_id -> state dict PLANS = { 'Daily': 500, 'Weekly': 2500, 'Bi-weekly': 5500, 'Monthly': 8000 }

--- Telegram message helpers ---

def send_message(chat_id, text, reply_markup=None): bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

--- Paystack helpers ---

PAYSTACK_BASE = 'https://api.paystack.co'

def create_paystack_customer(email, first_name, last_name, phone=None): url = PAYSTACK_BASE + '/customer' payload = { 'email': email, 'first_name': first_name, 'last_name': last_name } if phone: payload['phone'] = phone r = requests.post(url, json=payload, headers={'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}'}) r.raise_for_status() return r.json()['data']

def create_dedicated_account(customer_id, preferred_bank=None): url = PAYSTACK_BASE + '/dedicated_account' payload = {'customer': customer_id} if preferred_bank: payload['preferred_bank'] = preferred_bank r = requests.post(url, json=payload, headers={'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}'}) r.raise_for_status() return r.json()['data']

--- Voucher assignment ---

def assign_voucher_for_plan(plan, chat_id): conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('SELECT code FROM vouchers WHERE plan=? AND used=0 LIMIT 1', (plan,)) row = c.fetchone() if not row: conn.close() return None code = row[0] c.execute('UPDATE vouchers SET used=1, assigned_to=?, assigned_at=CURRENT_TIMESTAMP WHERE code=?', (chat_id, code)) conn.commit() conn.close() return code

--- Webhook endpoint for Paystack ---

@app.route('/paystack/webhook', methods=['POST']) def paystack_webhook(): # Verify signature signature = request.headers.get('X-Paystack-Signature') or request.headers.get('x-paystack-signature') raw = request.get_data() import hashlib, hmac expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest() if signature != expected: return jsonify({'status':'error','message':'invalid signature'}), 400 event = request.json # Handle charge.success or dedicated_account.transaction.success evt = event.get('event') data = event.get('data',{}) # We will treat 'charge.success' and 'transfer' ... Paystack sends different events; we'll accept any with status 'success' if data.get('status') == 'success' or evt in ['dedicated_account.transaction', 'charge.success', 'transfer.success', 'customer.transfer.success']: reference = data.get('reference') or data.get('transaction') # Try to find payment record by reference conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('SELECT chat_id FROM payments WHERE paystack_ref=?', (reference,)) row = c.fetchone() if row: chat_id = row[0] # mark payment c.execute('UPDATE payments SET status="success" WHERE paystack_ref=?', (reference,)) conn.commit() conn.close() # assign voucher # fetch user plan conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('SELECT plan FROM users WHERE chat_id=?', (chat_id,)) r = c.fetchone() plan = r[0] if r else None code = None if plan: code = assign_voucher_for_plan(plan, chat_id) if code: send_message(chat_id, f'Payment confirmed ✅\nYour voucher code is: {code}\nEnter it in the portal to redeem.') else: send_message(chat_id, f'Payment confirmed ✅\nNo voucher available for plan {plan}. Please contact admin.') return jsonify({'status':'ok'}) return jsonify({'status':'ignored'})

--- Simple endpoints to check service ---

@app.route('/health') def health(): return 'OK'

--- Telegram polling (simple state machine) ---

def start_polling(): from time import sleep print('Starting polling...') offset = None while True: try: updates = bot.get_updates(offset=offset, timeout=30) for u in updates: offset = u.update_id + 1 if u.message is None: continue chat_id = u.message.chat_id text = (u.message.text or '').strip() # Basic handlers if text.lower() == '/start' or text.lower() == 'hi' or text.lower()=='hello': CONV[chat_id] = {'stage':'ask_name'} send_message(chat_id, 'Hello! What is your first name?') continue state = CONV.get(chat_id) if not state: send_message(chat_id, 'Send /start to begin purchase.') continue stage = state.get('stage') if stage == 'ask_name': state['first_name'] = text state['stage'] = 'ask_last' send_message(chat_id, 'Great. Now send your last name.') continue if stage == 'ask_last': state['last_name'] = text state['stage'] = 'ask_email' send_message(chat_id, 'Please send your email address.') continue if stage == 'ask_email': state['email'] = text state['stage'] = 'ask_plan' # show plans keyboard = ReplyKeyboardMarkup([[k] for k in PLANS.keys()], one_time_keyboard=True, resize_keyboard=True) send_message(chat_id, 'Which plan would you like? Choose one:', reply_markup=keyboard) continue if stage == 'ask_plan': plan = text if plan not in PLANS: send_message(chat_id, 'Please choose a plan from the keyboard.') continue state['plan'] = plan # create Paystack customer + dedicated account try: cust = create_paystack_customer(state['email'], state['first_name'], state['last_name']) va = create_dedicated_account(cust['id'], preferred_bank=PREFERRED_BANK) # store user conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('INSERT OR REPLACE INTO users(chat_id, first_name, last_name, email, plan, va_account) VALUES (?,?,?,?,?,?)', (chat_id, state['first_name'], state['last_name'], state['email'], plan, va['account_number'])) conn.commit() conn.close() # create a payment record with Paystack reference - Paystack will send events with 'reference' field # We'll keep reference as va['reference'] if available otherwise va['account_number'] reference = va.get('reference') or va.get('account_number') amount = PLANS[plan] conn = sqlite3.connect(DB_PATH) c = conn.cursor() c.execute('INSERT OR IGNORE INTO payments(paystack_ref, chat_id, amount, status) VALUES (?,?,?,?)', (reference, chat_id, amount, 'pending')) conn.commit() conn.close() # send account details to user text_msg = f"Please pay ₦{amount} to the following account:\n\nAccount Name: Flutter Wave / {state['first_name']} {state['last_name']}\nBank: {PREFERRED_BANK.replace('-', ' ').title()}\nAccount Number: {va.get('account_number')}\nReference: {reference}\n\nAfter payment is processed you will receive your voucher automatically." send_message(chat_id, text_msg) CONV.pop(chat_id, None) except Exception as e: send_message(chat_id, 'Sorry, could not create payment account. Try again later.') print('err create va', e) continue # Fallback send_message(chat_id, 'I did not understand. Send /start to begin.') except Exception as e: print('poll err', e) time.sleep(1)

--- Start web server + polling thread ---

if name == 'main': init_db() load_vouchers_into_db() t = threading.Thread(target=start_polling, daemon=True) t.start() port = int(os.environ.get('PORT', '5000')) app.run(host='0.0.0.0', port=port)

requirements.txt

flask

python-telegram-bot==13.15

requests

vouchers.json (example)

{

"Daily": ["DL-ABC001","DL-ABC002"],

"Weekly": ["WK-ABC001","WK-ABC002"],

"Bi-weekly": ["BW-ABC001"],

"Monthly": ["MN-ABC001"]

}

README.md (short)

1. Create a GitHub repo and push these files (app.py, requirements.txt, vouchers.json)

2. On Railway create new project -> Deploy from GitHub -> select repo

3. Set environment variables in Railway: BOT_TOKEN, PAYSTACK_SECRET_KEY, PREFERRED_BANK=fidelity-bank

4. Deploy. Set Paystack webhook to https://<your-railway-url>/paystack/webhook

5. Ensure vouchers.json contains your pre-generated Omada codes

6. Test: send /start to your bot, follow prompts, pay to the virtual account

7. When Paystack sends webhook after payment success the bot will send voucher automatically
