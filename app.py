import os
import json
import sqlite3
from datetime import datetime

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session, g

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from xml.sax.saxutils import escape

from services.diagnosis import run_leak_investigation

try:
    from services.weather import get_weather_summary
except Exception:
    get_weather_summary = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "leaktrace.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


PLAN_RULES = {
    "starter": {"name": "Starter", "price": 99, "max_users": 3, "monthly_credits": 25},
    "pro": {"name": "Pro", "price": 199, "max_users": 6, "monthly_credits": 100},
    "business": {"name": "Business", "price": 399, "max_users": None, "monthly_credits": None},
}

CREDIT_PACKS = {
    "10": {"credits": 10, "price": 25},
    "25": {"credits": 25, "price": 50},
    "50": {"credits": 50, "price": 90},
}

PUBLIC_ENDPOINTS = {"login", "setup", "static"}

def month_key():
    return datetime.utcnow().strftime("%Y-%m")

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()
    conn.close()
    return user

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def owner_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "owner":
            flash("Owner access is required.", "warning")
            return redirect(url_for("company_dashboard"))
        return fn(*args, **kwargs)
    return wrapper

def user_count(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_active=1").fetchone()["c"]

def company_row(conn, company_id):
    return conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()

def plan_for(company):
    return PLAN_RULES.get((company["plan_code"] or "starter"), PLAN_RULES["starter"])

def usage_summary(conn, company_id):
    company = company_row(conn, company_id)
    plan = plan_for(company)
    used = conn.execute("""
        SELECT COUNT(*) AS c FROM usage_events
        WHERE company_id=? AND event_type='completed_ai_investigation' AND month_key=?
    """, (company_id, month_key())).fetchone()["c"]
    purchased = conn.execute("""
        SELECT COALESCE(SUM(credits_remaining), 0) AS c
        FROM credit_purchases WHERE company_id=?
    """, (company_id,)).fetchone()["c"]
    active_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE company_id=? AND is_active=1", (company_id,)).fetchone()["c"]
    included = plan["monthly_credits"]
    remaining = None if included is None else max(included - used, 0)
    allowed = True if included is None else (remaining > 0 or purchased > 0)
    return {"company": company, "plan": plan, "used": used, "included": included, "remaining_included": remaining, "purchased_remaining": purchased, "active_users": active_users, "allowed": allowed, "month_key": month_key()}

def consume_completed_investigation_credit(conn, company_id, user_id, investigation_id):
    summary = usage_summary(conn, company_id)
    if not summary["allowed"]:
        return False, "No investigation credits available"
    source = "included"
    purchase_id = None
    if summary["included"] is not None and summary["used"] >= summary["included"]:
        pack = conn.execute("SELECT * FROM credit_purchases WHERE company_id=? AND credits_remaining>0 ORDER BY purchased_at ASC LIMIT 1", (company_id,)).fetchone()
        if not pack:
            return False, "No purchased investigation credits available"
        purchase_id = pack["id"]
        source = "purchased"
        conn.execute("UPDATE credit_purchases SET credits_remaining=credits_remaining-1 WHERE id=?", (purchase_id,))
    conn.execute("""
        INSERT INTO usage_events (company_id, user_id, investigation_id, event_type, credit_source, credit_purchase_id, month_key)
        VALUES (?, ?, ?, 'completed_ai_investigation', ?, ?, ?)
    """, (company_id, user_id, investigation_id, source, purchase_id, month_key()))
    return True, source

def case_access(conn, investigation_id):
    user = current_user()
    if not user:
        return None
    return conn.execute("SELECT * FROM investigations WHERE id=? AND company_id=?", (investigation_id, user["company_id"])).fetchone()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, column_type):
    existing_cols = [
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]

    if column not in existing_cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def clean_text(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def pdf_text(value, default="Not provided"):
    value = clean_text(value, default)
    return escape(value.replace("\n", " "))


def build_case_filters(args):
    clauses = []
    params = []

    status = clean_text(args.get("status"))
    mode = clean_text(args.get("mode"))
    q = clean_text(args.get("q"))

    if status:
        clauses.append("COALESCE(status, 'New') = ?")
        params.append(status)

    if mode:
        clauses.append("mode = ?")
        params.append(mode)

    if q:
        like = f"%{q}%"
        clauses.append("""(
            case_number LIKE ? OR
            customer_name LIKE ? OR
            customer_phone LIKE ? OR
            customer_email LIKE ? OR
            property_address LIKE ? OR
            claim_number LIKE ? OR
            ai_source LIKE ?
        )""")
        params.extend([like, like, like, like, like, like, like])

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params, {"status": status, "mode": mode, "q": q}


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_number TEXT UNIQUE,
        mode TEXT NOT NULL,
        status TEXT DEFAULT 'New',
        priority TEXT DEFAULT 'Normal',
        assigned_to TEXT,

        customer_name TEXT,
        customer_phone TEXT,
        customer_email TEXT,
        customer_notes TEXT,
        property_name TEXT,
        insurance_company TEXT,
        claim_number TEXT,
        adjuster_name TEXT,
        adjuster_phone TEXT,
        adjuster_email TEXT,
        contractor_company TEXT,
        contractor_license TEXT,
        contractor_phone TEXT,
        contractor_email TEXT,
        report_notes TEXT,

        property_type TEXT,
        symptom_type TEXT,
        symptom_location TEXT,
        leak_timing TEXT,
        storm_context TEXT,

        property_address TEXT,
        property_lat REAL,
        property_lon REAL,
        weather_summary TEXT,
        weather_rainfall TEXT,
        weather_wind TEXT,
        weather_conditions TEXT,

        interior_room TEXT,
        nearest_wall TEXT,
        distance_from_wall TEXT,
        distance_from_corner TEXT,
        floor_level TEXT,

        interior_lat REAL,
        interior_lon REAL,
        interior_accuracy REAL,
        interior_heading REAL,

        roof_lat REAL,
        roof_lon REAL,
        roof_accuracy REAL,
        roof_heading REAL,

        cal_front_left_lat REAL,
        cal_front_left_lon REAL,
        cal_front_left_accuracy REAL,
        cal_front_right_lat REAL,
        cal_front_right_lon REAL,
        cal_front_right_accuracy REAL,
        cal_back_right_lat REAL,
        cal_back_right_lon REAL,
        cal_back_right_accuracy REAL,

        cal_back_left_lat REAL,
        cal_back_left_lon REAL,
        cal_back_left_accuracy REAL,
        cal_ridge_lat REAL,
        cal_ridge_lon REAL,
        cal_ridge_accuracy REAL,

        roof_type TEXT,
        roof_age TEXT,
        known_features TEXT,
        description TEXT,

        ai_source TEXT,
        ai_cause TEXT,
        ai_confidence REAL,
        ai_secondary_source TEXT,
        ai_urgency TEXT,
        ai_summary TEXT,
        ai_confirmation_steps TEXT,
        ai_repair_recommendation TEXT,
        ai_cost_range TEXT,
        ai_heatmap_json TEXT,
        ai_callouts_json TEXT,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS investigation_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER NOT NULL,
        photo_stage TEXT,
        file_path TEXT NOT NULL,
        original_filename TEXT,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(investigation_id) REFERENCES investigations(id)
    );

    CREATE TABLE IF NOT EXISTS investigation_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER NOT NULL,
        was_correct TEXT,
        actual_source TEXT,
        actual_cause TEXT,
        actual_repair TEXT,
        repair_cost TEXT,
        reviewer_notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(investigation_id) REFERENCES investigations(id)
    );

    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        plan_code TEXT DEFAULT 'starter',
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        billing_status TEXT DEFAULT 'manual',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'technician',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        user_id INTEGER,
        investigation_id INTEGER,
        event_type TEXT NOT NULL,
        credit_source TEXT,
        credit_purchase_id INTEGER,
        month_key TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS credit_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        pack_code TEXT NOT NULL,
        credits_purchased INTEGER NOT NULL,
        credits_remaining INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        stripe_session_id TEXT,
        status TEXT DEFAULT 'manual_pending_stripe',
        purchased_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS investigation_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER NOT NULL,
        version_number INTEGER NOT NULL,
        generated_by_user_id INTEGER,
        credit_used INTEGER DEFAULT 1,
        ai_snapshot_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS repair_estimates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        investigation_id INTEGER,
        customer_name TEXT,
        property_address TEXT,
        estimate_type TEXT DEFAULT 'repair',
        roof_zip TEXT,
        roof_squares REAL DEFAULT 0,
        material_tier TEXT DEFAULT 'standard',
        labor_rate REAL DEFAULT 85,
        material_cost REAL DEFAULT 0,
        labor_cost REAL DEFAULT 0,
        overhead_profit REAL DEFAULT 0,
        total_cost REAL DEFAULT 0,
        notes TEXT,
        status TEXT DEFAULT 'Draft',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    migrations = {
        "status": "TEXT DEFAULT 'New'",
        "priority": "TEXT DEFAULT 'Normal'",
        "assigned_to": "TEXT",
        "customer_name": "TEXT",
        "customer_phone": "TEXT",
        "customer_email": "TEXT",
        "customer_notes": "TEXT",
        "property_name": "TEXT",
        "insurance_company": "TEXT",
        "claim_number": "TEXT",
        "adjuster_name": "TEXT",
        "adjuster_phone": "TEXT",
        "adjuster_email": "TEXT",
        "contractor_company": "TEXT",
        "contractor_license": "TEXT",
        "contractor_phone": "TEXT",
        "contractor_email": "TEXT",
        "report_notes": "TEXT",
        "property_address": "TEXT",
        "property_lat": "REAL",
        "property_lon": "REAL",
        "weather_summary": "TEXT",
        "weather_rainfall": "TEXT",
        "weather_wind": "TEXT",
        "weather_conditions": "TEXT",

        "interior_room": "TEXT",
        "nearest_wall": "TEXT",
        "distance_from_wall": "TEXT",
        "distance_from_corner": "TEXT",
        "floor_level": "TEXT",

        "interior_lat": "REAL",
        "interior_lon": "REAL",
        "interior_accuracy": "REAL",
        "interior_heading": "REAL",

        "roof_lat": "REAL",
        "roof_lon": "REAL",
        "roof_accuracy": "REAL",
        "roof_heading": "REAL",

        "cal_front_left_lat": "REAL",
        "cal_front_left_lon": "REAL",
        "cal_front_left_accuracy": "REAL",
        "cal_front_right_lat": "REAL",
        "cal_front_right_lon": "REAL",
        "cal_front_right_accuracy": "REAL",
        "cal_back_right_lat": "REAL",
        "cal_back_right_lon": "REAL",
        "cal_back_right_accuracy": "REAL",

        "cal_back_left_lat": "REAL",
        "cal_back_left_lon": "REAL",
        "cal_back_left_accuracy": "REAL",
        "cal_ridge_lat": "REAL",
        "cal_ridge_lon": "REAL",
        "cal_ridge_accuracy": "REAL",

        "ai_heatmap_json": "TEXT",
        "ai_callouts_json": "TEXT",
        "company_id": "INTEGER",
        "created_by_user_id": "INTEGER",
        "completed_ai_count": "INTEGER DEFAULT 0",
        "last_completed_ai_at": "TEXT",
    }

    for column, column_type in migrations.items():
        ensure_column(conn, "investigations", column, column_type)

    conn.commit()
    conn.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS



@app.before_request
def require_login_for_app():
    init_db()
    g.user = current_user()
    if request.endpoint in PUBLIC_ENDPOINTS or (request.endpoint or '').startswith('static'):
        return None
    conn = get_db()
    has_users = user_count(conn) > 0
    conn.close()
    if not has_users:
        return redirect(url_for('setup'))
    if not g.user:
        return redirect(url_for('login', next=request.path))
    return None

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    init_db()
    conn = get_db()
    if user_count(conn) > 0:
        conn.close()
        return redirect(url_for('login'))
    if request.method == 'POST':
        company_name = request.form.get('company_name') or 'LeakTrace Company'
        name = request.form.get('name') or 'Owner'
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        if not email or len(password) < 8:
            flash('Enter an email and a password with at least 8 characters.', 'warning')
            conn.close()
            return render_template('setup.html')
        cur = conn.execute("INSERT INTO companies (name, plan_code, billing_status) VALUES (?, 'business', 'trial')", (company_name,))
        company_id = cur.lastrowid
        conn.execute("INSERT INTO users (company_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, 'owner')", (company_id, name, email, generate_password_hash(password)))
        conn.commit(); conn.close()
        flash('Owner account created. Login to continue.', 'success')
        return redirect(url_for('login'))
    conn.close()
    return render_template('setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    init_db()
    conn = get_db()
    if user_count(conn) == 0:
        conn.close(); return redirect(url_for('setup'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = conn.execute('SELECT * FROM users WHERE email=? AND is_active=1', (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session.clear(); session['user_id'] = user['id']; session['company_id'] = user['company_id']
            return redirect(request.args.get('next') or url_for('company_dashboard'))
        flash('Invalid login.', 'danger')
        return render_template('login.html')
    conn.close(); return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); flash('Logged out.', 'success'); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def company_dashboard():
    conn = get_db(); summary = usage_summary(conn, g.user['company_id'])
    recent_cases = conn.execute('SELECT * FROM investigations WHERE company_id=? ORDER BY created_at DESC LIMIT 8', (g.user['company_id'],)).fetchall()
    open_cases = conn.execute("SELECT COUNT(*) AS c FROM investigations WHERE company_id=? AND COALESCE(status,'New')!='Closed'", (g.user['company_id'],)).fetchone()['c']
    conn.close(); return render_template('company_dashboard.html', summary=summary, recent_cases=recent_cases, open_cases=open_cases)

@app.route('/team', methods=['GET', 'POST'])
@login_required
@owner_required
def team():
    conn = get_db(); summary = usage_summary(conn, g.user['company_id'])
    if request.method == 'POST':
        plan = summary['plan']
        if plan['max_users'] is not None and summary['active_users'] >= plan['max_users']:
            flash('Your plan has reached its user limit. Upgrade or disable a user first.', 'warning')
        else:
            try:
                conn.execute('INSERT INTO users (company_id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)', (g.user['company_id'], request.form.get('name') or 'Team Member', (request.form.get('email') or '').strip().lower(), generate_password_hash(request.form.get('password') or 'LeakTrace123!'), request.form.get('role') or 'technician'))
                conn.commit(); flash('Team user created.', 'success')
            except sqlite3.IntegrityError:
                flash('That email already exists.', 'warning')
        summary = usage_summary(conn, g.user['company_id'])
    users = conn.execute('SELECT * FROM users WHERE company_id=? ORDER BY is_active DESC, created_at DESC', (g.user['company_id'],)).fetchall()
    conn.close(); return render_template('team.html', users=users, summary=summary)

@app.route('/team/<int:user_id>/toggle', methods=['POST'])
@login_required
@owner_required
def toggle_user(user_id):
    conn = get_db()
    if user_id == g.user['id']:
        flash('You cannot disable yourself.', 'warning')
    else:
        conn.execute('UPDATE users SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=? AND company_id=?', (user_id, g.user['company_id']))
        conn.commit(); flash('User status updated.', 'success')
    conn.close(); return redirect(url_for('team'))

@app.route('/billing', methods=['GET', 'POST'])
@login_required
@owner_required
def billing():
    conn = get_db()
    if request.method == 'POST':
        plan_code = request.form.get('plan_code')
        if plan_code in PLAN_RULES:
            conn.execute('UPDATE companies SET plan_code=?, billing_status=? WHERE id=?', (plan_code, 'manual', g.user['company_id']))
            conn.commit(); flash('Plan updated. Stripe checkout can be wired to this same field.', 'success')
    summary = usage_summary(conn, g.user['company_id'])
    purchases = conn.execute('SELECT * FROM credit_purchases WHERE company_id=? ORDER BY purchased_at DESC', (g.user['company_id'],)).fetchall()
    conn.close(); return render_template('billing.html', summary=summary, plans=PLAN_RULES, credit_packs=CREDIT_PACKS, purchases=purchases)

@app.route('/billing/buy-credits', methods=['POST'])
@login_required
@owner_required
def buy_credits():
    pack_code = request.form.get('pack_code'); pack = CREDIT_PACKS.get(pack_code)
    if not pack:
        flash('Invalid credit pack.', 'warning'); return redirect(url_for('billing'))
    conn = get_db()
    conn.execute('INSERT INTO credit_purchases (company_id, pack_code, credits_purchased, credits_remaining, amount_cents) VALUES (?, ?, ?, ?, ?)', (g.user['company_id'], pack_code, pack['credits'], pack['credits'], pack['price']*100))
    conn.commit(); conn.close(); flash('Credit pack added in manual mode. Stripe Checkout is the next hook.', 'success')
    return redirect(url_for('billing'))

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/investigate/start", methods=["POST"])
@login_required
def start_investigation():
    init_db()

    mode = request.form.get("mode", "consumer")
    case_number = "LT-" + datetime.now().strftime("%Y%m%d%H%M%S")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO investigations (case_number, mode, status, priority, company_id, created_by_user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (case_number, mode, "New", "Normal", g.user["company_id"], g.user["id"])
    )
    investigation_id = cur.lastrowid
    conn.commit()
    conn.close()

    return redirect(url_for("investigation_wizard", investigation_id=investigation_id))


@app.route("/investigate/<int:investigation_id>", methods=["GET", "POST"])
@login_required
def investigation_wizard(investigation_id):
    init_db()
    conn = get_db()
    existing = case_access(conn, investigation_id)
    if existing is None:
        conn.close(); flash("Case not found or access denied.", "warning"); return redirect(url_for("admin_cases"))

    if request.method == "POST":
        fields = {
            "status": request.form.get("status") or "AI Complete",
            "priority": request.form.get("priority") or "Normal",
            "assigned_to": request.form.get("assigned_to"),
            "customer_name": request.form.get("customer_name"),
            "customer_phone": request.form.get("customer_phone"),
            "customer_email": request.form.get("customer_email"),
            "customer_notes": request.form.get("customer_notes"),
            "property_name": request.form.get("property_name"),
            "insurance_company": request.form.get("insurance_company"),
            "claim_number": request.form.get("claim_number"),
            "adjuster_name": request.form.get("adjuster_name"),
            "adjuster_phone": request.form.get("adjuster_phone"),
            "adjuster_email": request.form.get("adjuster_email"),
            "contractor_company": request.form.get("contractor_company"),
            "contractor_license": request.form.get("contractor_license"),
            "contractor_phone": request.form.get("contractor_phone"),
            "contractor_email": request.form.get("contractor_email"),
            "report_notes": request.form.get("report_notes"),
            "property_type": request.form.get("property_type"),
            "symptom_type": request.form.get("symptom_type"),
            "symptom_location": request.form.get("symptom_location"),
            "leak_timing": request.form.get("leak_timing"),
            "storm_context": request.form.get("storm_context"),

            "property_address": request.form.get("property_address"),

            "interior_room": request.form.get("interior_room"),
            "nearest_wall": request.form.get("nearest_wall"),
            "distance_from_wall": request.form.get("distance_from_wall"),
            "distance_from_corner": request.form.get("distance_from_corner"),
            "floor_level": request.form.get("floor_level"),

            "roof_type": request.form.get("roof_type"),
            "roof_age": request.form.get("roof_age"),
            "known_features": ",".join(request.form.getlist("known_features")),
            "description": request.form.get("description"),
        }

        conn.execute("""
            UPDATE investigations
            SET status=?,
                priority=?,
                assigned_to=?,
                customer_name=?,
                customer_phone=?,
                customer_email=?,
                customer_notes=?,
                property_name=?,
                insurance_company=?,
                claim_number=?,
                adjuster_name=?,
                adjuster_phone=?,
                adjuster_email=?,
                contractor_company=?,
                contractor_license=?,
                contractor_phone=?,
                contractor_email=?,
                report_notes=?,
                property_type=?,
                symptom_type=?,
                symptom_location=?,
                leak_timing=?,
                storm_context=?,
                property_address=?,
                interior_room=?,
                nearest_wall=?,
                distance_from_wall=?,
                distance_from_corner=?,
                floor_level=?,
                roof_type=?,
                roof_age=?,
                known_features=?,
                description=?
            WHERE id=?
        """, (
            fields["status"],
            fields["priority"],
            fields["assigned_to"],
            fields["customer_name"],
            fields["customer_phone"],
            fields["customer_email"],
            fields["customer_notes"],
            fields["property_name"],
            fields["insurance_company"],
            fields["claim_number"],
            fields["adjuster_name"],
            fields["adjuster_phone"],
            fields["adjuster_email"],
            fields["contractor_company"],
            fields["contractor_license"],
            fields["contractor_phone"],
            fields["contractor_email"],
            fields["report_notes"],
            fields["property_type"],
            fields["symptom_type"],
            fields["symptom_location"],
            fields["leak_timing"],
            fields["storm_context"],
            fields["property_address"],
            fields["interior_room"],
            fields["nearest_wall"],
            fields["distance_from_wall"],
            fields["distance_from_corner"],
            fields["floor_level"],
            fields["roof_type"],
            fields["roof_age"],
            fields["known_features"],
            fields["description"],
            investigation_id
        ))

        action = request.form.get("action", "generate_final")
        if action == "save_draft":
            conn.commit(); conn.close()
            flash("Draft saved. No investigation credit was used.", "success")
            return redirect(url_for("investigation_wizard", investigation_id=investigation_id))

        ok, credit_source = consume_completed_investigation_credit(conn, g.user["company_id"], g.user["id"], investigation_id)
        if not ok:
            conn.rollback(); conn.close()
            flash("You are out of investigation credits. Buy more credits or upgrade your plan.", "warning")
            return redirect(url_for("billing"))

        weather_data = None

        if get_weather_summary and fields["property_address"]:
            try:
                weather_data = get_weather_summary(fields["property_address"])
            except Exception as e:
                print("Weather lookup failed:", e)
                weather_data = None

        if weather_data:
            conn.execute("""
                UPDATE investigations
                SET weather_summary=?,
                    weather_rainfall=?,
                    weather_wind=?,
                    weather_conditions=?,
                    property_lat=?,
                    property_lon=?
                WHERE id=?
            """, (
                weather_data.get("summary"),
                str(weather_data.get("rainfall")),
                str(weather_data.get("wind_speed")),
                str(weather_data),
                weather_data.get("lat"),
                weather_data.get("lon"),
                investigation_id
            ))

        for stage in ["symptom_photos", "interior_photos", "attic_photos", "exterior_photos", "detail_photos"]:
            uploaded_files = request.files.getlist(stage)

            for f in uploaded_files:
                if f and f.filename and allowed_file(f.filename):
                    filename = secure_filename(
                        f"{investigation_id}_{stage}_{datetime.now().timestamp()}_{f.filename}"
                    )
                    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    f.save(save_path)

                    public_path = f"uploads/{filename}"

                    conn.execute("""
                        INSERT INTO investigation_photos
                            (investigation_id, photo_stage, file_path, original_filename)
                        VALUES (?, ?, ?, ?)
                    """, (
                        investigation_id,
                        stage,
                        public_path,
                        f.filename
                    ))

        conn.commit()

        investigation = conn.execute(
            "SELECT * FROM investigations WHERE id=?",
            (investigation_id,)
        ).fetchone()

        photos = conn.execute(
            "SELECT * FROM investigation_photos WHERE investigation_id=?",
            (investigation_id,)
        ).fetchall()

        diagnosis = run_leak_investigation(
            dict(investigation),
            [dict(p) for p in photos]
        )

        conn.execute("""
            UPDATE investigations
            SET ai_source=?,
                ai_cause=?,
                ai_confidence=?,
                ai_secondary_source=?,
                ai_urgency=?,
                ai_summary=?,
                ai_confirmation_steps=?,
                ai_repair_recommendation=?,
                ai_cost_range=?,
                ai_heatmap_json=?,
                ai_callouts_json=?,
                status='AI Complete'
            WHERE id=?
        """, (
            diagnosis.get("probable_source"),
            diagnosis.get("probable_cause"),
            diagnosis.get("confidence"),
            diagnosis.get("secondary_possibility"),
            diagnosis.get("urgency"),
            diagnosis.get("summary"),
            diagnosis.get("confirmation_steps"),
            diagnosis.get("repair_recommendation"),
            diagnosis.get("estimated_cost_range"),
            json.dumps(diagnosis.get("heatmap_zones", [])),
            json.dumps(diagnosis.get("callout_markers", [])),
            investigation_id
        ))

        version_number = conn.execute("SELECT COALESCE(MAX(version_number), 0) + 1 AS v FROM investigation_versions WHERE investigation_id=?", (investigation_id,)).fetchone()["v"]
        conn.execute("INSERT INTO investigation_versions (investigation_id, version_number, generated_by_user_id, credit_used, ai_snapshot_json) VALUES (?, ?, ?, 1, ?)", (investigation_id, version_number, g.user["id"], json.dumps(diagnosis)))
        conn.execute("UPDATE investigations SET completed_ai_count=COALESCE(completed_ai_count,0)+1, last_completed_ai_at=CURRENT_TIMESTAMP WHERE id=?", (investigation_id,))
        conn.commit()
        conn.close()

        flash(f"Final AI investigation generated. 1 {credit_source} credit used.", "success")
        return redirect(url_for("results", investigation_id=investigation_id))

    investigation = conn.execute(
        "SELECT * FROM investigations WHERE id=?",
        (investigation_id,)
    ).fetchone()

    conn.close()

    if investigation is None:
        flash("That investigation case was not found. Start a new investigation.", "warning")
        return redirect(url_for("index"))

    return render_template("wizard.html", investigation=investigation)


@app.route("/results/<int:investigation_id>")
@login_required
def results(investigation_id):
    init_db()
    conn = get_db()

    investigation = case_access(conn, investigation_id)

    if investigation is None:
        conn.close()
        flash("That investigation case was not found. Start a new investigation.", "warning")
        return redirect(url_for("index"))

    photos = conn.execute(
        "SELECT * FROM investigation_photos WHERE investigation_id=?",
        (investigation_id,)
    ).fetchall()

    feedback_record = conn.execute(
        "SELECT * FROM investigation_feedback WHERE investigation_id=? ORDER BY created_at DESC LIMIT 1",
        (investigation_id,)
    ).fetchone()

    heatmap_zones = []
    callout_markers = []

    if investigation["ai_heatmap_json"]:
        try:
            heatmap_zones = json.loads(investigation["ai_heatmap_json"])
        except Exception:
            heatmap_zones = []

    if investigation["ai_callouts_json"]:
        try:
            callout_markers = json.loads(investigation["ai_callouts_json"])
        except Exception:
            callout_markers = []

    conn.close()

    return render_template(
        "results.html",
        investigation=investigation,
        photos=photos,
        feedback_record=feedback_record,
        heatmap_zones=heatmap_zones,
        callout_markers=callout_markers
    )


@app.route("/feedback/<int:investigation_id>", methods=["POST"])
@login_required
def feedback(investigation_id):
    init_db()
    conn = get_db()

    conn.execute("""
        INSERT INTO investigation_feedback
            (investigation_id, was_correct, actual_source, actual_cause, actual_repair, repair_cost, reviewer_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        investigation_id,
        request.form.get("was_correct"),
        request.form.get("actual_source"),
        request.form.get("actual_cause"),
        request.form.get("actual_repair"),
        request.form.get("repair_cost"),
        request.form.get("reviewer_notes")
    ))

    conn.commit()
    conn.close()

    flash("Feedback saved. This case can now improve the training dataset.", "success")
    return redirect(url_for("results", investigation_id=investigation_id))


@app.route("/locator/<int:investigation_id>")
@login_required
def locator(investigation_id):
    init_db()
    conn = get_db()

    investigation = conn.execute(
        "SELECT * FROM investigations WHERE id=?",
        (investigation_id,)
    ).fetchone()

    conn.close()

    if investigation is None:
        flash("Case not found.", "warning")
        return redirect(url_for("index"))

    return render_template("locator.html", investigation=investigation)


@app.route("/api/save-interior-point/<int:investigation_id>", methods=["POST"])
@login_required
def save_interior_point(investigation_id):
    init_db()
    data = request.get_json(silent=True) or {}

    conn = get_db()
    conn.execute("""
        UPDATE investigations
        SET interior_lat=?,
            interior_lon=?,
            interior_accuracy=?,
            interior_heading=?
        WHERE id=?
    """, (
        data.get("lat"),
        data.get("lon"),
        data.get("accuracy"),
        data.get("heading"),
        investigation_id
    ))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/save-roof-point/<int:investigation_id>", methods=["POST"])
@login_required
def save_roof_point(investigation_id):
    init_db()
    data = request.get_json(silent=True) or {}

    conn = get_db()
    conn.execute("""
        UPDATE investigations
        SET roof_lat=?,
            roof_lon=?,
            roof_accuracy=?,
            roof_heading=?
        WHERE id=?
    """, (
        data.get("lat"),
        data.get("lon"),
        data.get("accuracy"),
        data.get("heading"),
        investigation_id
    ))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/save-calibration-point/<int:investigation_id>", methods=["POST"])
@login_required
def save_calibration_point(investigation_id):
    init_db()
    data = request.get_json(silent=True) or {}
    point_type = data.get("point_type")

    allowed = {
        "front_left": (
            "cal_front_left_lat",
            "cal_front_left_lon",
            "cal_front_left_accuracy"
        ),

        "front_right": (
            "cal_front_right_lat",
            "cal_front_right_lon",
            "cal_front_right_accuracy"
        ),

        "back_right": (
            "cal_back_right_lat",
            "cal_back_right_lon",
            "cal_back_right_accuracy"
        ),

        "back_left": (
            "cal_back_left_lat",
            "cal_back_left_lon",
            "cal_back_left_accuracy"
        ),

        "ridge": (
            "cal_ridge_lat",
            "cal_ridge_lon",
            "cal_ridge_accuracy"
        ),
    }

    if point_type not in allowed:
        return jsonify({"success": False, "error": "Invalid calibration point type"}), 400

    lat_col, lon_col, acc_col = allowed[point_type]

    conn = get_db()
    conn.execute(f"""
        UPDATE investigations
        SET {lat_col}=?,
            {lon_col}=?,
            {acc_col}=?
        WHERE id=?
    """, (
        data.get("lat"),
        data.get("lon"),
        data.get("accuracy"),
        investigation_id
    ))

    conn.commit()
    conn.close()

    return jsonify({"success": True})



def get_case_bundle(investigation_id):
    conn = get_db()
    investigation = conn.execute(
        "SELECT * FROM investigations WHERE id=?",
        (investigation_id,)
    ).fetchone()

    photos = conn.execute(
        "SELECT * FROM investigation_photos WHERE investigation_id=? ORDER BY created_at ASC",
        (investigation_id,)
    ).fetchall()

    feedback = conn.execute(
        "SELECT * FROM investigation_feedback WHERE investigation_id=? ORDER BY created_at DESC LIMIT 1",
        (investigation_id,)
    ).fetchone()

    conn.close()
    return investigation, photos, feedback


def make_pdf_report(investigation, photos, feedback=None, report_type="professional"):
    report_dir = os.path.join(BASE_DIR, "generated_reports")
    os.makedirs(report_dir, exist_ok=True)

    suffix = "insurance" if report_type == "insurance" else "professional"
    pdf_path = os.path.join(report_dir, f"{investigation['case_number']}_{suffix}.pdf")

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        rightMargin=42,
        leftMargin=42,
        topMargin=42,
        bottomMargin=42
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="SmallMuted",
        parent=styles["BodyText"],
        fontSize=8,
        textColor=colors.HexColor("#536273"),
        leading=10
    ))
    styles.add(ParagraphStyle(
        name="SectionTitle",
        parent=styles["Heading2"],
        fontSize=14,
        spaceBefore=14,
        spaceAfter=7,
        textColor=colors.HexColor("#0f2537")
    ))

    story = []
    title = "LeakTrace AI Carrier Evidence Report" if report_type == "insurance" else "LeakTrace AI Water Intrusion Report"

    story.append(Paragraph(title, styles["Title"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "AI-assisted field investigation summary. Findings should be verified by a qualified onsite professional before final repair authorization.",
        styles["SmallMuted"]
    ))
    story.append(Spacer(1, 14))

    overview_rows = [
        ["Case Number", pdf_text(investigation["case_number"])],
        ["Status / Priority", f"{pdf_text(investigation['status'], 'New')} / {pdf_text(investigation['priority'], 'Normal')}"],
        ["Customer", pdf_text(investigation["customer_name"])],
        ["Property", pdf_text(investigation["property_name"] or investigation["property_address"])],
        ["Address", pdf_text(investigation["property_address"])],
        ["Mode", pdf_text(investigation["mode"].title() if investigation["mode"] else "")],
        ["Created", pdf_text(investigation["created_at"])],
    ]

    if report_type == "insurance":
        overview_rows.extend([
            ["Insurance Company", pdf_text(investigation["insurance_company"])],
            ["Claim Number", pdf_text(investigation["claim_number"])],
            ["Adjuster", pdf_text(investigation["adjuster_name"])],
        ])
    else:
        overview_rows.extend([
            ["Contractor", pdf_text(investigation["contractor_company"])],
            ["License", pdf_text(investigation["contractor_license"])],
        ])

    table = Table(overview_rows, colWidths=[1.75 * inch, 4.85 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8f3f8")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#0f2537")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c7d2dc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)

    story.append(Spacer(1, 12))
    story.append(Paragraph("Executive Summary", styles["SectionTitle"]))
    story.append(Paragraph(pdf_text(investigation["ai_summary"], "No summary available."), styles["BodyText"]))

    story.append(Paragraph("Primary Finding", styles["SectionTitle"]))
    story.append(Paragraph(f"<b>Most Probable Source:</b> {pdf_text(investigation['ai_source'], 'Pending analysis')}", styles["BodyText"]))
    story.append(Paragraph(f"<b>Probable Cause:</b> {pdf_text(investigation['ai_cause'], 'Pending analysis')}", styles["BodyText"]))
    story.append(Paragraph(f"<b>Confidence:</b> {pdf_text(investigation['ai_confidence'], 'N/A')}%", styles["BodyText"]))
    story.append(Paragraph(f"<b>Secondary Possibility:</b> {pdf_text(investigation['ai_secondary_source'])}", styles["BodyText"]))
    story.append(Paragraph(f"<b>Urgency:</b> {pdf_text(investigation['ai_urgency'])}", styles["BodyText"]))

    story.append(Paragraph("Observed Conditions", styles["SectionTitle"]))
    condition_rows = [
        ["Property Type", pdf_text(investigation["property_type"])],
        ["Symptom", pdf_text(investigation["symptom_type"])],
        ["Symptom Location", pdf_text(investigation["symptom_location"])],
        ["Leak Timing", pdf_text(investigation["leak_timing"])],
        ["Storm Context", pdf_text(investigation["storm_context"])],
        ["Roof Type / Age", f"{pdf_text(investigation['roof_type'])} / {pdf_text(investigation['roof_age'])}"],
        ["Known Features", pdf_text(investigation["known_features"])],
    ]
    ctable = Table(condition_rows, colWidths=[1.75 * inch, 4.85 * inch])
    ctable.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dde5")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f8fb")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(ctable)

    story.append(Paragraph("Weather Correlation", styles["SectionTitle"]))
    story.append(Paragraph(f"<b>Summary:</b> {pdf_text(investigation['weather_summary'], 'No weather data available')}", styles["BodyText"]))
    story.append(Paragraph(f"<b>Rainfall:</b> {pdf_text(investigation['weather_rainfall'], 'N/A')}  <b>Wind:</b> {pdf_text(investigation['weather_wind'], 'N/A')}", styles["BodyText"]))

    story.append(Paragraph("Recommended Confirmation", styles["SectionTitle"]))
    story.append(Paragraph(pdf_text(investigation["ai_confirmation_steps"], "No confirmation steps available."), styles["BodyText"]))

    story.append(Paragraph("Repair Direction", styles["SectionTitle"]))
    story.append(Paragraph(pdf_text(investigation["ai_repair_recommendation"], "No repair recommendation available."), styles["BodyText"]))
    story.append(Paragraph(f"<b>Preliminary Cost Range:</b> {pdf_text(investigation['ai_cost_range'], 'Unknown')}", styles["BodyText"]))

    if report_type == "insurance":
        story.append(Paragraph("Carrier Documentation Notes", styles["SectionTitle"]))
        story.append(Paragraph(
            "This report separates observed symptoms, probable cause, weather context, and repair direction for claim documentation. It does not determine coverage.",
            styles["BodyText"]
        ))

    if investigation["report_notes"]:
        story.append(Paragraph("Contractor / Report Notes", styles["SectionTitle"]))
        story.append(Paragraph(pdf_text(investigation["report_notes"]), styles["BodyText"]))

    if feedback:
        story.append(Paragraph("Field Verification", styles["SectionTitle"]))
        story.append(Paragraph(f"<b>AI Correct:</b> {pdf_text(feedback['was_correct'])}", styles["BodyText"]))
        story.append(Paragraph(f"<b>Actual Source:</b> {pdf_text(feedback['actual_source'])}", styles["BodyText"]))
        story.append(Paragraph(f"<b>Actual Repair:</b> {pdf_text(feedback['actual_repair'])}", styles["BodyText"]))
        story.append(Paragraph(f"<b>Repair Cost:</b> {pdf_text(feedback['repair_cost'])}", styles["BodyText"]))
        story.append(Paragraph(pdf_text(feedback["reviewer_notes"], ""), styles["BodyText"]))

    story.append(PageBreak())
    story.append(Paragraph("Evidence Index", styles["SectionTitle"]))
    story.append(Paragraph(f"Uploaded evidence count: {len(photos)} photo(s).", styles["BodyText"]))
    for idx, photo in enumerate(photos, start=1):
        story.append(Paragraph(
            f"<b>{idx}. {pdf_text(photo['photo_stage'].replace('_', ' ').title())}</b> — {pdf_text(photo['original_filename'])}",
            styles["BodyText"]
        ))

    story.append(Spacer(1, 18))
    story.append(Paragraph("Generated by LeakTrace AI", styles["SmallMuted"]))

    doc.build(story)
    return pdf_path


@app.route("/report/<int:investigation_id>/pdf")
@login_required
def pdf_report(investigation_id):
    init_db()
    investigation, photos, feedback = get_case_bundle(investigation_id)

    if investigation is None:
        flash("Case not found.", "warning")
        return redirect(url_for("index"))

    pdf_path = make_pdf_report(investigation, photos, feedback, report_type="professional")
    return send_file(pdf_path, as_attachment=True)


@app.route("/report/<int:investigation_id>/insurance-pdf")
@login_required
def insurance_pdf_report(investigation_id):
    init_db()
    investigation, photos, feedback = get_case_bundle(investigation_id)

    if investigation is None:
        flash("Case not found.", "warning")
        return redirect(url_for("index"))

    pdf_path = make_pdf_report(investigation, photos, feedback, report_type="insurance")
    return send_file(pdf_path, as_attachment=True)


@app.route("/case/<int:investigation_id>/update", methods=["POST"])
@login_required
def update_case(investigation_id):
    init_db()
    update_fields = [
        "status", "priority", "assigned_to", "customer_name", "customer_phone", "customer_email",
        "customer_notes", "property_name", "property_address", "insurance_company", "claim_number",
        "adjuster_name", "adjuster_phone", "adjuster_email", "contractor_company", "contractor_license",
        "contractor_phone", "contractor_email", "report_notes"
    ]

    values = [request.form.get(field) for field in update_fields]
    assignments = ", ".join([f"{field}=?" for field in update_fields])

    conn = get_db()
    if case_access(conn, investigation_id) is None:
        conn.close(); flash("Case not found or access denied.", "warning"); return redirect(url_for("admin_cases"))
    conn.execute(f"UPDATE investigations SET {assignments} WHERE id=? AND company_id=?", values + [investigation_id, g.user["company_id"]])
    conn.commit()
    conn.close()

    flash("Case file updated.", "success")
    return redirect(url_for("results", investigation_id=investigation_id))


@app.route("/admin/cases")
@login_required
def admin_cases():
    init_db()
    conn = get_db()

    where_sql, params, filters = build_case_filters(request.args)

    if where_sql:
        where_sql = where_sql + " AND company_id=?"
        params = params + [g.user["company_id"]]
    else:
        where_sql = " WHERE company_id=?"
        params = [g.user["company_id"]]
    cases = conn.execute(
        f"SELECT * FROM investigations {where_sql} ORDER BY created_at DESC",
        params
    ).fetchall()

    stats = conn.execute("""
        SELECT
            COUNT(*) AS total_cases,
            SUM(CASE WHEN COALESCE(status, 'New') IN ('New', 'Awaiting Photos', 'Inspection Needed', 'Repair Scheduled') THEN 1 ELSE 0 END) AS open_cases,
            SUM(CASE WHEN COALESCE(status, 'New') = 'AI Complete' THEN 1 ELSE 0 END) AS ai_complete,
            SUM(CASE WHEN COALESCE(status, 'New') = 'Closed' THEN 1 ELSE 0 END) AS closed_cases,
            AVG(CASE WHEN ai_confidence IS NOT NULL THEN ai_confidence END) AS avg_confidence
        FROM investigations
        WHERE company_id=?
    """, (g.user["company_id"],)).fetchone()

    status_counts = conn.execute("""
        SELECT COALESCE(status, 'New') AS status, COUNT(*) AS count
        FROM investigations
        WHERE company_id=?
        GROUP BY COALESCE(status, 'New')
        ORDER BY count DESC
    """, (g.user["company_id"],)).fetchall()

    conn.close()

    return render_template(
        "admin_cases.html",
        cases=cases,
        stats=stats,
        status_counts=status_counts,
        filters=filters
    )


@app.route('/crm/estimates')
@login_required
def crm_estimates():
    conn = get_db()
    estimates = conn.execute('SELECT * FROM repair_estimates WHERE company_id=? ORDER BY created_at DESC', (g.user['company_id'],)).fetchall()
    conn.close()
    return render_template('crm_estimates.html', estimates=estimates)

@app.route('/crm/estimates/new', methods=['GET', 'POST'])
@app.route('/crm/estimates/new/<int:investigation_id>', methods=['GET', 'POST'])
@login_required
def crm_estimate_new(investigation_id=None):
    conn = get_db()
    investigation = case_access(conn, investigation_id) if investigation_id else None
    if request.method == 'POST':
        estimate_type = request.form.get('estimate_type') or 'repair'
        roof_zip = request.form.get('roof_zip') or ''
        roof_squares = float(request.form.get('roof_squares') or 0)
        material_tier = request.form.get('material_tier') or 'standard'
        base_by_tier = {'economy': 115, 'standard': 145, 'premium': 190, 'metal': 260}
        zip_factor = 1.0
        if roof_zip.startswith(('30','31','32')): zip_factor = 0.96
        elif roof_zip.startswith(('33','34')): zip_factor = 1.12
        elif roof_zip.startswith(('90','91','92','93','94')): zip_factor = 1.25
        material_per_square = base_by_tier.get(material_tier, 145) * zip_factor
        labor_rate = float(request.form.get('labor_rate') or 85)
        labor_hours = float(request.form.get('labor_hours') or max(roof_squares * (6 if estimate_type == 'new_roof' else 2.5), 1))
        material_cost = roof_squares * material_per_square
        labor_cost = labor_hours * labor_rate
        overhead_profit = (material_cost + labor_cost) * 0.20
        total_cost = material_cost + labor_cost + overhead_profit
        cur = conn.execute('\n            INSERT INTO repair_estimates\n            (company_id, investigation_id, customer_name, property_address, estimate_type, roof_zip, roof_squares, material_tier, labor_rate, material_cost, labor_cost, overhead_profit, total_cost, notes)\n            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ', (g.user['company_id'], investigation_id, request.form.get('customer_name'), request.form.get('property_address'), estimate_type, roof_zip, roof_squares, material_tier, labor_rate, material_cost, labor_cost, overhead_profit, total_cost, request.form.get('notes')))
        conn.commit(); estimate_id = cur.lastrowid; conn.close()
        flash('CRM estimate created for owner review.', 'success')
        return redirect(url_for('crm_estimate_print', estimate_id=estimate_id))
    conn.close()
    return render_template('crm_estimate_form.html', investigation=investigation)

@app.route('/crm/estimates/<int:estimate_id>/print')
@login_required
def crm_estimate_print(estimate_id):
    conn = get_db()
    estimate = conn.execute('SELECT * FROM repair_estimates WHERE id=? AND company_id=?', (estimate_id, g.user['company_id'])).fetchone()
    conn.close()
    if estimate is None:
        flash('Estimate not found.', 'warning')
        return redirect(url_for('crm_estimates'))
    return render_template('crm_estimate_print.html', estimate=estimate)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)