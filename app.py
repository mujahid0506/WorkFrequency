from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from pymongo import MongoClient
from datetime import datetime, time
import os

# ================= APP SETUP =================

app = Flask(__name__)
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*")

MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["production"]

# ================= GLOBAL STATE =================

esp_state = {}
DEBOUNCE_WINDOW = 10  # seconds

ESP_DEPARTMENTS = {
    "ESP01": "Assembly",
    "ESP02": "Testing",
    "ESP03": "Packing",
    "ESP04": "Quality"
}

ESP_MODELS = {
    "ESP01": "Model_A",
    "ESP02": "Model_A",
    "ESP03": "Model_B",
    "ESP04": "Model_C"
}

SHIFT_ORDER = ["SHIFT_1", "SHIFT_2", "SHIFT_3"]

# ================= UTIL FUNCTIONS =================

def init_esp(esp_id):
    if esp_id not in esp_state:
        esp_state[esp_id] = {
            "mode": "BREAK",
            "board_start_time": None,
            "last_job_press_time": None,
            "break_start": None
        }

def get_current_shift():
    now = datetime.now().time()
    if time(6, 0) <= now < time(14, 0):
        return "SHIFT_1"
    elif time(14, 0) <= now < time(22, 0):
        return "SHIFT_2"
    else:
        return "SHIFT_3"

def get_previous_shift(current_shift):
    idx = SHIFT_ORDER.index(current_shift)
    return SHIFT_ORDER[idx - 1] if idx > 0 else SHIFT_ORDER[-1]

def get_graph_data(esp_id):
    current_shift = get_current_shift()
    previous_shift = get_previous_shift(current_shift)

    cursor = db.esp_cycle_times.find(
        {
            "esp_id": esp_id,
            "shift": {"$in": [current_shift, previous_shift]}
        },
        {"_id": 0}
    ).sort("timestamp", 1)

    current_data, previous_data = [], []

    for row in cursor:
        if row["shift"] == current_shift:
            current_data.append(row)
        else:
            previous_data.append(row)

    target_doc = db.target_config.find_one(
        {"department": ESP_DEPARTMENTS.get(esp_id)},
        sort=[("updated_at", -1)]
    )

    target_time = target_doc["target_time_sec"] if target_doc else None
    model = target_doc["model"] if target_doc else None

    return {
        "current_shift_data": current_data,
        "previous_shift_data": previous_data,
        "target_time": target_time,
        "model": model
    }

# ================= ROUTES =================

@app.route("/")
def home():
    return "🚀 Railway WebSocket Backend Running"

# ================= SET MODE =================

@app.route("/set-mode", methods=["POST"])
def set_mode():
    data = request.json or {}
    esp_id = data.get("esp_id")
    mode = data.get("mode")

    if not esp_id:
        return jsonify({"error": "esp_id required"}), 400

    init_esp(esp_id)
    state = esp_state[esp_id]
    old_mode = state["mode"]
    now = datetime.utcnow()

    if mode not in ["WORK", "BREAK"]:
        return jsonify({"error": "Invalid mode"}), 400

    # ===== BREAK TRACKING =====
    if old_mode == "WORK" and mode == "BREAK":
        state["break_start"] = now

    if old_mode == "BREAK" and mode == "WORK":
        break_start = state.get("break_start")
        if break_start:
            duration = (now - break_start).total_seconds()
            db.break_sessions.insert_one({
                "esp_id": esp_id,
                "shift": get_current_shift(),
                "start_time": break_start,
                "end_time": now,
                "duration_sec": int(duration)
            })
            state["break_start"] = None

    state["mode"] = mode

    # 🔥 PUSH TO DASHBOARD
    socketio.emit("mode_update", {
        "esp_id": esp_id,
        "mode": mode
    })

    return jsonify({"mode": mode})

# ================= JOB DONE =================

@app.route("/job-done", methods=["POST"])
def job_done():
    data = request.json or {}
    esp_id = data.get("esp_id")

    if not esp_id:
        return jsonify({"error": "esp_id required"}), 400

    init_esp(esp_id)
    state = esp_state[esp_id]

    if state["mode"] != "WORK":
        return jsonify({"ignored": True})

    now = datetime.utcnow()

    if state["board_start_time"] is None:
        state["board_start_time"] = now
        return jsonify({"started": True})

    elapsed = (now - state["board_start_time"]).total_seconds()

    if elapsed < DEBOUNCE_WINDOW:
        return jsonify({"ignored": True})

    cycle_time = int(elapsed)

    db.esp_cycle_times.insert_one({
        "esp_id": esp_id,
        "cycle_time_sec": cycle_time,
        "shift": get_current_shift(),
        "timestamp": now
    })

    state["board_start_time"] = now

    # 🔥 PUSH GRAPH UPDATE
    socketio.emit("job_update", {
        "esp_id": esp_id,
        "graph_data": get_graph_data(esp_id)
    })

    return jsonify({"cycle_time": cycle_time})

# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)