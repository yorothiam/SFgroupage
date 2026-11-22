import streamlit as st
import pandas as pd
import sqlite3
import hashlib
import random
import string
import urllib.parse
import requests
from datetime import datetime

# ==========================================
# CONFIGURATION & BASE DE DONNÉES (SQLITE)
# ==========================================
st.set_page_config(
    page_title="SF GROUPAGE CHINE – SAINT-LOUIS",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="collapsed"
)

DB_NAME = "sf_groupage_unique.db"

def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_ref(prefix: str) -> str:
    rand_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{datetime.now().strftime('%y%m')}-{rand_str}"

def get_setting(key: str) -> str:
    conn = get_connection()
    res = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return res['value'] if res else ""

def update_setting(key: str, value: str):
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def normalize_whatsapp_phone(phone: str) -> str:
    """Format phone for WhatsApp Cloud API (digits only, international format)."""
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('0'):
        digits = '221' + digits[1:]
    elif len(digits) == 9 and not digits.startswith('221'):
        digits = '221' + digits
    return digits


def send_whatsapp_message(phone: str, message: str) -> tuple[bool, str]:
    """Send a WhatsApp notification through Meta WhatsApp Cloud API."""
    if get_setting('wa_notifications_enabled') != '1':
        return False, 'Notifications WhatsApp désactivées.'
    token = get_setting('wa_access_token').strip()
    phone_id = get_setting('wa_phone_number_id').strip()
    recipient = normalize_whatsapp_phone(phone)
    if not token or not phone_id:
        return False, 'Token API ou Phone Number ID manquant.'
    if not recipient:
        return False, 'Numéro destinataire invalide.'
    try:
        response = requests.post(
            f'https://graph.facebook.com/v20.0/{phone_id}/messages',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'messaging_product': 'whatsapp', 'recipient_type': 'individual',
                  'to': recipient, 'type': 'text', 'text': {'preview_url': False, 'body': message}},
            timeout=15
        )
        response.raise_for_status()
        return True, 'Message WhatsApp envoyé.'
    except requests.RequestException as exc:
        detail = getattr(getattr(exc, 'response', None), 'text', str(exc))
        return False, f'Échec WhatsApp API : {detail[:350]}'


def add_notification(user_id: int, title: str, message: str):
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO notifications (user_id, title, message, created_at) VALUES (?, ?, ?, ?)",
        (user_id, title, message, now)
    )
    user = conn.execute('SELECT full_name, whatsapp FROM users WHERE id=?', (user_id,)).fetchone()
    conn.commit()
    conn.close()

    # WhatsApp en option : envoie les notifications importantes au client.
    if user and get_setting('wa_notifications_enabled') == '1':
        category = 'general'
        lower = f'{title} {message}'.lower()
        if any(word in lower for word in ('commande', 'cmd-', 'order')):
            category = 'orders'
        elif any(word in lower for word in ('paiement', 'payment', 'pay-')):
            category = 'payments'
        elif any(word in lower for word in ('colis', 'suivi', 'expédi', 'transit', 'livré', 'arrivé')):
            category = 'tracking'
        elif any(word in lower for word in ('demande', 'devis', 'produit')):
            category = 'requests'
        if get_setting(f'wa_notify_{category}') == '1' or get_setting('wa_notify_general') == '1':
            text_message = f'Bonjour {user["full_name"]},\n\n{title}\n{message}\n\nSF GROUPAGE CHINE – SAINT-LOUIS'
            send_whatsapp_message(user['whatsapp'], text_message)

def log_order_event(order_id: int, status: str, description: str, updated_by: str):
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO order_history (order_id, status, description, updated_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, status, description, updated_by, now)
    )
    conn.commit()
    conn.close()

def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT DEFAULT 'client',
        username TEXT UNIQUE,
        full_name TEXT NOT NULL,
        whatsapp TEXT UNIQUE NOT NULL,
        city TEXT NOT NULL,
        address TEXT,
        email TEXT,
        company TEXT,
        password_hash TEXT NOT NULL,
        status TEXT DEFAULT 'actif',
        created_at TEXT
    )''')

    # Migration douce pour les bases déjà existantes
    user_columns = [row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()]
    if "username" not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN username TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username IS NOT NULL")

    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        mode TEXT,
        open_date TEXT,
        close_date TEXT,
        est_departure TEXT,
        est_arrival TEXT,
        instructions TEXT,
        status TEXT DEFAULT 'Ouvert'
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        specs TEXT,
        price_fcfa REAL,
        min_qty INTEGER DEFAULT 1,
        est_weight_kg REAL DEFAULT 0.0,
        mode TEXT,
        transport_included INTEGER DEFAULT 0,
        transport_details TEXT,
        deadline TEXT,
        est_delay TEXT,
        available INTEGER DEFAULT 1,
        image_data BLOB,
        batch_id INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        user_id INTEGER,
        product_name TEXT,
        link TEXT,
        image_data BLOB,
        quantity INTEGER,
        variants TEXT,
        known_price TEXT,
        packaging_options TEXT,
        delivery_city TEXT,
        mode TEXT,
        deadline TEXT,
        comment TEXT,
        status TEXT DEFAULT 'Nouvelle demande',
        admin_quote_price REAL DEFAULT 0.0,
        admin_transport_fee REAL DEFAULT 0.0,
        transport_included INTEGER DEFAULT 0,
        admin_notes TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        user_id INTEGER,
        source_type TEXT,
        source_id INTEGER,
        product_title TEXT,
        quantity INTEGER,
        variants TEXT,
        mode TEXT,
        batch_id INTEGER,
        items_price REAL,
        transport_fee REAL,
        extra_fee REAL DEFAULT 0.0,
        total_amount REAL,
        transport_included INTEGER,
        payment_schedule TEXT DEFAULT '1 tranche (100%)',
        status TEXT DEFAULT 'Commande validée',
        internal_note TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS order_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        status TEXT,
        description TEXT,
        updated_by TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        order_id INTEGER,
        user_id INTEGER,
        amount REAL,
        payment_date TEXT,
        method TEXT,
        proof_data BLOB,
        status TEXT DEFAULT 'En attente de vérification',
        verified_by TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS parcels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        order_id INTEGER,
        tracking_code TEXT,
        carton_count INTEGER DEFAULT 1,
        real_weight_kg REAL DEFAULT 0.0,
        applicable_fees REAL DEFAULT 0.0,
        photo_data BLOB,
        notes TEXT,
        updated_by TEXT,
        updated_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        message TEXT,
        is_read INTEGER DEFAULT 0,
        created_at TEXT
    )''')

    default_settings = {
        "site_name": "SF GROUPAGE CHINE – SAINT-LOUIS",
        "slogan": "Qualité, confiance et satisfaction.",
        "whatsapp": "76 866 10 90",
        "email": "contact@sfgroupage-sl.sn",
        "address": "Saint-Louis, Sénégal (Livraison Dakar & Régions)",
        "welcome_text": "Bienvenue chez SF Groupage Chine – Saint-Louis. Créez votre compte pour soumettre vos produits, valider vos commandes et suivre vos colis en toute simplicité. Notre équipe vous accompagne de la recherche du produit jusqu’à sa réception au Sénégal.",
        "air_delay": "Environ 15 jours (estimation habituelle pouvant varier selon le départ, le traitement des colis, le transport et les formalités)",
        "air_rate_info": "Offres sélectionnées 'Transport inclus' (selon conditions précisées par SF) ou calcul au poids selon la nature du produit.",
        "sea_delay": "Environ 2 mois (à titre indicatif et sous réserve des opérations logistiques)",
        "sea_rate_info": "190 000 F CFA / CBM (m³). Tarif calculé selon le volume, hors poids. Délai à confirmer selon le départ et l’acheminement.",
        "sourcing_fee": "2000",
        "validation_fee": "5000",
        "small_order_fee": "10000",
        "air_standard_rate": "9000",
        "air_express_rate": "14000",
        "sea_rate_cbm": "190000",
        "payment_methods": "Wave (76 866 10 90), Orange Money (76 866 10 90), Virement bancaire, Espèces (Agence Saint-Louis / Relais Dakar)",
        "banner_announcement": "🚀 SF GROUPAGE CHINE – SAINT-LOUIS : Groupage Aérien (~15 jours) & Maritime (~2 mois) ouverts !",
        "faq_text": "**1. Comment fonctionne le groupage ?**\nVous choisissez un produit ouvert à la validation ou vous nous envoyez un lien (1688, Alibaba, Taobao, Pinduoduo...). Nous vérifions, proposons un prix et des conditions, puis vous validez.\n\n**2. Les délais sont-ils garantis ?**\nNon, les délais habituels d'environ 15 jours en aérien et d'environ 2 mois en maritime sont des estimations et peuvent connaître des retards indépendants de notre volonté.\n\n**3. Quand paie-t-on le transport maritime ?**\nLes offres maritimes de SF sont indiquées 'Hors transport' : le fret est calculé séparément et réglé après l'arrivée des marchandises au Sénégal selon les conditions communiquées avant validation.",
        "cgu_text": "Une demande de produit n'est pas automatiquement une commande confirmée. La commande devient ferme après validation du prix et des conditions par le client, puis confirmation selon le processus de SF Groupage Chine – Saint-Louis. Tout frais supplémentaire est expliqué et validé selon les conditions acceptées."
    }

    for k, v in default_settings.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        users = [
            ('admin', 'Administrateur SF', '76 866 10 90', 'Saint-Louis', 'Saint-Louis', 'admin@sfgroupage.sn', 'SF Groupage Chine', hash_password('admin123'), 'actif', now),
            ('agent', 'Agent Entrepôt / Logistique', '77 000 00 01', 'Saint-Louis', 'Entrepôt Saint-Louis', 'agent@sfgroupage.sn', 'SF Groupage Chine', hash_password('agent123'), 'actif', now),
            ('client', 'Moussa Ndiaye (Client Test)', '77 123 45 67', 'Dakar', 'Mermoz, Dakar', 'moussa@exemple.sn', 'Commerce Ndiaye', hash_password('client123'), 'actif', now)
        ]
        c.executemany('''INSERT INTO users (role, full_name, whatsapp, city, address, email, company, password_hash, status, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', users)

        c.execute('''INSERT INTO batches (code, mode, open_date, close_date, est_departure, est_arrival, instructions, status)
                     VALUES ('VAGUE-AIR-21', 'Aerien', '2026-11-15', '2026-11-28', '2026-12-01', '2026-12-16', 'Validez vos articles avant la clôture.', 'Ouvert')''')
        c.execute('''INSERT INTO batches (code, mode, open_date, close_date, est_departure, est_arrival, instructions, status)
                     VALUES ('VAGUE-MER-11', 'Maritime', '2026-11-01', '2026-12-10', '2026-12-20', '2027-02-20', 'Option économique pour cartons et volumes importants.', 'Ouvert')''')

        c.execute('''INSERT INTO products (title, description, specs, price_fcfa, min_qty, est_weight_kg, mode, transport_included, transport_details, deadline, est_delay, available, batch_id)
                     VALUES ('Lot de 12 Sacs bandoulière', 'Modèle tendance pour revendeurs et boutiques.', 'Coloris variés | Dimensions moyennes', 54000, 1, 4.0, 'Aerien', 1, 'Transport aérien inclus selon conditions de cette offre', '2026-11-28', 'Environ 15 jours', 1, 1)''')
        c.execute('''INSERT INTO products (title, description, specs, price_fcfa, min_qty, est_weight_kg, mode, transport_included, transport_details, deadline, est_delay, available, batch_id)
                     VALUES ('Presse thermique multifonction', 'Pour personnalisation mugs, t-shirts et casquettes.', '220V | Châssis robuste', 135000, 1, 18.0, 'Maritime', 0, 'Hors transport : fret maritime calculé séparément et réglé à l arrivée au Sénégal', '2026-12-10', 'Environ 2 mois', 1, 2)''')

    # Crée/actualise le compte administrateur demandé (connexion par identifiant)
    admin_row = c.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    if admin_row:
        c.execute("UPDATE users SET username=?, password_hash=?, status='actif' WHERE id=?",
                  ('sambafall', hash_password('samba2026'), admin_row['id']))
    else:
        c.execute("INSERT INTO users (role, username, full_name, whatsapp, city, address, email, company, password_hash, status, created_at) VALUES ('admin', ?, ?, ?, ?, ?, ?, ?, ?, 'actif', ?)",
                  ('sambafall', 'Administrateur Sambafall', 'ADMIN-SAMBafall', 'Saint-Louis', 'Saint-Louis', 'admin@sfgroupage.sn', 'SF Groupage Chine', hash_password('samba2026'), now))

    conn.commit()
    conn.close()

init_db()

# ==========================================
# STYLES CSS — IDENTITÉ VISUELLE PREMIUM
# ==========================================
st.markdown("""
<style>
:root {
  --sf-bg:#07110F; --sf-panel:#101D1A; --sf-panel2:#172823;
  --sf-line:#29463B; --sf-text:#FFFFFF; --sf-muted:#E1EBE6;
  --sf-blue:#34D399; --sf-gold:#F6D365; --sf-ink:#F1FFF8;
}
/* Hide Streamlit chrome / side menu */
[data-testid="stSidebar"] { display:none !important; }
[data-testid="stToolbar"], [data-testid="stHeaderActionElements"], #MainMenu,
[data-testid="stStatusWidget"], [data-testid="stDecoration"], header {
  visibility:hidden !important; height:0 !important;
}
.stApp { background:radial-gradient(ellipse at 15% 0%,#18372D 0%,#091512 48%,#040907 100%); color:var(--sf-text); }
.block-container { padding-top:1.15rem !important; padding-bottom:2rem !important; max-width:1580px; }
.main-header { background:linear-gradient(120deg,#07110F 0%,#17372C 52%,#116149 100%); padding:2rem 2.2rem; border:1px solid #32604E; border-radius:24px; color:#fff; margin:0 0 1rem; box-shadow:0 18px 42px rgba(0,0,0,.42); position:relative; overflow:hidden; }
.main-header:after { content:""; position:absolute; width:250px;height:250px;right:-65px;top:-90px;border:1px solid rgba(255,255,255,.18);border-radius:50%;box-shadow:0 0 0 35px rgba(255,255,255,.045),0 0 0 70px rgba(255,255,255,.025); }
.main-header h1 { color:#FFFFFF !important; margin:0; font-size:clamp(1.65rem,3vw,2.6rem); letter-spacing:-.035em; font-weight:850; text-shadow:0 2px 18px rgba(0,0,0,.35); }
.main-header p { color:#E0F2FE !important; margin:.5rem 0 0; font-size:1.08rem; font-weight:600; }
.banner-box { background:linear-gradient(90deg,#382D10,#211B0D); border:1px solid #9B7B35; border-left:5px solid var(--sf-gold); padding:.9rem 1.15rem; border-radius:13px; color:#FFF6D9 !important; font-weight:750; margin-bottom:1rem; }
.card { background:linear-gradient(145deg,#14251F,#0D1814) !important; border:1px solid #2B493D !important; border-radius:19px; padding:1.2rem; box-shadow:0 10px 28px rgba(0,0,0,.30); margin-bottom:1rem; color:var(--sf-text) !important; }
div[data-testid="stMetric"] { background:linear-gradient(145deg,#1B332A,#101D19) !important; border:1px solid #315444 !important; padding:1.05rem; border-radius:16px; box-shadow:0 7px 20px rgba(0,0,0,.26); }
div[data-testid="stMetric"] label, div[data-testid="stMetric"] [data-testid="stMetricLabel"] { color:#DCEBE3 !important; font-weight:700 !important; }
div[data-testid="stMetric"] [data-testid="stMetricValue"] { color:#FFFFFF !important; font-weight:850 !important; }
/* Strong contrast for all headings, copy, labels and captions */
h1,h2,h3,h4,h5,h6,p,li,span,label,strong,b,small { color:inherit; }
.stApp h1,.stApp h2,.stApp h3,.stApp h4 { color:#F8FAFC !important; letter-spacing:-.025em; font-weight:800; }
.stApp h2 { padding-bottom:.45rem; border-bottom:1px solid #29463B; }
.stMarkdown,.stMarkdown p,.stMarkdown li,.stMarkdown strong,.stMarkdown em { color:#F0F5FC !important; }
.stCaption, [data-testid="stCaptionContainer"] p { color:#D1E1D8 !important; }
/* Form controls */
input,textarea,[data-baseweb="select"] > div,[data-baseweb="input"] > div,
[data-testid="stDateInput"] input,[data-testid="stNumberInput"] input {
  background:#0A1511 !important; color:#F8FAFC !important; border-color:#355446 !important;
}
[data-testid="stTextInput"] label,[data-testid="stTextArea"] label,[data-testid="stNumberInput"] label,
[data-testid="stSelectbox"] label,[data-testid="stMultiSelect"] label,[data-testid="stDateInput"] label,
[data-testid="stFileUploader"] label,[data-testid="stCheckbox"] label,[data-testid="stRadio"] label,
[data-testid="stSlider"] label,[data-testid="stForm"] label { color:#E8F5ED !important; font-weight:700 !important; }
input::placeholder,textarea::placeholder { color:#B4C9BE !important; opacity:1 !important; }
[data-baseweb="popover"] *,[data-baseweb="menu"] *,[role="listbox"] *,[role="option"] * { color:#F8FAFC !important; }
[data-baseweb="popover"] > div,[data-baseweb="menu"] { background:#12231C !important; border:1px solid #345446 !important; }
[data-testid="stAlert"] { background:#13251E !important; border:1px solid #355446 !important; color:#F8FAFC !important; border-radius:12px; }
[data-testid="stExpander"] { background:#101827 !important; border:1px solid #2C4A3D !important; border-radius:14px !important; }
[data-testid="stExpander"] summary,[data-testid="stExpander"] summary p { color:#F8FAFC !important; font-weight:750 !important; }
[data-testid="stTabs"] button,[data-testid="stTabs"] button p { color:#E6F5EB !important; font-weight:750 !important; }
[data-testid="stTabs"] button[aria-selected="true"] { color:#BBF7D0 !important; border-bottom-color:#34D399 !important; }
.stButton button,.stFormSubmitButton button,[data-testid="stDownloadButton"] button { border-radius:12px !important; font-weight:800 !important; min-height:2.75rem; background:#193128 !important; color:#F8FAFC !important; border:1px solid #3A5C4B !important; transition:all .18s ease; }
.stButton button:hover,.stFormSubmitButton button:hover,[data-testid="stDownloadButton"] button:hover { border-color:#34D399 !important; color:#FFFFFF !important; transform:translateY(-1px); box-shadow:0 6px 18px rgba(16,185,129,.18); }
.stButton button[kind="primary"],.stFormSubmitButton button[kind="primary"] { background:linear-gradient(120deg,#047857,#0F766E) !important; border:1px solid #34D399 !important; color:#FFFFFF !important; }
[data-testid="stRadio"] > div[role="radiogroup"] { display:flex; flex-wrap:wrap; gap:.4rem; background:#0C1914; border:1px solid #29483B; padding:.6rem; border-radius:16px; box-shadow:0 6px 18px rgba(0,0,0,.20); }
[data-testid="stRadio"] label { background:#172B22 !important; border-radius:11px; padding:.4rem .75rem; border:1px solid #2B493B !important; color:#F1F6FC !important; }
[data-testid="stRadio"] label:hover { border-color:#34D399 !important; background:#1D3A2E !important; }
[data-testid="stCheckbox"] [role="checkbox"] { border-color:#688779 !important; }
[data-testid="stProgress"] > div > div { background:#34D399 !important; }
[data-testid="stDataFrame"], [data-testid="stTable"] { border:1px solid #2D4058; border-radius:12px; overflow:hidden; }
.badge-air { background:#123C30; color:#B7F7D5 !important; padding:4px 10px; border-radius:10px; font-size:.8rem; font-weight:800; border:1px solid #287455; }
.badge-sea { background:#164633; color:#BBF7D0 !important; padding:4px 10px; border-radius:10px; font-size:.8rem; font-weight:800; border:1px solid #2D8A61; }
.badge-inc { background:#17442B; color:#BBF7D0 !important; padding:4px 9px; border-radius:8px; font-size:.78rem; font-weight:800; border:1px solid #398A59; }
.badge-hors { background:#4A2024; color:#FECACA !important; padding:4px 9px; border-radius:8px; font-size:.78rem; font-weight:800; border:1px solid #99434B; }
.whatsapp-btn { display:inline-block; background:linear-gradient(120deg,#16A34A,#047857); color:white !important; padding:11px 17px; border-radius:11px; text-decoration:none; font-weight:850; text-align:center; box-shadow:0 6px 16px rgba(16,185,129,.28); }
.sf-footer { margin-top:3rem; padding:1.8rem; border:1px solid #294866; border-radius:22px; background:linear-gradient(120deg,#07110F,#17372C 60%,#116149); color:white !important; text-align:center; box-shadow:0 15px 32px rgba(0,0,0,.35); }
.sf-footer h3 { color:#FFFFFF !important; margin:0 0 .55rem; }
.sf-footer p { color:#E0F2FE !important; margin:.3rem 0; }
.sf-feature { display:inline-block; margin:.35rem .4rem; padding:.48rem .8rem; border:1px solid rgba(167,243,208,.32); border-radius:30px; background:rgba(6,55,40,.65); color:#F0FFF7 !important; font-weight:700; }
hr { border-color:#29463B !important; }
a { color:#BBF7D0 !important; }
*:focus-visible { outline:2px solid #34D399 !important; outline-offset:2px; }
/* RESPONSIVE DESIGN: mobile, tablet and desktop */
/* Tablet: reduce gutters and allow column layouts to wrap naturally */
@media (max-width: 1024px) {
  .block-container { padding-left:1.25rem !important; padding-right:1.25rem !important; }
  .main-header { padding:1.6rem; border-radius:20px; }
  [data-testid="stHorizontalBlock"] { flex-wrap:wrap !important; gap: .8rem !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="column"] { min-width:min(260px, 100%) !important; flex:1 1 260px !important; }
  [data-testid="stMetric"] { padding:.85rem !important; }
}
/* Phones */
@media (max-width: 640px) {
  .block-container { padding: .65rem .7rem 1.4rem !important; max-width:100vw !important; }
  .main-header { padding:1rem .95rem !important; border-radius:15px !important; margin-bottom:.75rem !important; }
  .main-header h1 { font-size:clamp(1.25rem, 6vw, 1.7rem) !important; line-height:1.2 !important; }
  .main-header p { font-size:.92rem !important; line-height:1.4 !important; }
  .main-header:after { width:110px; height:110px; right:-35px; top:-30px; box-shadow:0 0 0 18px rgba(255,255,255,.035),0 0 0 36px rgba(255,255,255,.02); }
  [data-testid="stHorizontalBlock"] { flex-direction:column !important; gap:.55rem !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="column"] { width:100% !important; min-width:100% !important; flex:1 1 100% !important; }
  [data-testid="stMetric"] { padding:.7rem .8rem !important; border-radius:12px !important; }
  [data-testid="stMetricValue"] { font-size:1.35rem !important; }
  .card { padding:.8rem !important; border-radius:14px !important; }
  .banner-box { padding:.75rem .8rem !important; font-size:.92rem !important; }
  [data-testid="stRadio"] > div[role="radiogroup"] { gap:.35rem !important; padding:.45rem !important; }
  [data-testid="stRadio"] label { padding:.45rem .55rem !important; font-size:.88rem !important; }
  .stButton button,.stFormSubmitButton button,[data-testid="stDownloadButton"] button { width:100% !important; min-height:2.85rem !important; white-space:normal !important; }
  input,textarea,[data-baseweb="select"] > div { font-size:16px !important; } /* prevents iOS zoom */
  [data-testid="stTabs"] [role="tab"] { padding:.45rem .6rem !important; white-space:normal !important; }
  [data-testid="stDataFrame"], [data-testid="stTable"] { max-width:100% !important; overflow-x:auto !important; }
  img,video { max-width:100% !important; height:auto !important; }
  .whatsapp-btn { display:block !important; width:100% !important; box-sizing:border-box; padding:12px !important; }
  .sf-footer { padding:1rem .8rem !important; border-radius:15px !important; margin-top:1.5rem !important; }
  .sf-feature { margin:.25rem .15rem !important; padding:.4rem .65rem !important; font-size:.85rem !important; }
  h1,h2,h3 { overflow-wrap:anywhere; }
}
/* Small screens / extra narrow phones */
@media (max-width: 380px) {
  .block-container { padding-left:.45rem !important; padding-right:.45rem !important; }
  .main-header h1 { font-size:1.22rem !important; }
  [data-testid="stRadio"] label { font-size:.8rem !important; padding:.35rem .4rem !important; }
}
/* PREMIUM FINISH — refined surfaces, depth, motion and spacing */
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap');
/* Apply premium font to content, but preserve Streamlit's icon font. */
.stApp, .stApp *:not([data-testid="stIconMaterial"]):not(.material-symbols-rounded):not(.material-icons):not([class*="material-symbols"]):not([class*="material-icons"]) { font-family:'Manrope', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
/* Prevent icon ligatures from rendering as raw text or overlapping button labels. */
[data-testid="stIconMaterial"], .material-symbols-rounded, .material-icons, [class*="material-symbols"], [class*="material-icons"] { font-family:'Material Symbols Rounded','Material Icons' !important; font-weight:normal !important; font-style:normal !important; letter-spacing:normal !important; text-transform:none !important; line-height:1 !important; white-space:nowrap !important; word-wrap:normal !important; direction:ltr !important; -webkit-font-feature-settings:'liga' !important; font-feature-settings:'liga' !important; }
/* Hide decorative icons inside form-submit buttons for a clean, stable label. */
.stFormSubmitButton button [data-testid="stIconMaterial"], .stFormSubmitButton button .material-symbols-rounded, .stFormSubmitButton button .material-icons { display:none !important; }
.block-container { padding-left:clamp(1rem,3vw,2.6rem) !important; padding-right:clamp(1rem,3vw,2.6rem) !important; }
.main-header { background:linear-gradient(125deg,rgba(5,15,12,.98),rgba(16,54,40,.96) 58%,rgba(10,100,70,.93)) !important; border-color:rgba(246,211,101,.32) !important; }
.main-header:before { content:"SF GROUPAGE  •  LOGISTIQUE INTERNATIONALE"; display:block; font-size:.72rem; letter-spacing:.19em; font-weight:800; color:#F6D365; margin-bottom:.85rem; }
.card, div[data-testid="stMetric"], [data-testid="stExpander"], [data-testid="stForm"] { backdrop-filter:blur(12px); }
.card { border-color:rgba(167,243,208,.15) !important; transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease; }
.card:hover { transform:translateY(-2px); border-color:rgba(246,211,101,.45) !important; box-shadow:0 16px 34px rgba(0,0,0,.36); }
div[data-testid="stMetric"] { border-color:rgba(246,211,101,.18) !important; position:relative; overflow:hidden; }
div[data-testid="stMetric"]:after { content:""; position:absolute; top:0; left:0; width:100%; height:2px; background:linear-gradient(90deg,#16A34A,#F6D365,transparent); opacity:.8; }
[data-testid="stTabs"] [role="tab"] { border-radius:10px 10px 0 0; transition:all .16s ease; }
[data-testid="stTabs"] [role="tab"]:hover { background:rgba(52,211,153,.09); }
.stButton button,.stFormSubmitButton button,[data-testid="stDownloadButton"] button { box-shadow:0 4px 14px rgba(0,0,0,.18); letter-spacing:.01em; }
.stButton button[kind="primary"],.stFormSubmitButton button[kind="primary"] { background:linear-gradient(135deg,#0C8A5B,#087567) !important; box-shadow:0 7px 22px rgba(16,185,129,.2); }
[data-testid="stAlert"] { box-shadow:0 8px 20px rgba(0,0,0,.16); }
[data-testid="stDataFrame"] { background:#0D1914; border:1px solid rgba(167,243,208,.18) !important; }
.sf-footer { border-color:rgba(246,211,101,.28) !important; }
.sf-feature { backdrop-filter:blur(8px); }
@media (prefers-reduced-motion: reduce) { *,*:before,*:after { animation:none !important; transition:none !important; scroll-behavior:auto !important; } }
@media (max-width:640px) { .main-header:before { font-size:.62rem; letter-spacing:.12em; } .card:hover { transform:none; } }
</style>
""", unsafe_allow_html=True)

if "user" not in st.session_state:
    st.session_state.user = None

def get_whatsapp_link(message="Bonjour SF Groupage Chine – Saint-Louis, je souhaite avoir des informations."):
    phone = get_setting("whatsapp").replace(" ", "").replace("+", "")
    if not phone.startswith("221"):
        phone = "221" + phone
    return f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"

site_name = get_setting("site_name")
slogan = get_setting("slogan")
whatsapp_num = get_setting("whatsapp")
banner = get_setting("banner_announcement")

st.markdown(f"""
<div class="main-header">
    <h1>🚢 ✈️ {site_name}</h1>
    <p>« {slogan} » — 📞 WhatsApp / Tél : {whatsapp_num}</p>
</div>
""", unsafe_allow_html=True)

if banner:
    st.markdown(f'<div class="banner-box">{banner}</div>', unsafe_allow_html=True)

# ==========================================
# NAVIGATION PRINCIPALE — BARRE EN HAUT
# ==========================================
if not st.session_state.user:
    menu = ["🏠 Accueil", "✈️ Groupage Aérien", "🚢 Groupage Maritime", "🛍️ Catalogue à valider",
            "📝 Soumettre un produit", "💰 Tarifs & Calculateur", "ℹ️ Comment ça marche & FAQ", "📞 Contact & CGU", "🔐 Connexion / Inscription"]
elif st.session_state.user['role'] == 'client':
    menu = ["📊 Mon Tableau de Bord", "📝 Soumettre un produit", "🛍️ Catalogue à valider", "✈️ Groupage Aérien",
            "🚢 Groupage Maritime", "💰 Tarifs & Calculateur", "📦 Mes Demandes & Commandes", "💳 Mes Paiements & Reçus",
            "🚚 Suivi de mes Colis", "🔔 Notifications & Profil", "🏠 Accueil Vitrine", "📞 Contact & CGU"]
elif st.session_state.user['role'] == 'agent':
    menu = ["📦 Espace Agent Logistique", "🏠 Accueil Vitrine"]
else:
    menu = ["🛠️ Admin : Vue d'ensemble", "📋 Admin : Demandes Clients", "📦 Admin : Commandes & Suivi",
            "🛍️ Admin : Catalogue Produits", "🚢 Admin : Vagues de Groupage", "🚚 Admin : Gestion des Colis",
            "💰 Admin : Paiements & Finances", "👥 Admin : Clients & Équipe", "⚙️ Admin : Contenus & Paramètres",
            "🏠 Aperçu Site Public"]

if st.session_state.user:
    u = st.session_state.user
    top_left, top_right = st.columns([5,1])
    with top_left:
        st.caption(f"👤 {u['full_name']}  ·  Espace {u['role'].upper()}  ·  {u['city']}")
    with top_right:
        if st.button("🚪 Déconnexion", use_container_width=True):
            st.session_state.user = None
            st.rerun()

# Appliquer une navigation demandée AVANT de créer le widget radio.
# Cela évite StreamlitWidgetAlreadyInstantiatedError lorsqu'un bouton
# demande un changement de page pendant une exécution.
if "next_navigation" in st.session_state:
    requested_page = st.session_state.pop("next_navigation")
    if requested_page in menu:
        st.session_state["top_navigation"] = requested_page

# La liste des pages varie selon le rôle : garantir que la valeur mémorisée
# appartient toujours aux options actuellement disponibles.
if st.session_state.get("top_navigation") not in menu:
    st.session_state["top_navigation"] = menu[0]

choice = st.radio("Navigation", menu, horizontal=True, label_visibility="collapsed", key="top_navigation")
st.markdown("---")
st.markdown(f'<a href="{get_whatsapp_link()}" target="_blank" class="whatsapp-btn">💬 Assistance WhatsApp · {whatsapp_num}</a>', unsafe_allow_html=True)

def render_product_card(prod, conn):
    st.markdown('<div class="card">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 2.5])
    with c1:
        if prod['image_data']:
            st.image(prod['image_data'], use_container_width=True)
        else:
            st.image("https://cdn-icons-png.flaticon.com/512/679/679821.png", width=120)
    with c2:
        m_badge = '<span class="badge-air">✈️ AÉRIEN</span>' if prod['mode'] == 'Aerien' else '<span class="badge-sea">🚢 MARITIME</span>'
        t_badge = '<span class="badge-inc">✅ TRANSPORT INCLUS</span>' if prod['transport_included'] == 1 else '<span class="badge-hors">⚠️ HORS TRANSPORT</span>'
        st.markdown(f"#### {prod['title']} &nbsp; {m_badge} &nbsp; {t_badge}", unsafe_allow_html=True)
        st.write(prod['description'])
        if prod['specs']:
            st.caption(f"**Caractéristiques :** {prod['specs']}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Prix unitaire", f"{prod['price_fcfa']:,.0f} FCFA")
        m2.metric("Qté min.", f"{prod['min_qty']}")
        m3.metric("Délai indicatif", prod['est_delay'])
        st.info(f"**Transport :** {prod['transport_details']} | **Clôture/Limite :** {prod['deadline']}")

        with st.expander("🛒 Valider ma commande"):
            if not st.session_state.user:
                st.warning("Connectez-vous à votre compte client pour valider cette commande.")
            else:
                with st.form(key=f"form_cmd_{prod['id']}"):
                    qty = st.number_input("Quantité", min_value=int(prod['min_qty']), value=int(prod['min_qty']), step=1)
                    variants = st.text_input("Couleur, taille ou variante", placeholder="Ex: Noir, Taille M")
                    total_items = prod['price_fcfa'] * qty
                    st.markdown("**Récapitulatif avant confirmation :**")
                    st.write(f"- Articles ({qty} x {prod['price_fcfa']:,.0f} FCFA) : **{total_items:,.0f} FCFA** (montant confirmé)")
                    st.write(f"- Mode & Délai estimé : {prod['mode']} ({prod['est_delay']})")
                    if prod['transport_included'] == 1:
                        st.write("- Transport : **Inclus** selon conditions précisées")
                        st.write(f"- **Total connu :** **{total_items:,.0f} FCFA**")
                    else:
                        st.write("- Transport : **Hors transport** (calculé séparément / à l'arrivée au Sénégal)")
                        st.write(f"- **Total connu (hors fret) :** **{total_items:,.0f} FCFA**")
                    agree = st.checkbox("Je confirme le prix, les conditions de transport et le caractère indicatif des délais.")
                    if st.form_submit_button("✅ Valider ma commande"):
                        if not agree:
                            st.error("Veuillez cocher la case de confirmation.")
                        else:
                            ref = generate_ref("CMD")
                            now = datetime.now().strftime("%Y-%m-%d %H:%M")
                            cur = conn.cursor()
                            cur.execute('''INSERT INTO orders (ref, user_id, source_type, source_id, product_title, quantity, variants, mode, batch_id, items_price, transport_fee, total_amount, transport_included, status, created_at)
                                           VALUES (?, ?, 'catalogue', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'Commande validée', ?)''',
                                        (ref, st.session_state.user['id'], prod['id'], prod['title'], qty, variants, prod['mode'], prod['batch_id'], total_items, total_items, prod['transport_included'], now))
                            oid = cur.lastrowid
                            conn.commit()
                            log_order_event(oid, "Commande validée", f"Commande créée ({qty} unités).", st.session_state.user['full_name'])
                            add_notification(st.session_state.user['id'], f"Commande {ref} validée", f"Votre commande '{prod['title']}' est enregistrée.")
                            st.success(f"🎉 Commande **{ref}** enregistrée !")
    st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# PAGES PUBLIQUES
# ==========================================
if choice in ["🏠 Accueil", "🏠 Accueil Vitrine", "🏠 Aperçu Site Public"]:
    st.markdown(f"### {site_name}")
    st.write(get_setting("welcome_text"))
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<a href="{get_whatsapp_link()}" target="_blank" class="whatsapp-btn" style="width:100%;">📞 Contacter sur WhatsApp ({whatsapp_num})</a>', unsafe_allow_html=True)
    c2.info("📍 **Saint-Louis** — Livraison et accompagnement à **Dakar** et dans les **régions du Sénégal**.")
    c3.success("✨ **De la recherche au Sénégal** : Sourcing, groupage, suivi et réception.")

    st.markdown("---")
    a, m = st.columns(2)
    with a:
        st.markdown(f'<div class="card"><h3>✈️ Groupage Aérien</h3><p><strong>Délai indicatif :</strong> {get_setting("air_delay")}</p><p>{get_setting("air_rate_info")}</p></div>', unsafe_allow_html=True)
    with m:
        st.markdown(f'<div class="card"><h3>🚢 Groupage Maritime</h3><p><strong>Délai indicatif :</strong> {get_setting("sea_delay")}</p><p>{get_setting("sea_rate_info")}</p></div>', unsafe_allow_html=True)

    st.subheader("🔄 Étapes de fonctionnement")
    s1, s2, s3, s4 = st.columns(4)
    s1.info("**1. Inscription**\nCréez votre compte client.")
    s2.info("**2. Produit ou Lien**\nChoisissez un produit ou envoyez votre demande.")
    s3.info("**3. Validation & Paiement**\nValidez le récapitulatif et réglez.")
    s4.info("**4. Suivi & Livraison**\nSuivez votre colis jusqu'au Sénégal.")

    st.subheader("🛍️ Produits ouverts à la validation")
    conn = get_connection()
    prods = conn.execute("SELECT * FROM products WHERE available = 1 ORDER BY id DESC LIMIT 4").fetchall()
    for p in prods:
        render_product_card(p, conn)
    conn.close()

elif choice == "✈️ Groupage Aérien":
    st.header("✈️ Groupage Aérien")
    st.info(f"**Délai indicatif :** {get_setting('air_delay')}")
    st.success(f"**Tarifs & Transport :** {get_setting('air_rate_info')}")
    conn = get_connection()
    batches = conn.execute("SELECT * FROM batches WHERE mode='Aerien' AND status='Ouvert'").fetchall()
    for b in batches:
        st.write(f"- Vague **{b['code']}** | Clôture : **{b['close_date']}** | Arrivée estimée : {b['est_arrival']} | {b['instructions']}")
    for p in conn.execute("SELECT * FROM products WHERE available=1 AND mode='Aerien'").fetchall():
        render_product_card(p, conn)
    conn.close()

elif choice == "🚢 Groupage Maritime":
    st.header("🚢 Groupage Maritime")
    st.info(f"**Délai indicatif :** {get_setting('sea_delay')}")
    st.warning(f"**Modalités & Tarification :** {get_setting('sea_rate_info')}")
    conn = get_connection()
    batches = conn.execute("SELECT * FROM batches WHERE mode='Maritime' AND status='Ouvert'").fetchall()
    for b in batches:
        st.write(f"- Vague **{b['code']}** | Clôture : **{b['close_date']}** | Arrivée estimée : {b['est_arrival']} | {b['instructions']}")
    for p in conn.execute("SELECT * FROM products WHERE available=1 AND mode='Maritime'").fetchall():
        render_product_card(p, conn)
    conn.close()

elif choice == "🛍️ Catalogue à valider":
    st.header("🛍️ Catalogue des produits ouverts à la validation")
    mode_f = st.selectbox("Mode", ["Tous", "Aerien", "Maritime"])
    conn = get_connection()
    q = "SELECT * FROM products WHERE available=1" + (f" AND mode='{mode_f}'" if mode_f != "Tous" else "") + " ORDER BY id DESC"
    for p in conn.execute(q).fetchall():
        render_product_card(p, conn)
    conn.close()

elif choice == "📝 Soumettre un produit":
    st.header("📝 Proposer / Commander un produit")
    if not st.session_state.user:
        st.warning("Connectez-vous dans **🔐 Connexion / Inscription** pour soumettre un produit.")
    else:
        with st.form("submit_req"):
            c1, c2 = st.columns(2)
            p_name = c1.text_input("Nom ou description du produit *")
            p_link = c1.text_input("Lien du produit (1688, Alibaba, Taobao, Pinduoduo ou autre)")
            qty = c1.number_input("Quantité souhaitée *", min_value=1, value=1)
            variants = c1.text_input("Couleur, taille, modèle ou variante *")
            k_price = c1.text_input("Prix affiché et devise (si connus)")
            mode = c2.selectbox("Mode souhaité *", ["Aerien", "Maritime"])
            air_type = c2.selectbox("Formule aérienne", ["Standard (~15 jours)", "Express (~5 jours)"]) if mode == "Aerien" else "Maritime (délai à confirmer)"
            service_type = c2.selectbox("Service d’accompagnement", ["Aucun / déjà choisi", "Sourcing de produits (2 000 F CFA)", "Validation du panier (5 000 F CFA)", "Validation petites quantités (10 000 F CFA)", "Commande en gros (sur devis)"])
            city = c2.text_input("Ville de livraison au Sénégal *", value=st.session_state.user['city'])
            pkg = c2.text_input("Options / Emballage / Personnalisation")
            deadline = c2.date_input("Date limite éventuelle", value=None)
            photo = c2.file_uploader("Photo / capture d'écran", type=["png", "jpg", "jpeg"])
            comment = st.text_area("Commentaire complémentaire")
            if st.form_submit_button("📤 Envoyer la demande"):
                if not p_name or not variants or not city:
                    st.error("Champs obligatoires (*) manquants.")
                else:
                    ref = generate_ref("DEM")
                    now = datetime.now().strftime("%Y-%m-%d %H:%M")
                    conn = get_connection()
                    conn.execute('''INSERT INTO requests (ref, user_id, product_name, link, image_data, quantity, variants, known_price, packaging_options, delivery_city, mode, deadline, comment, status, created_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Nouvelle demande', ?)''',
                                 (ref, st.session_state.user['id'], p_name, p_link, photo.read() if photo else None, qty, variants, k_price, pkg, city, mode, str(deadline) if deadline else "", f"Service : {service_type}\nTransport : {air_type}\nCommentaire : {comment}", now))
                    conn.commit()
                    conn.close()
                    add_notification(st.session_state.user['id'], f"Demande {ref} envoyée", f"Demande '{p_name}' enregistrée.")
                    st.success(f"✅ Demande **{ref}** transmise à l'administrateur !")

elif choice == "💰 Tarifs & Calculateur":
    st.header("💰 Tarifs des services & estimation transport")
    st.caption("Tarifs indicatifs repris du guide de services. Les marchandises, frais de service et transport sont distincts.")
    st.subheader("🛍️ Services d’achat en Chine")
    fee1, fee2, fee3, fee4 = st.columns(4)
    fee1.metric("Sourcing", f"{int(get_setting('sourcing_fee') or 2000):,} F CFA".replace(",", " "))
    fee2.metric("Validation panier", f"{int(get_setting('validation_fee') or 5000):,} F CFA".replace(",", " "))
    fee3.metric("Petites quantités", f"{int(get_setting('small_order_fee') or 10000):,} F CFA".replace(",", " "))
    fee4.metric("Commandes en gros", "Sur devis")
    st.markdown("Recherche de produits et fournisseurs sur Alibaba, 1688, Pinduoduo, Taobao, AliExpress et autres plateformes. Vérification des articles, quantités et informations avant validation.")
    st.divider()
    st.subheader("✈️ Calcul estimatif du transport aérien")
    air_kind = st.radio("Formule", ["Standard (~15 jours)", "Express (~5 jours)"], horizontal=True)
    weight = st.number_input("Poids estimé (kg)", min_value=0.0, value=1.0, step=0.5)
    air_rate = int(get_setting('air_standard_rate') or 9000) if air_kind.startswith("Standard") else int(get_setting('air_express_rate') or 14000)
    st.metric("Estimation du fret aérien", f"{weight * air_rate:,.0f} F CFA".replace(",", " "), f"{air_rate:,} F CFA/kg".replace(",", " "))
    st.caption("Standard : environ 15 jours · Express : environ 5 jours. Délais indicatifs susceptibles de varier.")
    st.divider()
    st.subheader("🚢 Calcul estimatif du transport maritime")
    volume = st.number_input("Volume estimé (CBM / m³)", min_value=0.0, value=0.1, step=0.05, format="%.3f")
    sea_rate = int(get_setting('sea_rate_cbm') or 190000)
    st.metric("Estimation du fret maritime", f"{volume * sea_rate:,.0f} F CFA".replace(",", " "), f"{sea_rate:,} F CFA/CBM".replace(",", " "))
    st.caption("Le maritime est facturé selon le volume occupé, et non le poids. Délai à confirmer selon départ et acheminement.")
    st.info("À retenir : prix des marchandises, frais de service et transport sont distincts. Cette estimation ne constitue pas un devis ferme. Transmettez les liens/photos, quantités et mode d’expédition pour obtenir une estimation confirmée.")
    if st.button("📝 Soumettre une demande avec ces tarifs", use_container_width=True):
        # La demande est appliquée au prochain rerun, avant la création du radio.
        st.session_state["next_navigation"] = "📝 Soumettre un produit"
        st.rerun()

elif choice == "ℹ️ Comment ça marche & FAQ":
    st.header("ℹ️ Comment ça marche & FAQ")
    st.markdown(get_setting("faq_text"))

elif choice == "📞 Contact & CGU":
    st.header("📞 Contact & Conditions Générales")
    st.write(f"**Entreprise :** {site_name}")
    st.write(f"**WhatsApp / Téléphone :** {whatsapp_num}")
    st.write(f"**E-mail :** {get_setting('email')}")
    st.write(f"**Adresse :** {get_setting('address')}")
    st.markdown("---")
    st.subheader("Conditions Générales & Politique de Confidentialité")
    st.write(get_setting("cgu_text"))

# ==========================================
# CONNEXION / INSCRIPTION
# ==========================================
elif choice == "🔐 Connexion / Inscription":
    t1, t2, t3 = st.tabs(["🔑 Connexion", "📝 Inscription", "❓ Mot de passe oublié"])
    with t1:
        with st.form("login_f"):
            ph = st.text_input("Nom d’utilisateur ou numéro WhatsApp")
            pw = st.text_input("Mot de passe", type="password")
            if st.form_submit_button("Se connecter"):
                conn = get_connection()
                u = conn.execute("SELECT * FROM users WHERE (whatsapp=? OR username=?) AND password_hash=?", (ph.strip(), ph.strip(), hash_password(pw))).fetchone()
                conn.close()
                if u and u['status'] == 'actif':
                    st.session_state.user = dict(u)
                    st.rerun()
                elif u and u['status'] != 'actif':
                    st.error("Compte suspendu.")
                else:
                    st.error("Identifiants invalides.")
        with st.expander("Comptes de test"):
            st.write("Administrateur : identifiant `sambafall` | mot de passe `samba2026` (à changer après première connexion).")
    with t2:
        with st.form("reg_f"):
            name = st.text_input("Prénom et nom *")
            phone = st.text_input("Numéro WhatsApp *")
            city = st.selectbox("Ville / région *", ["Saint-Louis", "Dakar", "Thiès", "Touba / Diourbel", "Kaolack", "Ziguinchor", "Louga", "Autres régions"])
            addr = st.text_input("Adresse de livraison (facultatif)")
            email = st.text_input("E-mail (facultatif)")
            comp = st.text_input("Nom de boutique / entreprise (facultatif)")
            p1 = st.text_input("Mot de passe *", type="password")
            p2 = st.text_input("Confirmation mot de passe *", type="password")
            cgu = st.checkbox("J'accepte les conditions de service *")
            if st.form_submit_button("Créer mon compte"):
                if not name or not phone or not p1 or p1 != p2 or not cgu:
                    st.error("Vérifiez les champs obligatoires, la confirmation du mot de passe et l'acceptation des conditions.")
                else:
                    conn = get_connection()
                    try:
                        conn.execute('''INSERT INTO users (role, full_name, whatsapp, city, address, email, company, password_hash, status, created_at)
                                        VALUES ('client', ?, ?, ?, ?, ?, ?, ?, 'actif', ?)''',
                                     (name.strip(), phone.strip(), city, addr, email, comp, hash_password(p1), datetime.now().strftime("%Y-%m-%d %H:%M")))
                        conn.commit()
                        st.success("Compte créé ! Connectez-vous dans l'onglet Connexion.")
                    except Exception:
                        st.error("Ce numéro WhatsApp existe déjà.")
                    finally:
                        conn.close()
    with t3:
        fp = st.text_input("Numéro WhatsApp du compte")
        st.markdown(f'<a href="{get_whatsapp_link(f"Bonjour SF Groupage, réinitialisation mot de passe pour {fp}")}" target="_blank" class="whatsapp-btn">📲 Contacter le support WhatsApp</a>', unsafe_allow_html=True)

# ==========================================
# ESPACE CLIENT
# ==========================================
elif st.session_state.user and st.session_state.user['role'] == 'client':
    uid = st.session_state.user['id']
    conn = get_connection()

    if choice == "📊 Mon Tableau de Bord":
        st.header(f"Tableau de bord — {st.session_state.user['full_name']}")
        c1, c2, c3 = st.columns(3)
        c1.metric("Demandes", conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=?", (uid,)).fetchone()[0])
        c2.metric("Commandes", conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0])
        c3.metric("Colis", conn.execute("SELECT COUNT(*) FROM parcels p JOIN orders o ON p.order_id=o.id WHERE o.user_id=?", (uid,)).fetchone()[0])
        for n in conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall():
            st.info(f"**{n['title']}** ({n['created_at']}) : {n['message']}")

    elif choice == "📦 Mes Demandes & Commandes":
        td, tc = st.tabs(["Mes Demandes", "Mes Commandes"])
        with td:
            for r in conn.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall():
                with st.expander(f"{r['ref']} — {r['product_name']} [{r['status']}]"):
                    st.write(f"Quantité: {r['quantity']} | Variantes: {r['variants']} | Mode: {r['mode']}")
                    if r['admin_notes']:
                        st.warning(f"Réponse SF : {r['admin_notes']}")
                    if r['status'] in ['Devis envoyé', 'En attente de validation']:
                        frais_trans = 0 if r['transport_included'] else r['admin_transport_fee']
                        tot = r['admin_quote_price'] + frais_trans
                        st.write(f"Articles : {r['admin_quote_price']:,.0f} FCFA | Transport : {'Inclus' if r['transport_included'] else f'{frais_trans:,.0f} FCFA (Hors transport si 0)'} | **Total connu : {tot:,.0f} FCFA**")
                        if st.button("✅ Valider ce devis", key=f"v_{r['id']}"):
                            cref = generate_ref("CMD")
                            cur = conn.cursor()
                            cur.execute('''INSERT INTO orders (ref, user_id, source_type, source_id, product_title, quantity, variants, mode, items_price, transport_fee, total_amount, transport_included, status, created_at)
                                           VALUES (?, ?, 'demande', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Commande validée', ?)''',
                                        (cref, uid, r['id'], r['product_name'], r['quantity'], r['variants'], r['mode'], r['admin_quote_price'], frais_trans, tot, r['transport_included'], datetime.now().strftime("%Y-%m-%d %H:%M")))
                            oid = cur.lastrowid
                            conn.execute("UPDATE requests SET status='Commande passée' WHERE id=?", (r['id'],))
                            conn.commit()
                            log_order_event(oid, "Commande validée", "Devis validé par le client.", st.session_state.user['full_name'])
                            st.rerun()
        with tc:
            for o in conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall():
                paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE order_id=? AND status='Confirmé'", (o['id'],)).fetchone()[0]
                reste = max(0, o['total_amount'] - paid)
                with st.expander(f"{o['ref']} — {o['product_title']} [{o['status']}]"):
                    st.write(f"**Total connu :** {o['total_amount']:,.0f} FCFA | **Payé :** {paid:,.0f} FCFA | **Reste :** {reste:,.0f} FCFA | **Échéancier :** {o['payment_schedule']}")
                    for h in conn.execute("SELECT * FROM order_history WHERE order_id=? ORDER BY id ASC", (o['id'],)).fetchall():
                        st.write(f"- `{h['created_at']}` **{h['status']}** : {h['description']}")

    elif choice == "💳 Mes Paiements & Reçus":
        st.header("💳 Paiements & Reçus")
        st.info(get_setting("payment_methods"))
        ords = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
        if ords:
            with st.form("pay_f"):
                omap = {f"{o['ref']} - {o['product_title']} ({o['total_amount']:,.0f} FCFA)": o['id'] for o in ords}
                s_o = st.selectbox("Commande", list(omap.keys()))
                amt = st.number_input("Montant payé (FCFA)", min_value=500, step=500)
                pdt = st.date_input("Date de paiement")
                meth = st.selectbox("Moyen de paiement", ["Wave", "Orange Money", "Virement bancaire", "Espèces"])
                proof = st.file_uploader("Capture / reçu", type=["png", "jpg", "jpeg", "pdf"])
                if st.form_submit_button("Envoyer"):
                    pref = generate_ref("PAY")
                    conn.execute('''INSERT INTO payments (ref, order_id, user_id, amount, payment_date, method, proof_data, status, created_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, 'En attente de vérification', ?)''',
                                 (pref, omap[s_o], uid, amt, str(pdt), meth, proof.read() if proof else None, datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success(f"Paiement {pref} envoyé !")
        for p in conn.execute("SELECT p.*, o.ref as oref FROM payments p JOIN orders o ON p.order_id=o.id WHERE p.user_id=? ORDER BY p.id DESC", (uid,)).fetchall():
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{p['ref']}** (Cmd `{p['oref']}`) : {p['amount']:,.0f} FCFA via {p['method']} — **{p['status']}**")
            if p['status'] == 'Confirmé':
                txt = f"RECU {p['ref']} - {site_name}\nClient: {st.session_state.user['full_name']}\nCommande: {p['oref']}\nMontant: {p['amount']:,.0f} FCFA\nMoyen: {p['method']}\nStatut: CONFIRME"
                c2.download_button("📥 Reçu", data=txt, file_name=f"Recu_{p['ref']}.txt", key=f"r_{p['id']}")

    elif choice == "🚚 Suivi de mes Colis":
        st.header("🚚 Suivi de mes Colis")
        for p in conn.execute("SELECT p.*, o.ref as oref, o.status as ost FROM parcels p JOIN orders o ON p.order_id=o.id WHERE o.user_id=? ORDER BY p.id DESC", (uid,)).fetchall():
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.write(f"**Colis `{p['ref']}`** (Cmd `{p['oref']}` — Statut : **{p['ost']}**) | Poids réel : **{p['real_weight_kg']} kg** | Cartons : {p['carton_count']} | Frais : {p['applicable_fees']:,.0f} FCFA")
            if p['photo_data']:
                st.image(p['photo_data'], width=220)
            st.markdown('</div>', unsafe_allow_html=True)

    elif choice == "🔔 Notifications & Profil":
        tn, tp = st.tabs(["Notifications", "Profil"])
        with tn:
            conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
            conn.commit()
            for n in conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall():
                st.write(f"- `{n['created_at']}` **{n['title']}** : {n['message']}")
        with tp:
            u = st.session_state.user
            with st.form("prof_f"):
                nm = st.text_input("Prénom et nom", value=u['full_name'])
                ct = st.text_input("Ville / région", value=u['city'])
                ad = st.text_input("Adresse", value=u['address'] or "")
                em = st.text_input("E-mail", value=u['email'] or "")
                cp = st.text_input("Boutique / entreprise", value=u['company'] or "")
                pw = st.text_input("Nouveau mot de passe", type="password")
                if st.form_submit_button("Mettre à jour"):
                    if pw:
                        conn.execute("UPDATE users SET full_name=?, city=?, address=?, email=?, company=?, password_hash=? WHERE id=?", (nm, ct, ad, em, cp, hash_password(pw), uid))
                    else:
                        conn.execute("UPDATE users SET full_name=?, city=?, address=?, email=?, company=? WHERE id=?", (nm, ct, ad, em, cp, uid))
                    conn.commit()
                    st.session_state.user = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
                    st.success("Profil mis à jour !")
                    st.rerun()

    conn.close()

# ==========================================
# ESPACE AGENT LOGISTIQUE
# ==========================================
elif st.session_state.user and st.session_state.user['role'] == 'agent':
    st.header("📦 Espace Agent Logistique")
    conn = get_connection()
    ords = conn.execute("SELECT o.*, u.full_name FROM orders o JOIN users u ON o.user_id=u.id ORDER BY o.id DESC").fetchall()
    if ords:
        omap = {f"{o['ref']} - {o['full_name']} ({o['product_title']}) [{o['status']}]": o for o in ords}
        o = omap[st.selectbox("Commande", list(omap.keys()))]
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Avancement du suivi")
            sts = ["Commande validée", "Achat fournisseur en cours", "Acheté", "Colis reçu à l’entrepôt en Chine", "En préparation", "Colis regroupé", "Départ maritime", "Expédié", "En transit", "Arrivé au Sénégal", "Disponible pour retrait/livraison", "Livré"]
            nst = st.selectbox("Statut", sts)
            desc = st.text_input("Note de suivi")
            if st.button("Mettre à jour le statut"):
                conn.execute("UPDATE orders SET status=? WHERE id=?", (nst, o['id']))
                conn.commit()
                log_order_event(o['id'], nst, desc, st.session_state.user['full_name'])
                add_notification(o['user_id'], f"Suivi {o['ref']}", f"Nouveau statut : {nst}. {desc}")
                st.success("Statut mis à jour !")
                st.rerun()
        with c2:
            st.subheader("Enregistrer un colis")
            with st.form("ag_col"):
                tr = st.text_input("Référence suivi")
                ct = st.number_input("Cartons", min_value=1, value=1)
                wg = st.number_input("Poids réel (kg)", min_value=0.0, step=0.1)
                ph = st.file_uploader("Photo colis", type=["png", "jpg", "jpeg"])
                nt = st.text_input("Remarques")
                if st.form_submit_button("Enregistrer colis"):
                    cref = generate_ref("COL")
                    conn.execute('''INSERT INTO parcels (ref, order_id, tracking_code, carton_count, real_weight_kg, photo_data, notes, updated_by, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                 (cref, o['id'], tr, ct, wg, ph.read() if ph else None, nt, st.session_state.user['full_name'], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success(f"Colis {cref} enregistré !")
    conn.close()

# ==========================================
# ESPACE ADMINISTRATEUR
# ==========================================
elif st.session_state.user and st.session_state.user['role'] == 'admin':
    conn = get_connection()
    adm_name = st.session_state.user['full_name']

    if choice == "🛠️ Admin : Vue d'ensemble":
        st.header("🛠️ Tableau de Bord Administrateur")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Clients", conn.execute("SELECT COUNT(*) FROM users WHERE role='client'").fetchone()[0])
        c2.metric("Demandes", conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        c3.metric("Commandes", conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
        c4.metric("Paiements en attente", conn.execute("SELECT COUNT(*) FROM payments WHERE status='En attente de vérification'").fetchone()[0])

        st.subheader("📲 Modèles de messages WhatsApp")
        tpls = {
            "Demande reçue": "Bonjour {client}, votre demande {ref} est bien reçue chez SF Groupage Chine – Saint-Louis.",
            "Devis disponible": "Bonjour {client}, votre proposition/devis pour {ref} est disponible dans votre espace client.",
            "Paiement confirmé": "Bonjour {client}, votre paiement pour {ref} est confirmé. Merci pour votre confiance.",
            "Colis prêt / arrivé": "Bonjour {client}, votre colis ({ref}) est arrivé au Sénégal. Contactez-nous pour retrait ou livraison."
        }
        mt = st.selectbox("Modèle", list(tpls.keys()))
        cnm = st.text_input("Nom client", "Client")
        crf = st.text_input("Référence", "CMD-2611-XXXXX")
        cph = st.text_input("WhatsApp client", "77 123 45 67")
        msg = tpls[mt].format(client=cnm, ref=crf)
        st.text_area("Message", msg)
        p_clean = "221" + cph.replace(" ", "").replace("+", "")
        st.markdown(f'<a href="https://wa.me/{p_clean}?text={urllib.parse.quote(msg)}" target="_blank" class="whatsapp-btn">📤 Envoyer sur WhatsApp</a>', unsafe_allow_html=True)

    elif choice == "📋 Admin : Demandes Clients":
        st.header("📋 Demandes de produits clients")
        for r in conn.execute("SELECT r.*, u.full_name, u.whatsapp FROM requests r JOIN users u ON r.user_id=u.id ORDER BY r.id DESC").fetchall():
            with st.expander(f"{r['ref']} | {r['full_name']} ({r['whatsapp']}) — {r['product_name']} [{r['status']}]"):
                if r['image_data']:
                    st.image(r['image_data'], width=220)
                st.write(f"Lien : {r['link']} | Qté : {r['quantity']} | Variantes : {r['variants']} | Mode : {r['mode']} | Ville : {r['delivery_city']}")
                with st.form(f"ar_{r['id']}"):
                    sts = ["Nouvelle demande", "En vérification", "Informations complémentaires demandées", "Devis envoyé", "En attente de validation", "Validée", "Refusée"]
                    nst = st.selectbox("État", sts, index=sts.index(r['status']) if r['status'] in sts else 0)
                    qp = st.number_input("Prix proposé articles (FCFA)", value=float(r['admin_quote_price']), step=500.0)
                    ti = st.checkbox("Transport inclus ?", value=bool(r['transport_included']))
                    tf = st.number_input("Frais transport (FCFA)", value=float(r['admin_transport_fee']), step=500.0)
                    nt = st.text_area("Réponse / Précisions au client", value=r['admin_notes'] or "")
                    if st.form_submit_button("Enregistrer & Notifier"):
                        conn.execute("UPDATE requests SET status=?, admin_quote_price=?, transport_included=?, admin_transport_fee=?, admin_notes=? WHERE id=?",
                                     (nst, qp, int(ti), tf, nt, r['id']))
                        conn.commit()
                        add_notification(r['user_id'], f"Demande {r['ref']}", f"Statut : {nst}. {nt}")
                        st.success("Mis à jour !")
                        st.rerun()

    elif choice == "📦 Admin : Commandes & Suivi":
        st.header("📦 Commandes & Suivi")
        batches = conn.execute("SELECT id, code FROM batches").fetchall()
        bmap = {0: "Aucune"}
        for b in batches:
            bmap[b['id']] = b['code']
        for o in conn.execute("SELECT o.*, u.full_name FROM orders o JOIN users u ON o.user_id=u.id ORDER BY o.id DESC").fetchall():
            with st.expander(f"{o['ref']} | {o['full_name']} — {o['product_title']} (x{o['quantity']}) [{o['status']}]"):
                with st.form(f"ao_{o['id']}"):
                    sts = ["Commande validée", "Achat fournisseur en cours", "Acheté", "Colis reçu à l’entrepôt en Chine", "En préparation", "Colis regroupé", "Départ maritime", "Expédié", "En transit", "Arrivé au Sénégal", "Frais de transport communiqués", "Paiement en attente", "Disponible pour retrait/livraison", "Livré"]
                    nst = st.selectbox("Statut", sts, index=sts.index(o['status']) if o['status'] in sts else 0)
                    nb = st.selectbox("Vague de groupage", list(bmap.keys()), format_func=lambda x: bmap[x], index=list(bmap.keys()).index(o['batch_id']) if o['batch_id'] in bmap else 0)
                    sch = st.selectbox("Échéancier", ["1 tranche (100%)", "2 tranches (50% / 50%)", "Articles puis fret à l'arrivée"])
                    tf = st.number_input("Frais transport (FCFA)", value=float(o['transport_fee']), step=500.0)
                    ef = st.number_input("Frais complémentaires (FCFA)", value=float(o['extra_fee']), step=500.0)
                    note = st.text_input("Note interne / détail suivi", value=o['internal_note'] or "")
                    if st.form_submit_button("Mettre à jour"):
                        tot = o['items_price'] + tf + ef
                        conn.execute("UPDATE orders SET status=?, batch_id=?, payment_schedule=?, transport_fee=?, extra_fee=?, total_amount=?, internal_note=? WHERE id=?",
                                     (nst, nb if nb != 0 else None, sch, tf, ef, tot, note, o['id']))
                        conn.commit()
                        log_order_event(o['id'], nst, note or f"Statut : {nst}", adm_name)
                        add_notification(o['user_id'], f"Commande {o['ref']}", f"Nouveau statut : {nst}. Total : {tot:,.0f} FCFA.")
                        st.success("Commande mise à jour !")
                        st.rerun()

    elif choice == "🛍️ Admin : Catalogue Produits":
        st.header("🛍️ Catalogue Produits")
        with st.form("ap_f"):
            c1, c2 = st.columns(2)
            tt = c1.text_input("Titre *")
            ds = c1.text_area("Description *")
            sp = c1.text_input("Caractéristiques")
            pr = c1.number_input("Prix (FCFA) *", min_value=100, step=500)
            mq = c1.number_input("Quantité min.", min_value=1, value=1)
            md = c2.selectbox("Mode", ["Aerien", "Maritime"])
            wg = c2.number_input("Poids estimé (kg)", min_value=0.0, step=0.1)
            ti = c2.checkbox("Transport inclus ?")
            td = c2.text_input("Précision transport *", value="Transport inclus selon offre" if ti else "Hors transport : calculé à l'arrivée au Sénégal")
            dl = c2.date_input("Date limite")
            dy = c2.text_input("Délai estimé", value="Environ 15 jours" if md == "Aerien" else "Environ 2 mois")
            im = c2.file_uploader("Photo", type=["png", "jpg", "jpeg"])
            if st.form_submit_button("Ajouter le produit"):
                conn.execute('''INSERT INTO products (title, description, specs, price_fcfa, min_qty, est_weight_kg, mode, transport_included, transport_details, deadline, est_delay, available, image_data)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)''',
                             (tt, ds, sp, pr, mq, wg, md, int(ti), td, str(dl), dy, im.read() if im else None))
                conn.commit()
                st.success("Produit ajouté !")
                st.rerun()
        for p in conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall():
            c1, c2, c3 = st.columns([3, 1, 1])
            c1.write(f"**{p['title']}** ({p['mode']}) — {p['price_fcfa']:,.0f} FCFA | {'Visible' if p['available'] else 'Masqué'}")
            if c2.button("Masquer/Afficher", key=f"m_{p['id']}"):
                conn.execute("UPDATE products SET available=? WHERE id=?", (0 if p['available'] else 1, p['id']))
                conn.commit()
                st.rerun()
            if c3.button("Supprimer", key=f"d_{p['id']}"):
                conn.execute("DELETE FROM products WHERE id=?", (p['id'],))
                conn.commit()
                st.rerun()

    elif choice == "🚢 Admin : Vagues de Groupage":
        st.header("🚢 Vagues de Groupage")
        with st.form("nb_f"):
            c1, c2 = st.columns(2)
            cd = c1.text_input("Code / Nom de vague *")
            md = c2.selectbox("Mode", ["Aerien", "Maritime"])
            op = c1.date_input("Ouverture")
            cl = c2.date_input("Clôture")
            dp = c1.date_input("Départ estimé")
            ar = c2.date_input("Arrivée estimée")
            ins = st.text_area("Consignes")
            if st.form_submit_button("Créer vague"):
                conn.execute("INSERT INTO batches (code, mode, open_date, close_date, est_departure, est_arrival, instructions, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Ouvert')",
                             (cd, md, str(op), str(cl), str(dp), str(ar), ins))
                conn.commit()
                st.success("Vague créée !")
                st.rerun()
        for b in conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall():
            cnt = conn.execute("SELECT COUNT(*) FROM orders WHERE batch_id=?", (b['id'],)).fetchone()[0]
            st.write(f"- **{b['code']}** ({b['mode']}) | Clôture : {b['close_date']} | Arrivée : {b['est_arrival']} | Commandes : **{cnt}**")

    elif choice == "🚚 Admin : Gestion des Colis":
        st.header("🚚 Gestion des Colis")
        ords = conn.execute("SELECT o.id, o.ref, o.product_title, u.full_name FROM orders o JOIN users u ON o.user_id=u.id").fetchall()
        if ords:
            with st.form("ac_f"):
                omap = {f"{o['ref']} - {o['full_name']} ({o['product_title']})": o['id'] for o in ords}
                oid = omap[st.selectbox("Commande", list(omap.keys()))]
                tr = st.text_input("Référence suivi")
                ct = st.number_input("Cartons", min_value=1, value=1)
                wg = st.number_input("Poids réel (kg)", min_value=0.0, step=0.1)
                fe = st.number_input("Frais applicables (FCFA)", min_value=0.0, step=500.0)
                ph = st.file_uploader("Photo", type=["png", "jpg", "jpeg"])
                nt = st.text_input("Notes")
                if st.form_submit_button("Enregistrer"):
                    cref = generate_ref("COL")
                    conn.execute('''INSERT INTO parcels (ref, order_id, tracking_code, carton_count, real_weight_kg, applicable_fees, photo_data, notes, updated_by, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                 (cref, oid, tr, ct, wg, fe, ph.read() if ph else None, nt, adm_name, datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                    st.success(f"Colis {cref} enregistré !")
                    st.rerun()
        for p in conn.execute("SELECT p.*, o.ref as oref FROM parcels p JOIN orders o ON p.order_id=o.id ORDER BY p.id DESC").fetchall():
            st.write(f"- **{p['ref']}** (Cmd `{p['oref']}`) | Poids : {p['real_weight_kg']} kg | Cartons : {p['carton_count']} | Frais : {p['applicable_fees']:,.0f} FCFA | Par : {p['updated_by']} ({p['updated_at']})")

    elif choice == "💰 Admin : Paiements & Finances":
        st.header("💰 Finances & Paiements")
        tf = conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM orders").fetchone()[0]
        tc = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='Confirmé'").fetchone()[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Facturé", f"{tf:,.0f} FCFA")
        c2.metric("Confirmé", f"{tc:,.0f} FCFA")
        c3.metric("Solde restant", f"{max(0, tf-tc):,.0f} FCFA")

        df = pd.read_sql_query("SELECT o.ref, u.full_name, u.whatsapp, o.mode, o.items_price, o.transport_fee, o.extra_fee, o.total_amount, o.status FROM orders o JOIN users u ON o.user_id=u.id", conn)
        st.download_button("📥 Exporter CSV / Excel", data=df.to_csv(index=False).encode("utf-8-sig"), file_name="finances_sf_groupage.csv", mime="text/csv")

        for p in conn.execute("SELECT p.*, u.full_name, o.ref as oref FROM payments p JOIN users u ON p.user_id=u.id JOIN orders o ON p.order_id=o.id ORDER BY p.id DESC").fetchall():
            with st.expander(f"{p['ref']} | {p['full_name']} — {p['amount']:,.0f} FCFA ({p['method']}) [{p['status']}]"):
                if p['proof_data']:
                    # Les justificatifs peuvent être des images ou des PDF.
                    # Afficher les images et proposer le téléchargement des PDF.
                    if p['proof_data'].startswith(b"%PDF"):
                        st.download_button(
                            "📄 Télécharger le justificatif PDF",
                            data=p['proof_data'],
                            file_name=f"justificatif_{p['ref']}.pdf",
                            mime="application/pdf",
                            key=f"proof_pdf_{p['id']}"
                        )
                    else:
                        st.image(p['proof_data'], width=250)
                a, b = st.columns(2)
                if a.button("✅ Confirmer", key=f"ok_{p['id']}"):
                    conn.execute("UPDATE payments SET status='Confirmé', verified_by=? WHERE id=?", (adm_name, p['id']))
                    conn.commit()
                    add_notification(p['user_id'], f"Paiement {p['ref']} confirmé", f"Montant : {p['amount']:,.0f} FCFA.")
                    st.rerun()
                if b.button("❌ Rejeter", key=f"no_{p['id']}"):
                    conn.execute("UPDATE payments SET status='Rejeté', verified_by=? WHERE id=?", (adm_name, p['id']))
                    conn.commit()
                    st.rerun()

    elif choice == "👥 Admin : Clients & Équipe":
        st.header("👥 Clients & Équipe")
        sr = st.text_input("Recherche nom ou téléphone")
        for u in conn.execute("SELECT * FROM users WHERE full_name LIKE ? OR whatsapp LIKE ? ORDER BY id DESC", (f"%{sr}%", f"%{sr}%")).fetchall():
            st.write(f"- **{u['full_name']}** ({u['whatsapp']}) | {u['city']} | Rôle : `{u['role']}` | Statut : **{u['status']}**")
            if u['role'] != 'admin':
                nxt = 'suspendu' if u['status'] == 'actif' else 'actif'
                if st.button(f"Passer à '{nxt}'", key=f"u_{u['id']}"):
                    conn.execute("UPDATE users SET status=? WHERE id=?", (nxt, u['id']))
                    conn.commit()
                    st.rerun()

    elif choice == "⚙️ Admin : Contenus & Paramètres":
        st.header("⚙️ Paramètres & Contenus du site")
        with st.form("cms_all"):
            sn = st.text_input("Nom", value=get_setting("site_name"))
            sl = st.text_input("Slogan", value=get_setting("slogan"))
            wa = st.text_input("WhatsApp", value=get_setting("whatsapp"))
            em = st.text_input("E-mail", value=get_setting("email"))
            ad = st.text_input("Adresse", value=get_setting("address"))
            ba = st.text_input("Bannière", value=get_setting("banner_announcement"))
            wl = st.text_area("Texte d'accueil", value=get_setting("welcome_text"))
            ad_d = st.text_input("Délai Aérien", value=get_setting("air_delay"))
            ad_r = st.text_area("Infos Aérien", value=get_setting("air_rate_info"))
            sd_d = st.text_input("Délai Maritime", value=get_setting("sea_delay"))
            sd_r = st.text_area("Infos Maritime", value=get_setting("sea_rate_info"))
            pm = st.text_area("Moyens de paiement", value=get_setting("payment_methods"))
            fq = st.text_area("FAQ", value=get_setting("faq_text"))
            cg = st.text_area("CGU & Confidentialité", value=get_setting("cgu_text"))
            if st.form_submit_button("💾 Sauvegarder"):
                for k, v in {
                    "site_name": sn, "slogan": sl, "whatsapp": wa, "email": em, "address": ad,
                    "banner_announcement": ba, "welcome_text": wl, "air_delay": ad_d, "air_rate_info": ad_r,
                    "sea_delay": sd_d, "sea_rate_info": sd_r, "payment_methods": pm, "faq_text": fq, "cgu_text": cg
                }.items():
                    update_setting(k, v)
                st.success("Paramètres enregistrés !")
                st.rerun()

        st.markdown("---")
        st.subheader("📲 Notifications automatiques WhatsApp")
        st.caption("Les messages sont envoyés via l’API officielle WhatsApp Cloud de Meta. Il faut un compte Meta Business, un numéro configuré et un jeton API valide.")
        with st.form("whatsapp_notifications_config"):
            wa_on = st.checkbox("Activer les notifications WhatsApp automatiques", value=get_setting("wa_notifications_enabled") == "1")
            wa_phone_id = st.text_input("Phone Number ID (Meta)", value=get_setting("wa_phone_number_id"))
            wa_token = st.text_input("Access Token", value=get_setting("wa_access_token"), type="password", help="Token API WhatsApp Cloud. Il est stocké dans les paramètres SQLite de cette application.")
            wa_test_phone = st.text_input("Numéro pour le test (format Sénégal ou international)", value=get_setting("wa_test_phone"))
            st.markdown("**Choisir les notifications à envoyer :**")
            wa_orders = st.checkbox("Commandes / validation", value=get_setting("wa_notify_orders") == "1")
            wa_requests = st.checkbox("Demandes / devis", value=get_setting("wa_notify_requests") == "1")
            wa_payments = st.checkbox("Paiements", value=get_setting("wa_notify_payments") == "1")
            wa_tracking = st.checkbox("Colis / suivi / livraison", value=get_setting("wa_notify_tracking") == "1")
            wa_general = st.checkbox("Autres notifications", value=get_setting("wa_notify_general") == "1")
            if st.form_submit_button("💾 Enregistrer la configuration WhatsApp"):
                settings_to_save = {
                    "wa_notifications_enabled": "1" if wa_on else "0",
                    "wa_phone_number_id": wa_phone_id.strip(),
                    "wa_access_token": wa_token.strip(),
                    "wa_test_phone": wa_test_phone.strip(),
                    "wa_notify_orders": "1" if wa_orders else "0",
                    "wa_notify_requests": "1" if wa_requests else "0",
                    "wa_notify_payments": "1" if wa_payments else "0",
                    "wa_notify_tracking": "1" if wa_tracking else "0",
                    "wa_notify_general": "1" if wa_general else "0",
                }
                for key, value in settings_to_save.items():
                    update_setting(key, value)
                st.success("Configuration WhatsApp sauvegardée.")
                st.rerun()

        if st.button("📨 Envoyer un message de test WhatsApp", key="wa_test_button"):
            if get_setting("wa_notifications_enabled") != "1":
                st.warning("Active d’abord les notifications et sauvegarde la configuration.")
            else:
                test_msg = "✅ Test réussi ! Les notifications WhatsApp de SF GROUPAGE CHINE – SAINT-LOUIS sont configurées."
                ok, detail = send_whatsapp_message(get_setting("wa_test_phone"), test_msg)
                if ok:
                    st.success(detail)
                else:
                    st.error(detail)

    conn.close()

# ==========================================
# FOOTER PREMIUM — RASSURANCE & VALORISATION
# ==========================================
st.markdown(f"""
<div class="sf-footer">
    <h3>🚢 SF GROUPAGE CHINE · SAINT-LOUIS</h3>
    <p>Votre passerelle entre les fournisseurs en Chine et le Sénégal.</p>
    <div>
      <span class="sf-feature">✈️ Groupage aérien</span>
      <span class="sf-feature">🚢 Groupage maritime</span>
      <span class="sf-feature">🔎 Suivi de commande</span>
      <span class="sf-feature">🤝 Accompagnement personnalisé</span>
    </div>
    <p style="margin-top:1rem;">📍 Saint-Louis · Livraison Dakar & régions · 💬 {whatsapp_num}</p>
    <p style="font-size:.82rem;opacity:.85;">Qualité · Confiance · Transparence · Satisfaction</p>
</div>
""", unsafe_allow_html=True)

