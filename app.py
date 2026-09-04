from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import mysql.connector
import requests
import os
from dotenv import load_dotenv
load_dotenv()
import uuid
from datetime import datetime
from flask_mail import Mail, Message
import random
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get("MAIL_USERNAME")

mail = Mail(app)
otp_store = {}

# ── API KEYS ──────────────────────────────────────────

MAGIC_HOUR_API_KEY = os.environ.get("MAGIC_HOUR_API_KEY")
PIXVERSE_API_KEY = os.environ.get("PIXVERSE_API_KEY")

PIXVERSE_GENERATE_URL = "https://app-api.pixverse.ai/openapi/v2/video/text/generate"
PIXVERSE_STATUS_URL   = "https://app-api.pixverse.ai/openapi/v2/video/result/{video_id}"

# ── CREDIT COST TABLES (mirrors frontend JS) ──────────
PIXVERSE_CREDITS = {
    '540p':  {'5': 1,  '8': 2},
    '720p':  {'5': 2,  '8': 4},
    '1080p': {'5': 4,  '8': 8}
}

MAGICHOUR_RATE = {
    '480p':  24,
    '720p':  48,
    '1080p': 96
}

def calculate_cost(api, quality, duration):
    duration = int(duration)
    if api == 'pixverse':
        d_key = '5' if duration <= 5 else '8'
        return PIXVERSE_CREDITS.get(quality, {}).get(d_key, 1)
    else:
        rate = MAGICHOUR_RATE.get(quality, 24)
        return rate * duration


# ── DB CONNECTION ─────────────────────────────────────
db_config = {
    "host": os.environ.get("DB_HOST"),
    "user": os.environ.get("DB_USER"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "ssl_ca": os.environ.get("SSL_CA_PATH", "ca.pem")
}

db = mysql.connector.connect(**db_config)
cursor = db.cursor()

def get_cursor():
    """Return a fresh cursor, reconnecting if the connection dropped."""
    global db, cursor
    try:
        db.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error:
        db = mysql.connector.connect(**db_config)
    cursor = db.cursor()
    return cursor


def get_user_credits(email):
    """Fetch current credit balance for a user from DB."""
    c = get_cursor()
    c.execute("SELECT credits FROM users WHERE email = %s", (email,))
    row = c.fetchone()
    return row[0] if row else 0


# ── ENSURE videos TABLE EXISTS ────────────────────────
def init_videos_table():
    """
    Creates the videos table if it doesn't exist yet.
    Safe to call every startup — uses IF NOT EXISTS.
    """
    c = get_cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id            VARCHAR(100) PRIMARY KEY,
            user_email    VARCHAR(255) NOT NULL,
            prompt        TEXT         NOT NULL,
            quality       VARCHAR(10)  NOT NULL DEFAULT '540p',
            duration      INT          NOT NULL DEFAULT 5,
            aspect        VARCHAR(20)  NOT NULL DEFAULT 'landscape',
            api           VARCHAR(20)  NOT NULL DEFAULT 'pixverse',
            credits_used  INT          NOT NULL DEFAULT 0,
            favorited     TINYINT(1)   NOT NULL DEFAULT 0,
            video_url     TEXT,
            thumbnail_url TEXT,
            status        VARCHAR(20)  NOT NULL DEFAULT 'processing',
            created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_user_email (user_email),
            INDEX idx_status     (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    db.commit()

init_videos_table()
def init_payments_table():
    c = get_cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            user_email   VARCHAR(255) NOT NULL,
            plan         VARCHAR(50)  NOT NULL,
            amount       DECIMAL(10,2) NOT NULL,
            credits      INT          NOT NULL,
            status       VARCHAR(20)  NOT NULL DEFAULT 'success',
            created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_user_email (user_email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    db.commit()

init_payments_table()


# ── VIDEO DB HELPERS ──────────────────────────────────

def save_video(user_email, prompt, quality, duration, aspect, api, credits_used,
               video_url="", thumbnail_url="", status="processing", external_id=None):
    """
    Insert a new video row.
    external_id = the video_id returned by PixVerse / MagicHour API.
    We store it as the row `id` so polling can find the exact row.
    """
    vid = external_id if external_id else str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    c   = get_cursor()
    c.execute("""
        INSERT INTO videos
            (id, user_email, prompt, quality, duration, aspect, api,
             credits_used, favorited, video_url, thumbnail_url, status,
             created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s)
    """, (vid, user_email, prompt, quality, int(duration), aspect, api,
          credits_used, video_url, thumbnail_url, status, now, now))
    db.commit()
    return vid


def update_video_url(video_id, video_url, status="complete"):
    """
    Mark a video complete and store its final URL.
    Call from the status-polling route when generation finishes.
    """
    c = get_cursor()
    c.execute("""
        UPDATE videos
        SET status = %s, video_url = %s, updated_at = %s
        WHERE id = %s
    """, (status, video_url, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), video_id))
    db.commit()


def mark_video_error(video_id):
    """Mark a video as errored."""
    c = get_cursor()
    c.execute("""
        UPDATE videos SET status = 'error', updated_at = %s WHERE id = %s
    """, (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), video_id))
    db.commit()


# ── FRESH URL HELPER ──────────────────────────────────

def get_fresh_magichour_url(video_id):
    """
    Call Magic Hour API to get a fresh signed URL for a video.
    Uses the same /v1/video-projects/{id} endpoint as status polling.
    Returns the fresh URL string, or empty string on failure.
    """
    headers = {"Authorization": f"Bearer {MAGIC_HOUR_API_KEY}"}
    try:
        response = requests.get(
            f"https://api.magichour.ai/v1/video-projects/{video_id}",
            headers=headers,
            timeout=15
        )
        data = response.json()
        # Magic Hour returns the URL in different places depending on version
        url = (
            data.get("video_url") or
            data.get("url") or
            (data.get("downloads") or [{}])[0].get("url") or
            (data.get("output") or {}).get("url") or
            ""
        )
        if url:
            # Save fresh URL back to DB so it's cached for next load
            c = get_cursor()
            c.execute(
                "UPDATE videos SET video_url = %s, updated_at = %s WHERE id = %s",
                (url, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), video_id)
            )
            db.commit()
            print(f"[MagicHour] Fresh URL fetched and cached for {video_id}")
        return url
    except Exception as e:
        print(f"[MagicHour] Failed to get fresh URL for {video_id}: {e}")
        return ""

# ── SEND OTP ──────────────────────────────────────────
@app.route('/forgot/send-otp', methods=['POST'])
def send_otp():
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({'error': 'Email is required.'}), 400

    # Check if email exists in DB
    c = get_cursor()
    c.execute("SELECT email FROM users WHERE email = %s", (email,))
    if not c.fetchone():
        return jsonify({'error': 'No account found with this email.'}), 404

    # Generate 6-digit OTP, valid for 10 minutes
    otp = str(random.randint(100000, 999999))
    otp_store[email] = {
        'otp':        otp,
        'expires_at': time.time() + 600   # 10 minutes
    }

    # Send email
    try:
        msg = Message(
            subject='Your VisionFlux Password Reset Code',
            recipients=[email]
        )
        msg.html = f"""
        <div style="font-family:sans-serif;background:#000;color:#fff;padding:40px;max-width:480px;margin:auto;border-radius:12px;">
          <h2 style="font-size:22px;margin-bottom:8px;">VisionFlux</h2>
          <p style="color:#888;font-size:13px;margin-bottom:32px;">Password Reset Request</p>

          <p style="color:#aaa;font-size:14px;margin-bottom:16px;">
            Use the code below to reset your password. It expires in <strong style="color:#fff;">10 minutes</strong>.
          </p>

          <div style="background:#111;border:1px solid #222;border-radius:10px;padding:28px;text-align:center;margin-bottom:28px;">
            <span style="font-size:36px;font-weight:700;letter-spacing:12px;color:#00ff66;">{otp}</span>
          </div>

          <p style="color:#555;font-size:12px;">
            If you didn't request this, you can safely ignore this email.<br>
            Your password will not be changed.
          </p>
        </div>
        """
        mail.send(msg)
        print(f"[OTP] Sent {otp} to {email}")
        return jsonify({'ok': True})

    except Exception as e:
        print(f"[OTP] Mail error: {e}")
        return jsonify({'error': 'Failed to send email. Check your mail config.'}), 500


# ── VERIFY OTP ────────────────────────────────────────
@app.route('/forgot/verify-otp', methods=['POST'])
def verify_otp():
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()
    otp   = (data.get('otp')   or '').strip()

    record = otp_store.get(email)

    if not record:
        return jsonify({'error': 'No OTP found. Please request a new code.'}), 400

    if time.time() > record['expires_at']:
        otp_store.pop(email, None)
        return jsonify({'error': 'Code has expired. Please request a new one.'}), 400

    if otp != record['otp']:
        return jsonify({'error': 'Incorrect code. Please try again.'}), 400

    # Mark OTP as verified (keep record so reset route can confirm)
    otp_store[email]['verified'] = True
    return jsonify({'ok': True})


# ── RESET PASSWORD ────────────────────────────────────
@app.route('/forgot/reset-password', methods=['POST'])
def reset_password():
    data     = request.get_json()
    email    = (data.get('email')    or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required.'}), 400

    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    record = otp_store.get(email)
    if not record or not record.get('verified'):
        return jsonify({'error': 'OTP not verified. Please complete verification first.'}), 403

    # Update password in DB
    try:
        c = get_cursor()
        c.execute("UPDATE users SET password = %s WHERE email = %s", (password, email))
        db.commit()
        otp_store.pop(email, None)   # clear OTP after successful reset
        print(f"[Reset] Password updated for {email}")
        return jsonify({'ok': True})
    except Exception as e:
        print(f"[Reset] DB error: {e}")
        return jsonify({'error': 'Database error. Please try again.'}), 500

# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

# ── CURRENT USER INFO ─────────────────────────────────
@app.route("/api/me")
def api_me():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401
    return jsonify({"email": session["user"]})

# ── HOME ──────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("home.html")

@app.route('/forgot')
def forgot():
    return render_template('forgot.html')


# ── SIGNUP ────────────────────────────────────────────
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email    = request.form["email"]
        password = request.form["password"]
        confirm  = request.form["confirm_password"]

        if password != confirm:
            flash("Passwords do not match")
            return redirect(url_for("signup"))

        try:
            c = get_cursor()
            c.execute(
                "INSERT INTO users (email, password, credits) VALUES (%s, %s, 120)",
                (email, password)
            )
            db.commit()
            flash("Account created successfully! You have 120 free credits.")
            return redirect(url_for("login"))
        except mysql.connector.Error:
            flash("Email already exists")
            return redirect(url_for("signup"))

    return render_template("signup.html")


# ── LOGIN ─────────────────────────────────────────────
# ── LOGIN ─────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form["email"]
        password = request.form["password"]

        c = get_cursor()
        c.execute("SELECT credits, role FROM users WHERE email = %s AND password = %s", (email, password))
        user = c.fetchone()

        if user:
            session["user"]    = email
            session["credits"] = user[0]
            session["role"]    = user[1]
            flash("Login successful!")

            if user[1] == "admin":
                return redirect(url_for("admin_dashboard"))
            else:
                return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password")
            return redirect(url_for("login"))

    return render_template("login.html")


# ── DASHBOARD ─────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    session["credits"] = get_user_credits(session["user"])
    return render_template("dashboard.html", user=session["user"], credits=session["credits"])


# ── LOGOUT ────────────────────────────────────────────
@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("credits", None)
    flash("Logged out successfully")
    return redirect(url_for("login"))


# ── GENERATE PAGE ─────────────────────────────────────
@app.route("/generate")
def generate():
    if "user" not in session:
        return redirect(url_for("login"))
    session["credits"] = get_user_credits(session["user"])
    return render_template("generate.html", credits=session["credits"])


# ── GALLERY PAGE ──────────────────────────────────────
@app.route("/gallery")
def gallery():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("gallery.html")


# ── PRICING PAGE ──────────────────────────────────────
@app.route("/pricing")
def pricing():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("pricing.html")


# ── CHECKOUT PAGE ─────────────────────────────────────
@app.route("/checkout")
def checkout():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("checkout.html")

@app.route('/payment-history')
def payment_history():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template('payment_history.html')

@app.route('/api/payments')
def api_payments():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401
    
    email = session["user"]   # ← your app uses session["user"], not session["user_email"]
    c = get_cursor()
    c.execute("""
        SELECT id, plan, amount, credits, status, created_at
        FROM payments
        WHERE user_email = %s
        ORDER BY created_at DESC
    """, (email,))
    rows = c.fetchall()
    
    return jsonify([{
        "id":         r[0],
        "plan":       r[1],
        "amount":     float(r[2]),
        "credits":    r[3],
        "status":     r[4],
        "created_at": str(r[5])
    } for r in rows])


# ─────────────────────────────────────────────
# ADMIN PAYMENT DASHBOARD
# ─────────────────────────────────────────────

@app.route("/payments")
def payments():

    # Check login
    if "user" not in session:
        return redirect(url_for("login"))

    # Check admin role
    if session.get("role") != "admin":
        flash("Access denied.")
        return redirect(url_for("dashboard"))

    # Database cursor
    dict_cursor = db.cursor(dictionary=True)

    # Get all payment records
    dict_cursor.execute("""

        SELECT
            id,
            user_email,
            plan,
            amount,
            credits,
            status,
            created_at
        FROM payments
        ORDER BY created_at DESC

    """)

    payment_data = dict_cursor.fetchall()

    # Total revenue
    dict_cursor.execute("""

        SELECT SUM(amount) AS total_amount
        FROM payments
        WHERE status = 'success'

    """)

    revenue_result = dict_cursor.fetchone()

    total_amount = (
        revenue_result["total_amount"]
        if revenue_result["total_amount"]
        else 0
    )

    # Total credits sold
    dict_cursor.execute("""

        SELECT SUM(credits) AS total_credits
        FROM payments
        WHERE status = 'success'

    """)

    credit_result = dict_cursor.fetchone()

    total_credits = (
        credit_result["total_credits"]
        if credit_result["total_credits"]
        else 0
    )

    return render_template(

        "payments.html",

        payments=payment_data,

        total_amount=total_amount,

        total_credits=total_credits

    )


# ══════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════

# ── CREDITS API ───────────────────────────────────────
@app.route("/api/credits")
def api_credits():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401
    credits = get_user_credits(session["user"])
    session["credits"] = credits
    return jsonify({"credits": credits})


# ── PURCHASE / TOP-UP ─────────────────────────────────
@app.route("/api/purchase", methods=["POST"])
def api_purchase():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data    = request.get_json()
    plan    = data.get("plan", "")
    credits = int(data.get("credits", 0))
    amount  = float(data.get("amount", 0))

    if plan not in ("ultimate", "creator") or credits not in (300, 500):
        return jsonify({"error": "Invalid plan"}), 400

    try:
        c = get_cursor()

        # Add credits to user
        c.execute(
            "UPDATE users SET credits = credits + %s WHERE email = %s",
            (credits, session["user"])
        )

        # Log payment
        c.execute("""
            INSERT INTO payments (user_email, plan, amount, credits, status)
            VALUES (%s, %s, %s, %s, 'success')
        """, (session["user"], plan, amount, credits))

        db.commit()
        new_balance        = get_user_credits(session["user"])
        session["credits"] = new_balance
        print(f"[Purchase] {session['user']} plan={plan} +{credits} → balance={new_balance}")
        return jsonify({"ok": True, "new_balance": new_balance})

    except mysql.connector.Error as e:
        print(f"[Purchase] DB error: {e}")
        return jsonify({"error": "Database error"}), 500


# ── GET ALL VIDEOS (Gallery API) ──────────────────────
@app.route("/api/videos")
def api_get_videos():
    """
    Returns all completed videos for the logged-in user.
    For Magic Hour videos, always fetches a fresh signed URL
    from the Magic Hour API so old videos never show ExpiredToken.
    Query params (all optional):
      api=pixverse|magichour
      q=<search text>
      sort=newest|oldest|quality|cost
    """
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    email  = session["user"]
    api_f  = request.args.get("api", "").strip()
    q      = request.args.get("q", "").strip()
    sort   = request.args.get("sort", "newest")

    where  = ["user_email = %s", "status = 'complete'"]
    params = [email]

    if api_f in ("pixverse", "magichour"):
        where.append("api = %s")
        params.append(api_f)

    if q:
        where.append("LOWER(prompt) LIKE %s")
        params.append(f"%{q.lower()}%")

    sort_map = {
        "newest":  "created_at DESC",
        "oldest":  "created_at ASC",
        "quality": "FIELD(quality,'1080p','720p','540p','480p')",
        "cost":    "credits_used DESC",
    }
    order_by = sort_map.get(sort, "created_at DESC")

    sql = f"""
        SELECT id, user_email, prompt, quality, duration, aspect, api,
               credits_used, favorited, video_url, thumbnail_url,
               status, created_at
        FROM videos
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
        LIMIT 200
    """

    c = get_cursor()
    c.execute(sql, params)
    rows = c.fetchall()

    videos = []
    for r in rows:
        video_id   = r[0]
        api_name   = r[6]
        stored_url = r[9] or ""

        # Magic Hour signed URLs expire after 24h.
        # Always fetch a fresh one so users can see all their old videos.
        if api_name == "magichour":
            fresh_url = get_fresh_magichour_url(video_id)
            url = fresh_url if fresh_url else stored_url
        else:
            # PixVerse URLs don't expire — use stored URL as-is
            url = stored_url

        videos.append({
            "id":            video_id,
            "prompt":        r[2],
            "quality":       r[3],
            "duration":      r[4],
            "aspect":        r[5],
            "api":           api_name,
            "credits_used":  r[7],
            "favorited":     bool(r[8]),
            "url":           url,
            "thumbnail_url": r[10] or "",
            "status":        r[11],
            "created_at":    r[12].isoformat() if r[12] else "",
        })

    return jsonify({"videos": videos, "total": len(videos)})


# ── GET FRESH VIDEO URL (used by modal retry) ─────────
@app.route("/api/videos/<video_id>/url")
def api_get_video_url(video_id):
    """
    Returns a fresh URL for a single video.
    Called by the frontend modal when the stored URL has expired.
    """
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    c = get_cursor()
    c.execute(
        "SELECT api, video_url FROM videos WHERE id = %s AND user_email = %s",
        (video_id, session["user"])
    )
    row = c.fetchone()
    if not row:
        return jsonify({"error": "Video not found"}), 404

    api_name, stored_url = row

    if api_name == "magichour":
        url = get_fresh_magichour_url(video_id)
        if not url:
            # Fall back to stored URL if API call fails
            url = stored_url or ""
    else:
        # PixVerse URLs don't expire
        url = stored_url or ""

    if not url:
        return jsonify({"error": "Could not retrieve video URL"}), 500

    return jsonify({"url": url})


# ── TOGGLE FAVORITE ───────────────────────────────────
@app.route("/api/videos/<video_id>/favorite", methods=["POST"])
def api_favorite_video(video_id):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.get_json(silent=True) or {}
    fav  = 1 if data.get("favorited", True) else 0

    c = get_cursor()
    c.execute(
        "SELECT id FROM videos WHERE id = %s AND user_email = %s",
        (video_id, session["user"])
    )
    if not c.fetchone():
        return jsonify({"error": "Video not found"}), 404

    c.execute(
        "UPDATE videos SET favorited = %s WHERE id = %s AND user_email = %s",
        (fav, video_id, session["user"])
    )
    db.commit()
    return jsonify({"ok": True, "favorited": bool(fav)})


# ── DELETE VIDEO ──────────────────────────────────────
@app.route("/api/videos/<video_id>", methods=["DELETE"])
def api_delete_video(video_id):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    c = get_cursor()
    c.execute(
        "SELECT id FROM videos WHERE id = %s AND user_email = %s",
        (video_id, session["user"])
    )
    if not c.fetchone():
        return jsonify({"error": "Video not found"}), 404

    c.execute(
        "DELETE FROM videos WHERE id = %s AND user_email = %s",
        (video_id, session["user"])
    )
    db.commit()
    return jsonify({"ok": True, "deleted": video_id})


# ── GENERATE VIDEO ────────────────────────────────────
@app.route("/generate-video", methods=["POST"])
def generate_video():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    prompt      = request.form.get("prompt", "").strip()
    quality     = request.form.get("quality", "720p")
    duration    = request.form.get("duration", "5")
    orientation = request.form.get("orientation", "landscape")
    api         = request.form.get("api", "pixverse")

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    cost            = calculate_cost(api, quality, duration)
    current_credits = get_user_credits(session["user"])

    if current_credits < cost:
        return jsonify({
            "error":   "not_enough_credits",
            "message": f"You need {cost} credits but only have {current_credits}.",
            "credits": current_credits
        }), 402

    if api == "pixverse":
        result      = _generate_pixverse(prompt, quality, duration, orientation)
    else:
        result      = _generate_magichour(prompt, quality, duration, orientation)

    response_data = result.get_json() if hasattr(result, 'get_json') else {}
    status_code   = result.status_code if hasattr(result, 'status_code') else 200

    if status_code in (200, 201) and "error" not in response_data:
        c = get_cursor()
        c.execute(
            "UPDATE users SET credits = credits - %s WHERE email = %s",
            (cost, session["user"])
        )
        db.commit()
        new_balance        = current_credits - cost
        session["credits"] = new_balance

        external_id = response_data.get("video_id", str(uuid.uuid4()))
        save_video(
            user_email    = session["user"],
            prompt        = prompt,
            quality       = quality,
            duration      = int(duration),
            aspect        = orientation,
            api           = api,
            credits_used  = cost,
            video_url     = "",
            thumbnail_url = "",
            status        = "processing",
            external_id   = external_id
        )

        response_data["credits_remaining"] = new_balance
        return jsonify(response_data), status_code

    return result


def _generate_pixverse(prompt, quality, duration, orientation):
    """Call PixVerse v2 API to start video generation."""
    aspect_map = {
        "landscape": "16:9",
        "square":    "1:1",
        "portrait":  "9:16"
    }
    aspect_ratio = aspect_map.get(orientation, "16:9")
    dur          = 5 if int(duration) <= 5 else 8
    quality_map  = {"720p": "720p", "1080p": "1080p"}
    pv_quality   = quality_map.get(quality, "720p")

    headers = {
        "API-KEY":     PIXVERSE_API_KEY,
        "Ai-trace-id": str(uuid.uuid4())
    }
    payload = {
        "prompt":       prompt,
        "aspect_ratio": aspect_ratio,
        "duration":     dur,
        "model":        "v5",
        "quality":      pv_quality,
        "water_mark":   False
    }
    print(f"\n[PixVerse] quality={pv_quality} dur={dur}s aspect={aspect_ratio}")

    try:
        response = requests.post(PIXVERSE_GENERATE_URL, json=payload, headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500

    print(f"[PixVerse] {response.status_code} → {response.text}")

    try:
        data = response.json()
    except Exception:
        return jsonify({"error": f"Invalid JSON: {response.text}"}), 500

    if response.status_code != 200:
        return jsonify({"error": f"PixVerse error: {data.get('ErrMsg', response.text)}"}), response.status_code

    if data.get("ErrCode", -1) != 0:
        return jsonify({"error": f"PixVerse: {data.get('ErrMsg', 'Unknown error')}"}), 400

    video_id = (data.get("Resp") or {}).get("video_id")
    if not video_id:
        return jsonify({"error": "No video_id returned", "raw": data}), 500

    print(f"[PixVerse] video_id={video_id}")
    return jsonify({"video_id": video_id, "api": "pixverse"})


def _generate_magichour(prompt, quality, duration, orientation):
    """Call Magic Hour API to start video generation."""
    headers = {
        "Authorization": f"Bearer {MAGIC_HOUR_API_KEY}",
        "Content-Type":  "application/json"
    }
    payload = {
        "name":        f"Video by {session['user']}",
        "end_seconds": float(duration),
        "orientation": orientation,
        "style":       {"prompt": prompt}
    }
    print(f"\n[MagicHour] quality={quality} dur={duration}s")

    try:
        response = requests.post(
            "https://api.magichour.ai/v1/text-to-video",
            json=payload, headers=headers, timeout=30
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500

    print(f"[MagicHour] {response.status_code} → {response.text}")

    try:
        data = response.json()
    except Exception:
        return jsonify({"error": f"Invalid JSON: {response.text}"}), 500

    if response.status_code not in (200, 201):
        return jsonify({"error": f"Magic Hour error: {data.get('message', response.text)}"}), response.status_code

    video_id = (data or {}).get("id")
    if not video_id:
        return jsonify({"error": "No video id returned", "raw": data}), 500

    print(f"[MagicHour] video_id={video_id}")
    return jsonify({"video_id": video_id, "api": "magichour"})


# ── VIDEO STATUS ──────────────────────────────────────
@app.route("/video-status/<video_id>")
def video_status(video_id):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    api = request.args.get("api", "pixverse")

    if api == "pixverse":
        result = _status_pixverse(video_id)
    else:
        result = _status_magichour(video_id)

    data = result.get_json() if hasattr(result, 'get_json') else {}
    if data.get("status") == "complete":
        video_url = ""
        downloads = data.get("downloads", [])
        if downloads:
            video_url = downloads[0].get("url", "")

        _persist_completed_video(video_id, video_url, session["user"])

    elif data.get("status") == "error":
        _mark_video_error_by_external(video_id, session["user"])

    return result


def _persist_completed_video(external_video_id, video_url, user_email):
    """
    Update the video row by its ID (which IS the external_video_id)
    with the final URL and mark it complete.
    """
    try:
        c = get_cursor()
        c.execute("""
            UPDATE videos
            SET status = 'complete', video_url = %s, updated_at = %s
            WHERE id = %s AND user_email = %s
        """, (video_url, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
               external_video_id, user_email))
        db.commit()
        print(f"[DB] Marked {external_video_id} complete → {video_url}")
    except Exception as e:
        print(f"[DB] Error persisting video: {e}")


def _mark_video_error_by_external(external_video_id, user_email):
    try:
        c = get_cursor()
        c.execute("""
            UPDATE videos
            SET status = 'error', updated_at = %s
            WHERE id = %s AND user_email = %s
        """, (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
               external_video_id, user_email))
        db.commit()
        print(f"[DB] Marked {external_video_id} as error")
    except Exception as e:
        print(f"[DB] Error marking video error: {e}")


def _status_pixverse(video_id):
    """Check PixVerse video status."""
    headers = {
        "Token":       PIXVERSE_API_KEY,
        "Ai-trace-id": str(uuid.uuid4())
    }
    url = PIXVERSE_STATUS_URL.format(video_id=video_id)

    try:
        response = requests.get(url, headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"[PixVerse Status] Network error: {e}")
        return jsonify({"status": "processing"})

    print(f"[PixVerse Status] {response.status_code} → {response.text}")

    try:
        data = response.json()
    except Exception:
        return jsonify({"status": "processing"})

    resp      = (data or {}).get("Resp") or {}
    pv_status = resp.get("status")

    if pv_status == 1:
        return jsonify({
            "status":    "complete",
            "downloads": [{"url": resp.get("url")}]
        })
    elif pv_status == -1:
        return jsonify({"status": "error"})
    else:
        return jsonify({"status": "processing"})


def _status_magichour(video_id):
    """Check Magic Hour video status."""
    headers = {"Authorization": f"Bearer {MAGIC_HOUR_API_KEY}"}

    try:
        response = requests.get(
            f"https://api.magichour.ai/v1/video-projects/{video_id}",
            headers=headers, timeout=15
        )
    except requests.exceptions.RequestException as e:
        print(f"[MagicHour Status] Network error: {e}")
        return jsonify({"status": "processing"})

    print(f"[MagicHour Status] {response.status_code} → {response.text}")

    try:
        data = response.json()
    except Exception:
        return jsonify({"status": "processing"})

    return jsonify(data)


# Option 1: Simple /admin route
# Option 1: Simple /admin route
@app.route("/admin")
def admin_dashboard():

    # Not logged in
    if "user" not in session:
        return redirect(url_for("login"))

    # Logged in but not admin
    if session.get("role") != "admin":
        flash("Access denied.")
        return redirect(url_for("dashboard"))

    c = get_cursor()

    # Total users
    c.execute("SELECT COUNT(*) FROM users")
    user_count = c.fetchone()[0]

    # Allocation count
    c.execute("SELECT COUNT(*) FROM users WHERE credits > 0")
    allocation_count = c.fetchone()[0]

    # Payment count
    c.execute("SELECT COUNT(*) FROM payments")
    payment_count = c.fetchone()[0]

    return render_template(

        "admin.html",

        user_count=user_count,

        allocation_count=allocation_count,

        payment_count=payment_count

    )


@app.route("/users")
def users():
    if "user" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        flash("Access denied.")
        return redirect(url_for("dashboard"))

    dict_cursor = db.cursor(dictionary=True)
    dict_cursor.execute("SELECT id, email, created_at, credits, role FROM users")
    users_data = dict_cursor.fetchall()

    return render_template("users.html", users=users_data)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )