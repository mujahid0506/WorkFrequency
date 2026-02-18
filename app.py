from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient
from datetime import datetime, time
import os
app = Flask(__name__)
CORS(app)
MONGO_URI = os.environ.get("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["production"]

# ================= GLOBAL STATE =================
def init_esp(esp_id):
    if esp_id not in esp_state:
        esp_state[esp_id] = {
            "mode": "BREAK",
            "board_start_time": None,
            "last_job_press_time": None,
            "break_start": None  # ← ADDED: Track when break started
        }

esp_state = {}
DEBOUNCE_WINDOW = 10  # seconds

# ================= ESP → DEPARTMENT =================
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


# ================= SHIFT LOGIC (3 SHIFTS) =================
SHIFT_ORDER = ["SHIFT_1", "SHIFT_2", "SHIFT_3"]

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

def cleanup_old_shifts():
    current_shift = get_current_shift()
    previous_shift = get_previous_shift(current_shift)

    db.esp_cycle_times.delete_many({
        "shift": {"$nin": [current_shift, previous_shift]}
    })
    
    # ← ADDED: Also cleanup old break sessions
    db.break_sessions.delete_many({
        "shift": {"$nin": [current_shift, previous_shift]}
    })

def morning_reset():
    now = datetime.now()
    if now.hour == 6 and now.minute == 0:
        db.esp_cycle_times.delete_many({})
        db.break_sessions.delete_many({})  # ← ADDED: Clear break sessions too
        print("🌅 Morning reset done")

# ================= STARTUP SAFETY =================
def startup_tasks():
    cleanup_old_shifts()
    db.esp_cycle_times.create_index(
        [("esp_id", 1), ("shift", 1), ("timestamp", 1)]
    )
    # ← ADDED: Create index for break_sessions collection
    db.break_sessions.create_index(
        [("esp_id", 1), ("shift", 1), ("start_time", 1)]
    )
    print("✅ Startup cleanup + indexing done")

def get_active_target_time(esp_id):
    department = ESP_DEPARTMENTS.get(esp_id)
    model = ESP_MODELS.get(esp_id)

    doc = db.target_config.find_one(
        {"department": department, "model": model},
        sort=[("updated_at", -1)]
    )

    return doc["target_time_sec"] if doc else None


# ================= MODE =================
@app.route("/set-mode", methods=["POST"])
def set_mode():
    data = request.json or {}
    mode = data.get("mode")
    esp_id = data.get("esp_id")

    if not esp_id:
        return jsonify({"error": "esp_id required"}), 400
    if mode not in ["WORK", "BREAK"]:
        return jsonify({"error": "Invalid mode"}), 400

    init_esp(esp_id)
    state = esp_state[esp_id]
    now = datetime.utcnow()
    
    # ← ADDED: Store old mode to detect transitions
    old_mode = state["mode"]

    if state["mode"] == mode:
        return jsonify({"mode": mode, "allowed": True})

    # ==================== BREAK TRACKING LOGIC START ====================
    # When switching FROM WORK to BREAK
    if old_mode == "WORK" and mode == "BREAK":
        # Record when break started
        state["break_start"] = now
        print(f"[{esp_id}] Break started at {now}")
    
    # When switching FROM BREAK to WORK
    if old_mode == "BREAK" and mode == "WORK":
        break_start = state.get("break_start")
        
        if break_start:
            break_end = now
            current_shift = get_current_shift()
            
            # Calculate break duration
            break_duration = (break_end - break_start).total_seconds()
            
            # Store break session in database
            db.break_sessions.insert_one({
                "esp_id": esp_id,
                "shift": current_shift,
                "start_time": break_start,
                "end_time": break_end,
                "duration_sec": int(break_duration)
            })
            
            print(f"[{esp_id}] Break ended. Duration: {break_duration}s")
            
            # Reset break start time
            state["break_start"] = None
    # ==================== BREAK TRACKING LOGIC END ====================

    if state["mode"] == "BREAK" and mode == "WORK":
        state["mode"] = "WORK"
        state["board_start_time"] = None
        return jsonify({"mode": "WORK", "allowed": True})

    if state["mode"] == "WORK" and mode == "BREAK":
        if state["board_start_time"] is None:
            state["mode"] = "BREAK"
            return jsonify({"mode": "BREAK", "allowed": True})

        elapsed = (now - state["board_start_time"]).total_seconds()

        if elapsed > DEBOUNCE_WINDOW:
            return jsonify({
                "blocked": True,
                "mode": "WORK",
                "reason": "Board in progress",
                "elapsed": int(elapsed)
            }), 403

        state["mode"] = "BREAK"
        state["board_start_time"] = None
        return jsonify({
            "mode": "BREAK",
            "allowed": True,
            "message": "Board cancelled"
        })

    state["mode"] = mode
    return jsonify({"mode": mode, "allowed": True})

@app.route("/set-target", methods=["POST"])
def set_target():
    data = request.json

    department = data.get("department")
    model = data.get("model")
    target_time = data.get("target_time_sec")

    if not department or not model or not target_time:
        return jsonify({"error": "Invalid data"}), 400

    db.target_config.update_one(
        {"department": department, "model": model},
        {
            "$set": {
                "target_time_sec": target_time,
                "updated_at": datetime.utcnow()
            }
        },
        upsert=True
    )

    return jsonify({"status": "active_target_updated"})


# ================= JOB BUTTON =================
@app.route("/job-done", methods=["POST"])
def job_done():
    data = request.json or {}
    esp_id = data.get("esp_id")

    if not esp_id:
        return jsonify({"error": "esp_id required"}), 400

    init_esp(esp_id)
    state = esp_state[esp_id]

    if state["mode"] != "WORK":
        return jsonify({"ignored": True, "reason": "Not in WORK mode"})

    now = datetime.utcnow()

    if state["last_job_press_time"]:
        if (now - state["last_job_press_time"]).total_seconds() < 1:
            return jsonify({"ignored": True, "reason": "Hardware debounce"})

    if state["board_start_time"] is None:
        state["board_start_time"] = now
        state["last_job_press_time"] = now
        return jsonify({"action": "started"})

    elapsed = (now - state["board_start_time"]).total_seconds()

    if elapsed < DEBOUNCE_WINDOW:
        return jsonify({"ignored": True, "reason": "Too fast", "elapsed": int(elapsed)})

    cycle_time = int(elapsed)

    db.esp_cycle_times.insert_one({
        "esp_id": esp_id,
        "cycle_time_sec": cycle_time,
        "shift": get_current_shift(),
        "timestamp": now
    })

    cleanup_old_shifts()
    morning_reset()

    state["board_start_time"] = now
    state["last_job_press_time"] = now

    return jsonify({"action": "completed_and_started", "cycle_time": cycle_time})

# ================= STATUS =================
@app.route("/job-event", methods=["GET"])
def job_event():
    esp_id = request.args.get("esp_id")
    if not esp_id or esp_id not in esp_state:
        return jsonify({"error": "Invalid esp_id"}), 400

    last_time = esp_state[esp_id]["last_job_press_time"]
    return jsonify({
        "esp_id": esp_id,
        "event_time": last_time.timestamp() if last_time else 0
    })

@app.route("/mode-status", methods=["GET"])
def mode_status():
    esp_id = request.args.get("esp_id")
    if not esp_id or esp_id not in esp_state:
        return jsonify({"error": "Invalid esp_id"}), 400

    state = esp_state[esp_id]
    elapsed = None
    if state["board_start_time"]:
        elapsed = int((datetime.utcnow() - state["board_start_time"]).total_seconds())

    return jsonify({
        "esp_id": esp_id,
        "mode": state["mode"],
        "board_active": state["board_start_time"] is not None,
        "elapsed": elapsed
    })

# ================= GRAPH API (RESTART SAFE) =================
@app.route("/graph-data", methods=["GET"])
def graph_data():
    esp_id = request.args.get("esp_id")
    if not esp_id:
        return jsonify({"error": "esp_id required"}), 400

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

    department = ESP_DEPARTMENTS.get(esp_id)
    target_doc = db.target_config.find_one(
      {"department": department},
      sort=[("updated_at",-1)]
     )
    target_time = target_doc["target_time_sec"] if target_doc else None
    model_name = target_doc["model"] if target_doc else None

    # ==================== FETCH BREAK SESSIONS START ====================
    # Fetch all break sessions for current shift
    break_cursor = db.break_sessions.find(
        {
            "esp_id": esp_id,
            "shift": current_shift
        },
        {"_id": 0}  # Exclude MongoDB _id field
    ).sort("start_time", 1)
    
    break_list = list(break_cursor)
    
    # Debug print to verify breaks are being fetched
    print(f"[{esp_id}] Found {len(break_list)} break sessions for {current_shift}")
    # ==================== FETCH BREAK SESSIONS END ====================

    return jsonify({
        "esp_id": esp_id,
        "department": department,
        "current_shift": current_shift,
        "previous_shift": previous_shift,
        "current_shift_data": current_data,
        "previous_shift_data": previous_data,
        "target_time": target_time,
        "model": model_name,
        "break_sessions": break_list  # ← ADDED: Send break sessions to frontend
    })

# ================= RUN =================
if __name__ == "__main__":
    startup_tasks()
    app.run(host="0.0.0.0", port=5000, debug=True)