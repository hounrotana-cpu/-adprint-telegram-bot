"""ADPrint Khmer menu bot. Python standard library only; durable Telegram subscription storage."""
# Deployment: paid Render service, one instance, Persistent Disk at /var/data (1 GB).
# Install the disk before replacing main.py. No additional Python packages required.
# Existing memory-only contacts cannot be reconstructed safely; ask them to Start once
# after migration. Future starts and stops survive restarts and deployments.
import secrets
import sqlite3
import hashlib
import hmac
import json
import os
import shutil
from pathlib import Path
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
SECRET = hmac.new(TOKEN.encode(), b'adprint-webhook-v1', hashlib.sha256).hexdigest() if TOKEN else ''
BASE_URL = os.getenv('RENDER_EXTERNAL_URL', '').rstrip('/')
READY = False
BOT_ID = None
BRIDGE_KEY = hmac.new(TOKEN.encode(), b'adprint-broadcast-bridge-v1', hashlib.sha256).hexdigest() if TOKEN else ''
# On Render, attach a Persistent Disk at /var/data before deploying.
# Never silently fall back to memory or an ephemeral source-directory database.
DATA_DIRECTORY = Path(os.getenv('ADPRINT_DATA_DIR', '/var/data'))
SUBSCRIBER_LOCK = threading.Lock()

def database():
    if not DATA_DIRECTORY.is_absolute():
        raise RuntimeError('absolute_data_directory_required')
    if os.getenv('RENDER') and not os.path.ismount(DATA_DIRECTORY):
        raise RuntimeError('persistent_disk_required')
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DATA_DIRECTORY / 'telegram.sqlite3'), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA synchronous=FULL')
    return connection

def init_storage():
    with SUBSCRIBER_LOCK:
        connection = database()
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS bridge_metadata (bot_id INTEGER PRIMARY KEY, generation TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')
                connection.execute('CREATE TABLE IF NOT EXISTS subscribers (bot_id INTEGER NOT NULL, chat_id TEXT NOT NULL, name TEXT NOT NULL, username TEXT NOT NULL, active INTEGER NOT NULL, event_date INTEGER NOT NULL, event_id INTEGER NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(bot_id,chat_id))')
                connection.execute('INSERT OR IGNORE INTO bridge_metadata(bot_id,generation) VALUES (?,?)', (BOT_ID, secrets.token_hex(16)))
        finally:
            connection.close()

def contacts_snapshot():
    with SUBSCRIBER_LOCK:
        connection = database()
        try:
            with connection:
                connection.execute('BEGIN')
                metadata = connection.execute('SELECT generation FROM bridge_metadata WHERE bot_id=?', (BOT_ID,)).fetchone()
                rows = connection.execute('SELECT * FROM subscribers WHERE bot_id=? ORDER BY chat_id', (BOT_ID,)).fetchall()
                if not metadata:
                    raise RuntimeError('storage_not_initialized')
                return metadata['generation'], [{'chatId': r['chat_id'], 'name': r['name'], 'username': r['username'], 'active': bool(r['active']), 'updateId': r['revision']} for r in rows]
        finally:
            connection.close()

def subscription_update(message, update_id):
    text = str(message.get('text', '')).strip().split()
    command = text[0].split('@')[0].lower() if text else ''
    if command not in ('/start', '/stop'):
        return command
    if not READY or not BOT_ID:
        raise RuntimeError('storage_not_ready')
    chat = message['chat']
    chat_id = str(chat['id'])
    event_date = message.get('date')
    if not isinstance(event_date, int) or isinstance(event_date, bool) or event_date < 0:
        raise ValueError('invalid_event_date')
    with SUBSCRIBER_LOCK:
        connection = database()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                old = connection.execute('SELECT event_date,event_id FROM subscribers WHERE bot_id=? AND chat_id=?', (BOT_ID,chat_id)).fetchone()
                # Telegram can restart update_id numbering after a long idle period.
                if old and (event_date,update_id) <= (old['event_date'],old['event_id']):
                    return command
                if not old and connection.execute('SELECT count(*) FROM subscribers WHERE bot_id=?', (BOT_ID,)).fetchone()[0] >= 10000:
                    raise RuntimeError('subscriber_capacity')
                connection.execute('UPDATE bridge_metadata SET revision=revision+1 WHERE bot_id=?', (BOT_ID,))
                revision = connection.execute('SELECT revision FROM bridge_metadata WHERE bot_id=?', (BOT_ID,)).fetchone()[0]
                name = (' '.join(filter(None,[chat.get('first_name'),chat.get('last_name')])) or chat_id)[:300]
                username = str(chat.get('username',''))[:100]
                connection.execute('INSERT INTO subscribers VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE SET name=excluded.name,username=excluded.username,active=excluded.active,event_date=excluded.event_date,event_id=excluded.event_id,revision=excluded.revision', (BOT_ID,chat_id,name,username,int(command=='/start'),event_date,update_id,revision))
        finally:
            connection.close()
    return command

VIDEO_PATH = Path(__file__).with_name('1678768941270154082.mp4')
STICKER_PROMO = """ជម្រាបសួរបង! 
🎉🎉 នេះជាតម្លៃប្រម៉ូសិនស្ទីកគ័រក្រដាស មានអ៊ុត៖
✅ មិនហើរពណ៌ ស្អិតល្អ បោះពុម្ពច្បាស់ស្អាត
• 1m² = $6.50
• 10m² = $55 ថែមជូន 3m²
👉 សរុបបាន 13m² = $4.23/m²"""
STICKER_FOLLOWUP = """បងចង់បានទំហំប៉ុន្មាន និងចំនួនប៉ុន្មានដែរ?"""
STICKER_CONTACT = """សូមផ្ញើរូបគំរូ និងថ្ងៃត្រូវការទៅផ្នែកលក់៖
https://t.me/ADPrint168"""
SEND_LOCK = threading.Lock()
SEND_PROGRESS = {}  # Bounded retry tracking; resets on restart.
VIDEO_FILE_ID = None
MENU = {'keyboard': [['ស្ទីកគ័រ', 'ប្រអប់'], ['ថង់ក្រដាស', 'សៀវភៅ'], ['ស្នើសុំតម្លៃ', 'ទាក់ទងផ្នែកលក់']], 'resize_keyboard': True}
CONTACT = 'សូមទាក់ទងផ្នែកលក់តាម https://t.me/ADPrint168 ដើម្បីផ្ញើព័ត៌មាន និងបញ្ជាក់ការបញ្ជាទិញ។'
QUOTE = 'សម្រាប់ស្នើសុំតម្លៃ សូមរៀបចំព័ត៌មាន៖\n1. ប្រភេទផលិតផល\n2. ទំហំ (សង់ទីម៉ែត្រ)\n3. ចំនួន\n4. សម្ភារៈ និងការកែច្នៃ\n5. ថ្ងៃត្រូវការទទួល\n6. រូបគំរូ ឬឯកសាររចនា\n\n' + CONTACT + '\nតម្លៃ និងថ្ងៃប្រគល់ត្រូវបញ្ជាក់ដោយផ្នែកលក់។'

def reply_for(message):
    text = str(message.get('text', '')).strip().lower()
    if message.get('photo') or message.get('document'):
        return 'Bot នេះមិនទាន់អាចពិនិត្យឯកសារ ឬបញ្ជូនទៅផ្នែកលក់បានទេ។ ' + CONTACT
    if text.startswith('/start') or text in ('hi', 'hello', 'សួស្តី', 'ជម្រាបសួរ', '/help'):
        return 'សួស្តីបង! សូមជ្រើសផលិតផលខាងក្រោម។'
    if text == 'ទាក់ទងផ្នែកលក់' or text == '/contact':
        return CONTACT
    products = [('ស្ទីកគ័រ', ('ស្ទីក', 'sticker')), ('ប្រអប់', ('ប្រអប់', 'box')), ('ថង់ក្រដាស', ('ថង់', 'bag')), ('សៀវភៅ', ('សៀវភៅ', 'book'))]
    for name, keywords in products:
        if any(word in text for word in keywords):
            if name == 'ស្ទីកគ័រ':
                return STICKER_PROMO
            return 'បងចាប់អារម្មណ៍បោះពុម្ព' + name + '។\n\n' + QUOTE
    if any(word in text for word in ('តម្លៃ', 'price', 'quote')) or text == '/quote':
        return QUOTE
    return 'សូមជ្រើសម៉ឺនុយខាងក្រោមសម្រាប់ព័ត៌មានបោះពុម្ព ឬស្នើសុំតម្លៃ។\n' + CONTACT + '\nBot នេះមិនទាន់កត់ត្រាការបញ្ជាទិញទេ។'

def telegram(method, payload):
    request = urllib.request.Request('https://api.telegram.org/bot' + TOKEN + '/' + method, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=50) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram API request failed')
    return result['result']

def send_sticker(chat_id, update_id):
    global VIDEO_FILE_ID
    # Track successful steps so ordinary webhook retries do not repeat them.
    with SEND_LOCK:
        if update_id not in SEND_PROGRESS:
            if len(SEND_PROGRESS) >= 1000:
                SEND_PROGRESS.pop(next(iter(SEND_PROGRESS)))
            SEND_PROGRESS[update_id] = 0
        if SEND_PROGRESS[update_id] == 0:
            if VIDEO_PATH.is_file() and BASE_URL.startswith('https://'):
                result = telegram('sendVideo', {'chat_id': chat_id,
                    'video': VIDEO_FILE_ID or BASE_URL + '/promo.mp4',
                    'caption': STICKER_PROMO})
                VIDEO_FILE_ID = result.get('video', {}).get('file_id') or VIDEO_FILE_ID
            else:
                telegram('sendMessage', {'chat_id': chat_id, 'text': STICKER_PROMO})
            SEND_PROGRESS[update_id] = 1
        if SEND_PROGRESS[update_id] == 1:
            telegram('sendMessage', {'chat_id': chat_id, 'text': STICKER_FOLLOWUP})
            SEND_PROGRESS[update_id] = 2
        if SEND_PROGRESS[update_id] == 2:
            telegram('sendMessage', {'chat_id': chat_id, 'text': STICKER_CONTACT,
                'reply_markup': MENU})
            SEND_PROGRESS[update_id] = 3

def register():
    global READY, BOT_ID
    if not TOKEN or not BASE_URL.startswith('https://'):
        print('Setup pending: set TELEGRAM_BOT_TOKEN and a public HTTPS URL.', flush=True)
        return
    for attempt in range(8):
        try:
            identity = telegram('getMe', {})
            if identity.get('username', '').lower() != 'adprintadmin_bot':
                print('Setup stopped: token belongs to a different bot.', flush=True)
                return
            BOT_ID = identity['id']
            init_storage()
            telegram('setWebhook', {'url': BASE_URL + '/telegram', 'secret_token': SECRET, 'allowed_updates': ['message'], 'max_connections': 4, 'drop_pending_updates': False})
            READY = True
            print('ADPrintAdmin_bot webhook registered.', flush=True)
            return
        except Exception:
            # Never log exceptions: Telegram URLs contain the token.
            print('Webhook setup failed; retrying without logging secrets.', flush=True)
            time.sleep(min(2 ** attempt, 30))
    print('Webhook setup failed. Check token and redeploy.', flush=True)

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/broadcast/contacts':
            supplied = self.headers.get('Authorization', '')
            if not BRIDGE_KEY or not hmac.compare_digest(supplied.encode(), ('Bearer ' + BRIDGE_KEY).encode()):
                return self.respond(403, {'error': 'forbidden'})
            if not READY or not BOT_ID:
                return self.respond(503, {'error': 'not_ready'})
            try:
                generation, contacts = contacts_snapshot()
            except (RuntimeError, sqlite3.Error, OSError):
                return self.respond(503, {'error': 'persistent_storage_unavailable'})
            return self.respond(200, {'botId': BOT_ID, 'generation': generation, 'contacts': contacts, 'storage': 'persistent', 'protocol': 2})
        if self.path == '/promo.mp4' and VIDEO_PATH.is_file():
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(VIDEO_PATH.stat().st_size))
            self.end_headers()
            with VIDEO_PATH.open('rb') as video:
                shutil.copyfileobj(video, self.wfile)
            return
        if self.path not in ('/', '/health'):
            return self.respond(404, {'error': 'not_found'})
        self.respond(200, {'service': 'ADPrint Telegram Bot', 'status': 'ready' if READY else 'setup_required', 'storage': 'persistent' if READY else 'unavailable', 'protocol': 2})

    def do_POST(self):
        if self.path != '/telegram':
            return self.respond(404, {'error': 'not_found'})
        supplied = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not TOKEN or not hmac.compare_digest(supplied.encode(), SECRET.encode()):
            return self.respond(403, {'error': 'forbidden'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1048576:
                return self.respond(413, {'error': 'invalid_size'})
            update = json.loads(self.rfile.read(size))
            if not isinstance(update, dict):
                return self.respond(400, {'error': 'invalid_update'})
            message = update.get('message')
            if not isinstance(message, dict):
                return self.respond(200, {'ok': True})
            chat = message.get('chat', {})
            if chat.get('type') != 'private' or not isinstance(chat.get('id'), int) or message.get('from', {}).get('is_bot'):
                return self.respond(200, {'ok': True})
            update_id = update.get('update_id')
            if not isinstance(update_id, int):
                return self.respond(400, {'error': 'invalid_update'})
            try:
                command = subscription_update(message, update_id)
            except (RuntimeError, sqlite3.Error, OSError):
                return self.respond(503, {'error': 'subscription_not_saved_retry'})
            if command == '/stop':
                return self.respond(200, {'ok': True})
            reply = reply_for(message)
            if reply == STICKER_PROMO:
                update_id = update.get('update_id')
                if not isinstance(update_id, int):
                    return self.respond(400, {'error': 'invalid_update'})
                try:
                    send_sticker(chat['id'], update_id)
                except Exception:
                    # Never log API exceptions because URLs contain the token.
                    return self.respond(503, {'error': 'send_failed_retry'})
                self.respond(200, {'ok': True})
            else:
                self.respond(200, {'method': 'sendMessage', 'chat_id': chat['id'], 'text': reply, 'reply_markup': MENU})
        except (ValueError, TypeError, AttributeError):
            self.respond(400, {'error': 'invalid_update'})

if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Handler)
    threading.Thread(target=register, daemon=True).start()
    print('ADPrint web service started.', flush=True)
    server.serve_forever()







