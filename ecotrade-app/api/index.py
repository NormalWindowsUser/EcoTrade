import os
import json
import urllib.request
import urllib.parse
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from google import genai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "ecotrade-secret-session-key-2026")

DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")

def get_db_connection():
    db_url = DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is not configured.")
    if "sslmode=" not in db_url:
        separator = "&" if "?" in db_url else "?"
        db_url += f"{separator}sslmode=require"
    return psycopg2.connect(db_url, connect_timeout=10)

def verify_turnstile(token, ip):
    if not token:
        return False
    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    payload = urllib.parse.urlencode({
        'secret': TURNSTILE_SECRET_KEY,
        'response': token,
        'remoteip': ip
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req) as res:
            result = json.loads(res.read().decode('utf-8'))
            return result.get('success', False)
    except Exception:
        return False

# --- PAGE ROUTES ---

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("account_page"))
    return render_template("login.html")

@app.route("/signup")
def signup_page():
    if "user_id" in session:
        return redirect(url_for("account_page"))
    return render_template("signup.html")

@app.route("/contribute")
def contribute_page():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template("contribute.html")

@app.route("/admin")
def admin_page():
    if session.get("user_role") != "admin":
        return redirect(url_for("login_page"))
    return render_template("admin.html")

@app.route("/chat")
def chat_page():
    return render_template("chat.html")

@app.route("/account")
def account_page():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template("account.html")

@app.route("/activity")
def activity_page():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template("account.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# --- AUTH API ENDPOINTS ---

@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    full_name = data.get("full_name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "public")
    school_name = data.get("school_name", "").strip() or None

    if not full_name or not email or not password:
        return jsonify({"status": "error", "message": "All required fields must be filled."}), 400

    conn = None
    cursor = None
    try:
        pw_hash = generate_password_hash(password)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (full_name, email, password_hash, role, school_name) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
            (full_name, email, pw_hash, role, school_name)
        )
        user_id = cursor.fetchone()[0]
        conn.commit()

        session["user_id"] = user_id
        session["user_name"] = full_name
        session["user_role"] = role

        return jsonify({"status": "success", "message": "Account created successfully!"})
    except psycopg2.IntegrityError:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "An account with this email already exists."}), 400
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"status": "error", "message": "Please provide both email and password."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, full_name, password_hash, role FROM users WHERE email = %s;", (email,))
        user = cursor.fetchone()

        if not user or not user[2] or not check_password_hash(user[2], password):
            return jsonify({"status": "error", "message": "Invalid email or password."}), 401

        session["user_id"] = user[0]
        session["user_name"] = user[1]
        session["user_role"] = user[3]

        return jsonify({"status": "success", "message": "Login successful!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# --- DATA API ENDPOINTS ---

@app.route("/api/user/contributions", methods=["GET"])
def get_user_contributions():
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "Unauthorized access."}), 401

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                c.id,
                COALESCE(m.name, 'Recyclable Material') AS material_name,
                COALESCE(h.hub_name, 'General Hub') AS hub_name,
                c.weight_kg,
                c.calculated_payout,
                c.status
            FROM contributions c
            LEFT JOIN materials m ON c.material_id = m.id
            LEFT JOIN recycling_hubs h ON c.hub_id = h.id
            WHERE c.user_id = %s
            ORDER BY c.id DESC;
        """, (session["user_id"],))
        rows = cursor.fetchall()

        history = [{
            "id": r[0],
            "material": r[1],
            "hub": r[2],
            "weight": float(r[3]),
            "payout": float(r[4]),
            "status": r[5]
        } for r in rows]

        return jsonify({"status": "success", "data": history})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/recent_contributions", methods=["GET"])
def get_recent_contributions():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                COALESCE(m.name, 'Unknown Material') AS material_name,
                COALESCE(h.hub_name, 'Unknown Hub') AS hub_name,
                c.weight_kg,
                c.calculated_payout,
                EXTRACT(EPOCH FROM (NOW() - c.created_at)) AS seconds_ago
            FROM contributions c
            LEFT JOIN materials m ON c.material_id = m.id
            LEFT JOIN recycling_hubs h ON c.hub_id = h.id
            ORDER BY c.created_at DESC
            LIMIT 50;
        """)
        rows = cursor.fetchall()

        contributions = [{
            "material_name": r[0],
            "hub_name": r[1],
            "weight": float(r[2]) if r[2] else 0.0,
            "payout": float(r[3]) if r[3] else 0.0,
            "seconds_ago": float(r[4]) if r[4] is not None else 0
        } for r in rows]

        return jsonify({"status": "success", "data": contributions})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/hubs", methods=["GET"])
def get_hubs():
    city_filter = request.args.get("city", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = "SELECT id, hub_name, address, city, operating_hours, contact_phone FROM recycling_hubs WHERE is_active = TRUE"
        params = []
        if city_filter:
            query += " AND city = %s"
            params.append(city_filter)
        query += " ORDER BY hub_name ASC;"

        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        hubs = [{
            "id": r[0],
            "name": r[1],
            "address": r[2],
            "city": r[3],
            "hours": r[4],
            "phone": r[5]
        } for r in rows]

        cursor.execute("SELECT DISTINCT city FROM recycling_hubs WHERE is_active = TRUE ORDER BY city ASC;")
        cities = [c[0] for c in cursor.fetchall() if c[0]]

        return jsonify({"status": "success", "data": hubs, "cities": cities})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/materials", methods=["GET"])
def get_materials():
    city = request.args.get("city", "").strip()
    hub_id = request.args.get("hub_id", "").strip()

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        where_clauses = ["m.is_active = TRUE"]
        params = []

        if city:
            where_clauses.append("h.city = %s")
            params.append(city)
        if hub_id and hub_id.isdigit():
            where_clauses.append("h.id = %s")
            params.append(int(hub_id))

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT 
                m.id,
                m.name,
                m.category,
                m.price_per_kg AS base_price,
                COALESCE(AVG(c.calculated_payout / NULLIF(c.weight_kg, 0)), m.price_per_kg) AS avg_reported_rate,
                COUNT(c.id) AS report_count,
                m.preparation_tips,
                m.eco_impact_desc
            FROM materials m
            LEFT JOIN contributions c ON m.id = c.material_id
            LEFT JOIN recycling_hubs h ON c.hub_id = h.id
            WHERE {where_str}
            GROUP BY m.id, m.name, m.category, m.price_per_kg, m.preparation_tips, m.eco_impact_desc
            ORDER BY m.name ASC;
        """

        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        materials = [{
            "id": r[0],
            "name": r[1],
            "category": r[2],
            "base_price": float(r[3]),
            "avg_reported_rate": float(r[4]),
            "report_count": int(r[5]),
            "tips": r[6],
            "impact": r[7]
        } for r in rows]

        return jsonify({"status": "success", "data": materials})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/contribute", methods=["POST"])
def add_contribution():
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "You must be logged in to submit a contribution."}), 401

    data = request.get_json(silent=True) or {}
    token = data.get("cf_turnstile_response")
    remote_ip = request.remote_addr

    if not verify_turnstile(token, remote_ip):
        return jsonify({"status": "error", "message": "Anti-bot verification failed."}), 400

    try:
        weight = float(data.get("weight", 0))
        price = float(data.get("price", 0))
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid weight or price format."}), 400

    material_name = data.get("material_name")
    hub_id = data.get("hub_id")
    payout = weight * price

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM materials WHERE name = %s LIMIT 1;", (material_name,))
        mat_row = cursor.fetchone()
        material_id = mat_row[0] if mat_row else 1

        cursor.execute(
            "INSERT INTO contributions (user_id, material_id, hub_id, weight_kg, calculated_payout, status) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
            (session["user_id"], material_id, hub_id, weight, payout, 'approved')
        )
        conn.commit()
        return jsonify({"status": "success", "payout": payout})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/stats", methods=["GET"])
def get_stats():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(weight_kg), 0), COALESCE(SUM(calculated_payout), 0) FROM contributions;")
        row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM recycling_hubs WHERE is_active = TRUE;")
        hubs_count = cursor.fetchone()[0]

        return jsonify({
            "status": "success",
            "total_weight": float(row[0]),
            "total_earnings": float(row[1]),
            "active_hubs": int(hubs_count)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/chat", methods=["POST"])
def chat_ai():
    data = request.get_json(silent=True) or {}
    user_prompt = data.get("prompt", "").strip()

    if not user_prompt:
        return jsonify({"status": "error", "reply": "Please enter a question."}), 400

    materials_context = []
    hubs_context = []
    contributions_context = []
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, category, price_per_kg, unit, preparation_tips, eco_impact_desc, is_active FROM materials;")
        materials_context = cursor.fetchall()

        cursor.execute("SELECT id, hub_name, address, city, operating_hours, contact_phone, is_active FROM recycling_hubs;")
        hubs_context = cursor.fetchall()

        cursor.execute("""
            SELECT 
                c.id, c.user_id, c.material_id, COALESCE(m.name, 'Unknown') AS material_name, 
                c.hub_id, COALESCE(h.hub_name, 'Unknown') AS hub_name, 
                c.weight_kg, c.calculated_payout, c.status, c.notes, c.created_at, c.reviewed_by
            FROM contributions c
            LEFT JOIN materials m ON c.material_id = m.id
            LEFT JOIN recycling_hubs h ON c.hub_id = h.id;
        """)
        contributions_context = cursor.fetchall()
    except Exception:
        pass
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        full_prompt = f"""You are an eco-recycling assistant(Your name is Eco AI) for EcoTrade in Malaysia. If the recycling center near to their location not in the database, then find any other recycling center for them.
Use the following database context to answer user questions:

All Recycling Hubs Table (id, hub_name, address, city, operating_hours, contact_phone, is_active):
{hubs_context}

All Contributions Table (id, user_id, material_id, material_name, hub_id, hub_name, weight_kg, calculated_payout, status, notes, created_at, reviewed_by):
{contributions_context}

User Question: {user_prompt}"""

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=full_prompt
        )
        return jsonify({"status": "success", "reply": response.text})
    except Exception as e:
        return jsonify({"status": "error", "reply": f"AI service error: {str(e)}"}), 500

if __name__ == "__main__":
    app.run()
