import os
import sqlite3
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

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


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_number TEXT UNIQUE,
        mode TEXT NOT NULL,

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
    """)

    # Safe migrations for existing local leaktrace.db files
    migrations = {
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
        "cal_ridge_lat": "REAL",
        "cal_ridge_lon": "REAL",
        "cal_ridge_accuracy": "REAL",
    }

    for column, column_type in migrations.items():
        ensure_column(conn, "investigations", column, column_type)

    conn.commit()
    conn.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/investigate/start", methods=["POST"])
def start_investigation():
    init_db()

    mode = request.form.get("mode", "consumer")
    case_number = "LT-" + datetime.now().strftime("%Y%m%d%H%M%S")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO investigations (case_number, mode) VALUES (?, ?)",
        (case_number, mode)
    )
    investigation_id = cur.lastrowid
    conn.commit()
    conn.close()

    return redirect(url_for("investigation_wizard", investigation_id=investigation_id))


@app.route("/investigate/<int:investigation_id>", methods=["GET", "POST"])
def investigation_wizard(investigation_id):
    init_db()
    conn = get_db()

    if request.method == "POST":
        fields = {
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
            SET property_type=?,
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

        # Weather lookup
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
                ai_cost_range=?
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
            investigation_id
        ))

        conn.commit()
        conn.close()

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
def results(investigation_id):
    init_db()
    conn = get_db()

    investigation = conn.execute(
        "SELECT * FROM investigations WHERE id=?",
        (investigation_id,)
    ).fetchone()

    if investigation is None:
        conn.close()
        flash("That investigation case was not found. Start a new investigation.", "warning")
        return redirect(url_for("index"))

    photos = conn.execute(
        "SELECT * FROM investigation_photos WHERE investigation_id=?",
        (investigation_id,)
    ).fetchall()

    conn.close()

    return render_template(
        "results.html",
        investigation=investigation,
        photos=photos
    )


@app.route("/feedback/<int:investigation_id>", methods=["POST"])
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
def save_calibration_point(investigation_id):
    init_db()
    data = request.get_json(silent=True) or {}
    point_type = data.get("point_type")

    allowed = {
        "front_left": ("cal_front_left_lat", "cal_front_left_lon", "cal_front_left_accuracy"),
        "front_right": ("cal_front_right_lat", "cal_front_right_lon", "cal_front_right_accuracy"),
        "ridge": ("cal_ridge_lat", "cal_ridge_lon", "cal_ridge_accuracy"),
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


@app.route("/admin/cases")
def admin_cases():
    init_db()
    conn = get_db()

    cases = conn.execute(
        "SELECT * FROM investigations ORDER BY created_at DESC"
    ).fetchall()

    conn.close()

    return render_template("admin_cases.html", cases=cases)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
