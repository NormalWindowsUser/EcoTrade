import os
import json
import urllib.request
import urllib.parse
from flask import Flask, render_template, jsonify, request
import psycopg2
from google import genai

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Default test secret key always yields 'success: true' during development
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

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

@app.route("/contribute")
def contribute_page():
    return render_template("contribute.html")

@app.route("/admin")
def admin_page():
    return render_template("admin.html")

@app.route("/chat")
def chat_page():
    return render_template("chat.html")

@app.route("/account")
def account_page():
    return render_template("account.html")

# --- API ENDPOINTS ---

@app.route("/api/hubs", methods=["GET"])
def get_hubs():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, hub_name, address, city, operating_hours, contact_phone FROM recycling_hubs WHERE is_active = TRUE ORDER BY hub_name ASC;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        hubs = [{
            "id": r[0],
            "name": r[1],
            "address": r[2],
            "city": r[3],
            "hours": r[4],
            "phone": r[5]
        } for r in rows]
        return jsonify({"status": "success", "data": hubs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/materials", methods=["GET"])
def get_materials():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, category, price_per_kg, preparation_tips, eco_impact_desc FROM materials WHERE is_active = TRUE;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        materials = [{
            "id": r[0],
            "name": r[1],
            "category": r[2],
            "price_per_kg": float(r[3]),
            "tips": r[4],
            "impact": r[5]
        } for r in rows]
        return jsonify({"status": "success", "data": materials})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/contribute", methods=["POST"])
def add_contribution():
    try:
        data = request.json
        token = data.get("cf_turnstile_response")
        remote_ip = request.remote_addr

        # Verify Cloudflare Turnstile anti-bot token
        if not verify_turnstile(token, remote_ip):
            return jsonify({"status": "error", "message": "Anti-bot verification failed. Please check the CAPTCHA."}), 400

        material_name = data.get("material_name")
        weight = float(data.get("weight", 0))
        price = float(data.get("price", 0))
        hub_id = data.get("hub_id")
        payout = weight * price

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM materials WHERE name = %s LIMIT 1;", (material_name,))
        mat_row = cursor.fetchone()
        material_id = mat_row[0] if mat_row else 1

        cursor.execute(
            "INSERT INTO contributions (user_id, material_id, hub_id, weight_kg, calculated_payout, status) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
            (2, material_id, hub_id, weight, payout, 'pending')
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "success", "payout": payout})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(weight_kg), 0), COALESCE(SUM(calculated_payout), 0) FROM contributions;")
        row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM recycling_hubs WHERE is_active = TRUE;")
        hubs_count = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return jsonify({
            "status": "success",
            "total_weight": float(row[0]),
            "total_earnings": float(row[1]),
            "active_hubs": int(hubs_count)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def chat_ai():
    try:
        data = request.json
        user_prompt = data.get("prompt", "")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price_per_kg FROM materials WHERE is_active = TRUE;")
        db_context = cursor.fetchall()
        cursor.close()
        conn.close()

        client = genai.Client(api_key=GEMINI_API_KEY)
        full_prompt = f"""You are an eco-recycling assistant for high school students in Malaysia.
        Use this current database context to answer:
        Database Material Prices: {db_context}
        
        User Question: {user_prompt}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=full_prompt
        )
        return jsonify({"status": "success", "reply": response.text})
    except Exception as e:
        return jsonify({"status": "error", "reply": f"AI service error: {str(e)}"}), 500

if __name__ == "__main__":
    app.run()
