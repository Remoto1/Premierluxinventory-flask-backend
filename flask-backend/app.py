import eventlet
eventlet.monkey_patch(all=True)

from flask import Flask, request, jsonify, session, render_template, redirect
from flask_cors import CORS
from pymongo import MongoClient
import os
import json
import numpy as np
from datetime import datetime, timedelta
from flask_socketio import SocketIO
from groq import Groq
from bson.objectid import ObjectId
import time
import uuid
import string
import random

# ---------- Flask + CORS Setup ----------
app = Flask(__name__)

is_production = "RENDER" in os.environ 

app.config.update(
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=is_production, 
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7)
)

allowed_origins = [
    "http://127.0.0.1:5000",
    "http://localhost:5000",
    "https://premierluxinventory.onrender.com"
]

CORS(app, supports_credentials=True, origins=allowed_origins)
socketio = SocketIO(app, cors_allowed_origins="*")
app.secret_key = "premierlux_secret_key"

# ---------- MongoDB setup ----------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://dbirolliverhernandez_db_user:yqHWCWJwNxKofjHs@cluster0.bgmzgav.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client["premierlux"]

# API Key Config
LOCAL_DEV_KEY = "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"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", LOCAL_DEV_KEY)

if not GROQ_API_KEY or not GROQ_API_KEY.startswith("gsk_"):
    print("⚠️ WARNING: Groq API Key is missing or invalid! AI features will not work.")

# Collections
inventory_collection = db["inventory"]
branches_collection = db["branches"]
batches_collection = db["batches"]
consumption_collection = db["consumption"]
suppliers_collection = db["suppliers"]
orders_collection = db["orders"]
users_collection = db["users"]
audit_collection = db["audit_logs"]
settings_collection = db["settings"]
ai_dashboard_collection = db["ai_dashboard"]

# ---------- USER SETUP (Force Fix) ----------

# This guarantees the Owner account ALWAYS exists with these credentials
users_collection.update_one(
    {"email": "owner@premierlux.com"}, 
    {"$set": {
        "name": "System Owner",
        "password": "owner123",  # ➤ This sets the password explicitly
        "role": "owner",
        "branch": "All",
        "status": "active",
        "created_at": datetime.now()
    }},
    upsert=True # ➤ This creates it if it doesn't exist
)

# Optional: Clean up old duplicate owner if it exists under a different email
users_collection.delete_many({
    "role": "owner", 
    "email": {"$ne": "owner@premierlux.com"}
})


# ---------- HELPER FUNCTIONS ----------
def log_behavior(user_email, action, details):
    audit_collection.insert_one({
        "user": user_email,
        "action": action,
        "details": details,
        "timestamp": datetime.now()
    })

def expiry_within_days(expiry_value, days):
    if not expiry_value: return False
    try:
        if isinstance(expiry_value, datetime): expiry_dt = expiry_value
        else: expiry_dt = datetime.fromisoformat(str(expiry_value))
        return expiry_dt <= datetime.now() + timedelta(days=days)
    except:
        return False


# ---------- AUTHENTICATION ----------

@app.route("/")
def home():
    if "user_email" not in session: return redirect("/login")
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    email = data.get("email")
    password = data.get("password")

    user = users_collection.find_one({"email": email})
    
    if not user or user.get("password") != password:
        return jsonify({"error": "Invalid email or password"}), 401

    # ➤ RULE: Check Approval Status
    if user.get("status") == "pending":
        return jsonify({"error": "Account pending approval from Owner."}), 403

    # ➤ RULE: System Lockdown (Owner bypasses)
    setting = settings_collection.find_one({"_id": "global_config"})
    is_locked = setting.get("lockdown", False) if setting else False

    if is_locked and user.get("role") != "owner":
        return jsonify({"error": "System is under MAINTENANCE. Owner access only."}), 403

    session.permanent = True
    session["user_email"] = user["email"]
    session["user_name"] = user.get("name", "User")
    session["role"] = user.get("role", "staff")
    session["branch"] = user.get("branch", "Main")
    
    log_behavior(user["email"], "Login", "User logged into the system")
    
    return jsonify({
        "message": "Login successful",
        "role": user.get("role"),
        "name": user.get("name"),
        "branch": user.get("branch")
    }), 200

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"message": "Logged out"}), 200

@app.route("/api/me", methods=["GET"])
def get_current_user():
    if "user_email" not in session: return jsonify({"error": "Not logged in"}), 401
    return jsonify({
        "name": session.get("user_name"),
        "email": session.get("user_email"),
        "role": session.get("role"),
        "branch": session.get("branch")
    })
# ---------- BRANCHES ----------

@app.route("/api/branches", methods=["GET"])
def get_branches():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["name"] = session.get("branch")

    branches = list(branches_collection.find(query))
    for b in branches: b["_id"] = str(b["_id"]) 
    return jsonify(branches)

@app.route("/api/branches", methods=["POST"])
def add_branch():
    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json or {}
    if not data.get("name"): return jsonify({"error": "Branch name required"}), 400

    branches_collection.insert_one({
        "name": data["name"],
        "address": data.get("address", ""),
        "manager": data.get("manager", ""),
        "phone": data.get("phone", "")
    })
    log_behavior(session.get("user_email"), "Add Branch", f"Added branch {data['name']}")
    return jsonify({"message": "Branch added"}), 201

@app.route("/api/branches/<id>", methods=["PUT"])
def update_branch(id):
    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    data = request.json or {}
    branches_collection.update_one({"_id": ObjectId(id)}, {"$set": data})
    return jsonify({"message": "Branch updated"}), 200

@app.route("/api/branches/<id>", methods=["DELETE"])
def delete_branch(id):
    if session.get("role") != "owner": return jsonify({"error": "Only Owner can delete branches"}), 403
    branches_collection.delete_one({"_id": ObjectId(id)})
    return jsonify({"message": "Branch deleted"}), 200


# ---------- INVENTORY & BATCHES ----------

@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["branch"] = session.get("branch")

    requested_branch = request.args.get("branch")
    search_q = request.args.get("q")

    if requested_branch and requested_branch != "All" and "branch" not in query:
        query["branch"] = requested_branch
        
    if search_q:
        query["name"] = {"$regex": search_q, "$options": "i"}

    items = list(inventory_collection.find(query, {"_id": 0}))
    return jsonify(items)

@app.route("/api/batches", methods=["GET"])
def get_batches():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["branch"] = session.get("branch")

    batches = list(batches_collection.find(query))
    for b in batches: b['_id'] = str(b['_id']) 
    return jsonify(batches), 200

@app.route("/api/batches", methods=["POST"])
def create_batch():
    data = request.get_json(force=True)
    
    target_branch = data.get("branch")
    if session.get("role") == "staff" and session.get("branch") != target_branch:
        return jsonify({"error": "Cannot add stock to other branches"}), 403

    auto_batch = data.get("batch_number") or f"BTN-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000,9999)}"
    auto_lot = data.get("lot_number") or f"LOT-{datetime.now().strftime('%Y%m%d')}"
    auto_qr = data.get("qr_code_id") or str(uuid.uuid4())[:8].upper()

    batch_doc = {
        "item_name": data.get("item_name"),
        "sku": data.get("sku"),
        "branch": target_branch,
        "current_stock": int(data.get("current_stock", 0)),
        "monthly_usage": int(data.get("monthly_usage", 0)),
        "price": float(data.get("price", 0)),
        "reorder_level": int(data.get("reorder_level", 0)),
        "batch_number": auto_batch,
        "lot_number": auto_lot,
        "supplier_batch": data.get("supplier_batch", "General"),
        "qr_code_id": auto_qr,
        "mfg_date": data.get("mfg_date"),
        "exp_date": data.get("exp_date"),
        "category": data.get("category", "Uncategorized"),
    }

    batches_collection.insert_one(batch_doc)

    inventory_collection.update_one(
        {"name": batch_doc["item_name"], "branch": batch_doc["branch"]},
        {
            "$setOnInsert": { "created_at": datetime.now() },
            "$set": { 
                "reorder_level": batch_doc["reorder_level"],
                "price": batch_doc["price"],
                "category": batch_doc["category"],
                "monthly_usage": batch_doc["monthly_usage"]
            },
            "$inc": { "quantity": batch_doc["current_stock"] }
        },
        upsert=True
    )
    
    log_behavior(session.get("user_email"), "Add Batch", f"Added {batch_doc['current_stock']} of {batch_doc['item_name']} to {target_branch}")
    return jsonify({"status": "ok", "message": "Batch created"}), 201

@app.route("/api/inventory/<name>/adjust", methods=["POST"])
def adjust_inventory(name):
    data = request.json or {}
    branch = data.get("branch")
    delta = int(data.get("delta", 0))
    
    if session.get("role") == "staff" and session.get("branch") != branch:
        return jsonify({"error": "Unauthorized branch access"}), 403

    query = {"name": name, "branch": branch}
    inv = inventory_collection.find_one(query)
    
    if not inv: return jsonify({"error": "Item not found"}), 404

    new_qty = max(0, int(inv.get("quantity", 0)) + delta)
    inventory_collection.update_one({"_id": inv["_id"]}, {"$set": {"quantity": new_qty}})

    consumption_collection.insert_one({
        "name": name,
        "date": datetime.now(),
        "quantity_used": abs(delta),
        "direction": "out" if delta < 0 else "in",
        "branch": branch,
        "reason_category": data.get("reason_category", "Manual"),
        "note": data.get("note", "")
    })

    return jsonify({"status": "ok", "quantity": new_qty})

@app.route("/api/inventory/<item_name>", methods=["DELETE"])
def delete_inventory(item_name):

    if session.get("role") == "staff": return jsonify({"error": "Unauthorized"}), 403
    inventory_collection.delete_one({"name": item_name})
    batches_collection.delete_many({"item_name": item_name})
    return jsonify({"message": "Item deleted"})


# ---------- SUPPLIERS & ORDERS ----------

@app.route("/api/suppliers", methods=["GET"])
def get_suppliers():
    if session.get("role") == "staff": return jsonify([]) 
    suppliers = list(suppliers_collection.find({}, {"_id": 0}))
    return jsonify(suppliers), 200

@app.route("/api/suppliers", methods=["POST"])
def add_supplier():
    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    suppliers_collection.insert_one(data)
    return jsonify({"message": "Supplier added"}), 201

@app.route("/api/suppliers/<name>", methods=["POST"])
def update_supplier(name):

    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    suppliers_collection.update_one({"name": name}, {"$set": data})
    return jsonify({"message": "Supplier updated"}), 200

@app.route("/api/suppliers/<name>", methods=["DELETE"])
def delete_supplier(name):

    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    suppliers_collection.delete_one({"name": name})
    return jsonify({"message": "Supplier deleted"}), 200

@app.route("/api/orders", methods=["GET"])
def get_orders():
    query = {}
    if session.get("role") == "staff":
        query["branch"] = session.get("branch")
        
    orders = list(orders_collection.find(query).sort("created_at", -1))
    for o in orders: o["_id"] = str(o["_id"])
    return jsonify(orders), 200

@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.json
    data["created_at"] = datetime.now()
    data["status"] = "pending"
    orders_collection.insert_one(data)
    return jsonify({"message": "Order placed"}), 201


# ---------- AI & INTELLIGENCE ----------

@app.route("/api/ai/dashboard", methods=["GET"])
def get_ai_dashboard():
    doc = ai_dashboard_collection.find_one({"_id": "summary"}) or {}
    return jsonify({
        "summary_text": doc.get("summary_text", "AI system initialized. Waiting for data."),
        "risk_text": doc.get("risk_text", "No critical risks detected."),
        "updated_at": doc.get("updated_at", datetime.now().isoformat())
    })


# //////////////////////////////////////// //
#      🧠 LUX CHAT (VISION & EXPIRY AWARE)  //
# //////////////////////////////////////// //

@app.route("/api/chat", methods=["POST"])
def chat():
    if not GROQ_API_KEY: 
        return jsonify({"type": "error", "text": "LUX is offline (API Key Missing)."}), 500
    
    try:
        client = Groq(api_key=GROQ_API_KEY)
        data = request.json
        user_msg = data.get("message", "")
        image_data = data.get("image") # Base64 string from frontend
        
        # ➤ FIX: If user sends image but no text, provide a default prompt
        if image_data and not user_msg:
            user_msg = "Please analyze this image and tell me what dental instrument or item this is."

        # 1. IDENTIFY USER SCOPE
        user_role = session.get("role", "staff")
        user_branch = session.get("branch", "Main")
        
        query = {}
        if user_role == "staff" and user_branch != "All":
            query["branch"] = user_branch

        # 2. GATHER DATA (The "Brain")
        
        # A. Inventory Stats
        total_count = inventory_collection.count_documents(query)
        
        # B. Critical Low Stock
        low_stock_cursor = inventory_collection.find(
            {**query, "$expr": {"$lte": ["$quantity", "$reorder_level"]}},
            {"name": 1, "quantity": 1, "reorder_level": 1, "_id": 0}
        ).limit(5)
        low_stock_list = [f"{i['name']} (Qty: {i['quantity']}/{i['reorder_level']})" for i in low_stock_cursor]
        low_stock_str = ", ".join(low_stock_list) if low_stock_list else "None"

        # C. ➤ NEW: Expiry Awareness (Next 60 Days)
        today_str = datetime.now().strftime("%Y-%m-%d")
        future_str = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        
        # Find batches expiring soon
        expiring_cursor = batches_collection.find({
            **query,
            "exp_date": {"$gte": today_str, "$lte": future_str}
        }).sort("exp_date", 1).limit(5)
        
        exp_list = [f"{b['item_name']} (Expires: {b.get('exp_date')})" for b in expiring_cursor]
        exp_str = ", ".join(exp_list) if exp_list else "No items expiring soon."

        # 3. CONSTRUCT SYSTEM PROMPT
        system_context = f"""
        You are LUX, the advanced AI Assistant for PremierLux Dental.
        
        REAL-TIME SYSTEM DATA:
        - Total SKUs: {total_count}
        - Low Stock Alerts: {low_stock_str}
        - Expiring Soon (60 days): {exp_str}
        
        USER CONTEXT:
        - Role: {user_role}
        - Branch: {user_branch}
        
        CAPABILITIES:
        1. If the user sends an image of a dental instrument, identify it precisely (e.g., "This looks like a Gracey Curette 11/12").
        2. If asked about expiry, list the specific items from the data above.
        3. Keep answers professional, concise, and helpful.
        """

        # 4. SELECT MODEL & FORMAT MESSAGE
        # If image is present, use Vision Model. If text only, use Text Model.
        if image_data:
            model = "meta-llama/llama-4-scout-17b-16e-instruct"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{system_context}\n\nUSER QUESTION: {user_msg}"},
                        {"type": "image_url", "image_url": {"url": image_data}}
                    ]
                }
            ]
        else:
            model = "llama-3.3-70b-versatile" # ➤ Fast Text Model
            messages = [
                {"role": "system", "content": system_context},
                {"role": "user", "content": user_msg}
            ]

        # 5. SEND TO GROQ
        chat_completion = client.chat.completions.create(
            messages=messages,
            model=model,
            temperature=0.3,
            max_tokens=500
        )
        
        return jsonify({"type": "llm_answer", "text": chat_completion.choices[0].message.content})

    except Exception as e:
        print(f"LUX Chat Error: {e}")
        return jsonify({"type": "error", "text": f"I encountered an error: {str(e)}"}), 500

@app.route('/api/ai/analyze', methods=['GET'])
def ai_analyze_inventory():
    if not GROQ_API_KEY: return jsonify({"insight_text": "AI Key Missing", "status_badge": "Offline"})
    try:
        client = Groq(api_key=GROQ_API_KEY)
        query = {}
        if session.get("role") == "staff": query["branch"] = session.get("branch")
        
        items = list(inventory_collection.find(query, {"_id":0, "name":1, "quantity":1, "reorder_level":1}).limit(20))
        
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Analyze inventory JSON. Return JSON keys: 'insight_text', 'status_badge' (Healthy/Warning/Critical), 'recommended_order' list."},
                {"role": "user", "content": str(items)}
            ],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        return jsonify(json.loads(completion.choices[0].message.content))
    except Exception as e:
        return jsonify({"insight_text": "Analysis failed", "status_badge": "Error"})

@app.get("/api/ai/predict-restock")
def aipredictrestock():
    """
    Enhanced predictive endpoint:
    - Respects staff branch scope
    - Accepts ?days=N (horizon), default 30
    - Computes risk_score 0–100 and risk_level label
    - Returns more structured payload for frontend UI
    """
    if "user_email" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # Scope by branch for staff
    query = {}
    if session.get("role") == "staff":
        query["branch"] = session.get("branch")

    # Read optional forecast horizon from query params (in days)
    try:
        horizon_days = int(request.args.get("days", 30))
        if horizon_days <= 0:
            horizon_days = 30
    except (TypeError, ValueError):
        horizon_days = 30

    items = list(inventory_collection.find(query))
    results = []

    for item in items:
        name = item.get("name")
        branch = item.get("branch", "Main")
        qty = int(item.get("quantity", 0) or 0)
        reorder = int(item.get("reorderlevel", 0) or 0)
        monthly_usage = int(item.get("monthlyusage", 0) or 0)

        # Avoid division by zero
        avg_daily_usage = monthly_usage / 30 if monthly_usage > 0 else 0

        if avg_daily_usage > 0:
            days_left = qty / avg_daily_usage
        else:
            # No usage history: treat as low risk unless quantity is already
            # below reorder level.
            days_left = 999

        # Base recommendation: aim for 1 month of usage + 20% buffer
        target_stock = int(max((monthly_usage * 1.2), reorder))
        recommended_order = max(target_stock - qty, 0)

        # Skip items that are clearly very safe (beyond horizon and above reorder)
        if days_left > horizon_days and qty >= reorder:
            continue

        # Risk score: closer to 0 days_left → higher risk (max 100)
        # If days_left <= 0, treat as stockout.
        if days_left <= 0:
            risk_score = 100
        else:
            ratio = min(horizon_days / days_left, 2.0)  # cap effect
            risk_score = int(min(ratio * 60 + 40, 100))  # 40–100

        # Clamp 0–100
        risk_score = max(0, min(risk_score, 100))

        # Risk level buckets
        if days_left <= 3 or risk_score >= 90:
            risk_level = "Critical"
        elif days_left <= 7 or risk_score >= 75:
            risk_level = "High"
        elif days_left <= 14 or risk_score >= 55:
            risk_level = "Medium"
        else:
            risk_level = "Low"

        results.append({
            "name": name,
            "branch": branch,
            "currentstock": qty,
            "reorderlevel": reorder,
            "monthlyusage": monthly_usage,
            "daysuntilout": int(days_left if days_left < 999 else horizon_days + 1),
            "risk_score": risk_score,
            "risk_level": risk_level,
            "recommendedorder": recommended_order,
            "horizon_days": horizon_days,
        })

    # Sort highest risk first
    results.sort(key=lambda x: (-x["risk_score"], x["daysuntilout"]))
    return jsonify(results)


# ➤ RESTORED: MATHEMATICAL REPLENISHMENT FORMULA
@app.get("/api/replenishment/recommendations")
def get_replenishment_recommendations():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["branch"] = session.get("branch")

    items = list(inventory_collection.find(query))
    recommendations = []

    for item in items:
        name = item.get("name")
        branch = item.get("branch", "Main")
        qty = int(item.get("quantity", 0))
        reorder = int(item.get("reorder_level", 0))

        # Formula: Reorder Point = (Daily Usage * Lead Time) + Safety Stock
        avg_daily_usage = int(item.get("monthly_usage", 0)) / 30
        if avg_daily_usage <= 0: avg_daily_usage = 1 

        lead_time_days = 7 
        safety_stock = reorder 

        reorder_point = (avg_daily_usage * lead_time_days) + safety_stock
        trigger_level = max(reorder, reorder_point)

        if qty <= trigger_level:
            target_stock = (avg_daily_usage * (lead_time_days + 7)) + safety_stock
            suggested_qty = max(int(target_stock - qty), 0)

            if suggested_qty > 0:
                recommendations.append({
                    "name": name,
                    "branch": branch,
                    "current_quantity": qty,
                    "reorder_level": reorder,
                    "reorder_point": int(reorder_point),
                    "suggested_order_qty": suggested_qty,
                })

    return jsonify(recommendations)

@app.route('/api/ai/market-intelligence', methods=['GET'])
def ai_market_intelligence():
    if session.get("role") == "staff": return jsonify({"error": "Unauthorized"}), 403
    
    if not GROQ_API_KEY: 
        return jsonify({"market_summary": "AI Key Missing", "predictions": []})

    try:
        pipeline = [
            {"$sort": {"mfg_date": 1}},
            {"$group": {
                "_id": {"item": "$item_name", "supplier": "$supplier_batch"},
                "price_history": {"$push": "$price"},
                "last_price": {"$last": "$price"}
            }},
            {"$limit": 10}
        ]
        market_data = list(batches_collection.aggregate(pipeline))
        data_str = json.dumps(market_data, default=str)

        client = Groq(api_key=GROQ_API_KEY)
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Analyze supplier prices. JSON keys: 'market_summary', 'predictions' (list with item, supplier, trend, forecast, advice)."},
                {"role": "user", "content": f"Data: {data_str}"}
            ],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        return jsonify(json.loads(completion.choices[0].message.content))
    except Exception as e:
        print(e)
        return jsonify({"market_summary": "Analysis unavailable", "predictions": []})
    



@app.route("/api/ai/generate-restock-plan", methods=["POST"])
def ai_generate_restock_plan():
    # 1. Security Check
    if session.get("role") not in ["owner", "admin"]: 
        return jsonify({"error": "Unauthorized"}), 403

    if not GROQ_API_KEY: 
        return jsonify({"error": "AI Key Missing"}), 500

    try:
        # 2. ➤ GET ACTIVE ORDERS (Pending or Approved)
        # We must exclude items that are already on their way
        active_orders = list(orders_collection.find(
            {"status": {"$in": ["pending", "approved"]}},
            {"item": 1, "branch": 1, "_id": 0}
        ))
        
        # Create a "Blocklist" set: "ItemName|BranchName"
        # If an item+branch combo is in this set, we skip it.
        active_order_keys = {f"{o.get('item')}|{o.get('branch')}" for o in active_orders}

        # 3. Fetch Candidates
        all_items = list(inventory_collection.find({}, {"_id":0, "name":1, "branch":1, "quantity":1, "reorder_level":1, "monthly_usage":1}))
        
        candidates = []
        for i in all_items:
            # Check Blocklist first
            item_key = f"{i.get('name')}|{i.get('branch')}"
            if item_key in active_order_keys:
                continue # ➤ SKIP: Already ordered

            # Logic: If Stock is <= Reorder Level + 20% OR very low (< 5)
            threshold = int(i.get("reorder_level", 0) * 1.2)
            if int(i.get("quantity", 0)) <= threshold or int(i.get("quantity", 0)) < 5:
                candidates.append(i)

        if not candidates:
            return jsonify({"recommendations": [], "message": "No new items need restocking."})

        # 4. Ask LUX (AI)
        client = Groq(api_key=GROQ_API_KEY)
        data_str = json.dumps(candidates)
        
        prompt = f"""
        You are LUX, an Intelligent Inventory Agent. Create a restock plan.
        
        Current Low Stock Data:
        {data_str}
        
        Rules:
        1. Suggest a 'quantity' to cover 1 month of usage (monthly_usage) + safety buffer.
        2. If monthly_usage is 0, suggest a minimum of 10.
        3. 'reason' should be short (e.g. "Critical Low", "Below Buffer").
        4. Output ONLY JSON: {{ "recommendations": [ {{ "item": "Name", "branch": "Branch", "quantity": 15, "reason": "..." }} ] }}
        """

        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a JSON-only inventory API."},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )

        return jsonify(json.loads(completion.choices[0].message.content))

    except Exception as e:
        print(f"AI Reorder Error: {e}")
        return jsonify({"error": "AI Brain Offline"}), 500


# ---------- ANALYTICS & ALERTS ----------

@app.get("/analytics/overview")
def analytics_overview():
    target_branch = request.args.get('branch')
    if session.get("role") == "staff" and session.get("branch") != "All":
        target_branch = session.get("branch")
    
    query = {}
    if target_branch and target_branch != 'All': query["branch"] = target_branch
    
    seven_days = datetime.now() - timedelta(days=7)
    
    return jsonify({
        "new_items": inventory_collection.count_documents({**query, "created_at": {"$gte": seven_days}}),
        "batches_7d": batches_collection.count_documents({**query, "mfg_date": {"$gte": seven_days.strftime("%Y-%m-%d")}}),
        "total_items": inventory_collection.count_documents(query),
        "branches": 1 if target_branch and target_branch != 'All' else branches_collection.count_documents({})
    })

@app.get("/analytics/movement")
def analytics_movement():
    target_branch = request.args.get('branch')
    if session.get("role") == "staff" and session.get("branch") != "All":
        target_branch = session.get("branch")

    query = {}
    if target_branch and target_branch != 'All':
        query["branch"] = target_branch

    today = datetime.now()
    labels = []
    stock_in = []
    stock_out = []

    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        labels.append(day.strftime("%a")) 

        # Stock IN
        in_count = 0
        batch_docs = list(batches_collection.find(query))
        for b in batch_docs:
            b_date = b.get("mfg_date") or b.get("created_at")
            if str(b_date).startswith(day_str):
                in_count += int(b.get("current_stock", 0))
        stock_in.append(in_count)

        # Stock OUT
        out_count = 0
        cons_docs = list(consumption_collection.find(query))
        for c in cons_docs:
            c_date = c.get("date")
            c_date_str = str(c_date)[:10] if c_date else ""
            if c_date_str == day_str and c.get("direction") == "out":
                out_count += int(c.get("quantity_used", 0))
        stock_out.append(out_count)

    return jsonify({"labels": labels, "stock_in": stock_in, "stock_out": stock_out})

@app.get("/analytics/movement-monthly")
def analytics_movement_monthly():
    target_branch = request.args.get('branch')
    selected_year = int(request.args.get('year', datetime.now().year)) 

    if session.get("role") == "staff" and session.get("branch") != "All":
        target_branch = session.get("branch")

    query = {}
    if target_branch and target_branch != 'All':
        query["branch"] = target_branch

    labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    stock_in = [0] * 12
    stock_out = [0] * 12

    all_batches = list(batches_collection.find(query))
    for b in all_batches:
        d_val = b.get("mfg_date") or b.get("created_at")
        try:
            d_date = datetime.fromisoformat(str(d_val)[:10])
            if d_date.year == selected_year:
                stock_in[d_date.month - 1] += int(b.get("current_stock", 0))
        except: continue

    all_cons = list(consumption_collection.find(query))
    for c in all_cons:
        d_val = c.get("date")
        try:
            d_date = datetime.fromisoformat(str(d_val)[:10])
            if d_date.year == selected_year and c.get("direction") == "out":
                stock_out[d_date.month - 1] += int(c.get("quantity_used", 0))
        except: continue

    return jsonify({"labels": labels, "stock_in": stock_in, "stock_out": stock_out})

@app.get("/analytics/top-products")
def analytics_top_products():
    pipeline = [
        {"$match": {"direction": "out"}},
        {"$group": {"_id": "$name", "used": {"$sum": "$quantity_used"}}},
        {"$sort": {"used": -1}},
        {"$limit": 5}
    ]
    data = list(consumption_collection.aggregate(pipeline))
    return jsonify(data)

@app.get("/analytics/low-stock")
def analytics_low_stock():
    # Fetch real low stock items for the dashboard table
    return jsonify(list(inventory_collection.find(
        {"$expr": {"$lte": ["$quantity", "$reorder_level"]}},
        {"_id": 0, "name": 1, "quantity": 1, "branch": 1}
    )))

@app.route("/api/low-stock-count", methods=["GET"])
def api_low_stock_count():
    count = inventory_collection.count_documents({"$expr": {"$lte": ["$quantity", "$reorder_level"]}})
    return jsonify({"count": int(count)})

@app.route("/api/total-inventory", methods=["GET"])
def api_total_inventory():
    pipeline = [{"$project": {"total": {"$multiply": ["$price", "$quantity"]}}}]
    data = list(inventory_collection.aggregate(pipeline))
    total = sum([d.get("total", 0) for d in data])
    return jsonify({"value": total})

@app.route("/api/branches-count", methods=["GET"])
def api_branches_count():
    return jsonify({"count": branches_collection.count_documents({})})

@app.get("/api/alerts")
def get_alerts():
    user_id = session.get("user_email")
    role = session.get("role", "staff")
    branch = session.get("branch")
    query = {}
    if role == "staff" and branch != 'All': query["branch"] = branch
    
    alerts = []
    items = list(inventory_collection.find(query))
    for i in items:
        if i.get("quantity", 0) <= i.get("reorder_level", 0):
            alerts.append({
                "id": f"low-{str(i.get('_id'))}",
                "type": "low_stock",
                "title": f"Low Stock: {i.get('name')}",
                "description": f"Only {i.get('quantity')} left in {i.get('branch')}",
                "branch": i.get("branch")
            })
    return jsonify(alerts)

@app.post("/api/alerts/<alert_id>/acknowledge")
def acknowledge_alert(alert_id):
    db.alert_acknowledgements.insert_one({"alert_id": alert_id, "user_id": session.get("user_email")})
    return jsonify({"status": "ok"})


# ---------- ROLE-BASED USER MANAGEMENT ----------

@app.route("/api/users", methods=["GET"])
def get_users():
    # Only Owner and Admin can see users
    if session.get("role") not in ["owner", "admin"]:
        return jsonify({"error": "Unauthorized"}), 403
        
    users = list(users_collection.find({}, {"password": 0})) 
    for u in users: u["_id"] = str(u["_id"])
    return jsonify(users)

@app.route("/api/users", methods=["POST"])
def create_user():
    current_role = session.get("role")
    
    # 1. Permission Check
    if current_role not in ["owner", "admin"]:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    if not data.get("email") or not data.get("password"):
        return jsonify({"error": "Missing fields"}), 400
    
    if users_collection.find_one({"email": data["email"]}):
        return jsonify({"error": "Email exists"}), 400

    # 2. Status Logic (Owner -> Active, Admin -> Pending)
    initial_status = "active" if current_role == "owner" else "pending"

    new_user = {
        "name": data.get("name", "New User"),
        "email": data["email"],
        "password": data["password"],
        "role": data.get("role", "staff"),
        "branch": data.get("branch", "Main"),
        "status": initial_status,  # ➤ New Field
        "created_at": datetime.now()
    }
    users_collection.insert_one(new_user)
    
    log_msg = f"Created user {data['email']}"
    if initial_status == "pending": log_msg += " (Pending Approval)"
    
    log_behavior(session.get("user_email"), "Create User", log_msg)
    
    return jsonify({
        "message": "User created successfully",
        "status": initial_status,
        "note": "Waiting for Owner approval" if initial_status == "pending" else ""
    }), 201

# ➤ NEW: Approve User Endpoint (Owner Only)
@app.route("/api/users/<user_id>/approve", methods=["PUT"])
def approve_user(user_id):
    if session.get("role") != "owner":
        return jsonify({"error": "Only Owner can approve accounts"}), 403

    users_collection.update_one(
        {"_id": ObjectId(user_id)}, 
        {"$set": {"status": "active"}}
    )
    log_behavior(session.get("user_email"), "Approve User", f"Approved user {user_id}")
    return jsonify({"message": "User approved"}), 200

@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    current_role = session.get("role")
    if current_role not in ["owner", "admin"]:
        return jsonify({"error": "Unauthorized"}), 403

    target_user = users_collection.find_one({"_id": ObjectId(user_id)})
    if not target_user: return jsonify({"error": "Not found"}), 404

    target_role = target_user.get("role")

    # ➤ RULE: Hierarchy Checks
    if target_role == "owner":
        return jsonify({"error": "Cannot delete Owner"}), 403
        
    if current_role == "admin":
        if target_role == "admin":
            return jsonify({"error": "Admins cannot delete other Admins"}), 403
        if target_role == "owner":
            return jsonify({"error": "Admins cannot delete Owner"}), 403

    users_collection.delete_one({"_id": ObjectId(user_id)})
    log_behavior(session.get("user_email"), "Delete User", f"Deleted {target_user.get('email')}")
    return jsonify({"message": "User deleted"}), 200

# ---------- SETTINGS (Owner Only) ----------

@app.route("/api/admin/settings", methods=["GET"])
def get_system_settings():
    if session.get("role") != "owner": return jsonify({"error": "Unauthorized"}), 403
    doc = settings_collection.find_one({"_id": "global_config"}) or {}
    return jsonify({"lockdown": doc.get("lockdown", False)})

@app.route("/api/admin/lockdown", methods=["POST"])
def toggle_lockdown():
    if session.get("role") != "owner": return jsonify({"error": "Unauthorized"}), 403
    new_status = request.json.get("status", False)
    settings_collection.update_one({"_id": "global_config"}, {"$set": {"lockdown": new_status}}, upsert=True)
    return jsonify({"message": f"Lockdown {'Enabled' if new_status else 'Disabled'}"})

@app.route("/api/admin/clear-logs", methods=["DELETE"])
def clear_logs():
    if session.get("role") != "owner": return jsonify({"error": "Unauthorized"}), 403
    audit_collection.delete_many({})
    return jsonify({"message": "Logs cleared"})

@app.route("/api/admin/activity-logs", methods=["GET"])
def get_logs():
    if session.get("role") not in ["owner", "admin"]: return jsonify({"error": "Unauthorized"}), 403
    logs = list(audit_collection.find({}).sort("timestamp", -1).limit(100))
    for l in logs: l["_id"] = str(l["_id"])
    return jsonify(logs)

# ---------- COMPLIANCE API (Restored Logic) ----------

@app.route("/api/compliance/overview", methods=["GET"])
def get_compliance_overview():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["branch"] = session.get("branch")

    today = datetime.now()
    
    # 1. Real Expired Count
    expired_query = {**query, "exp_date": {"$lt": today.strftime("%Y-%m-%d"), "$ne": None}}
    expired_batches = list(batches_collection.find(expired_query))
    
    # 2. Real Low Stock Count
    all_low_stock = list(inventory_collection.find({"$expr": {"$lte": ["$quantity", "$reorder_level"]}}))
    
    if session.get("role") == "staff" and session.get("branch") != "All":
        low_stock_items = [i for i in all_low_stock if i.get('branch') == session.get('branch')]
    else:
        low_stock_items = all_low_stock

    total_issues = len(expired_batches) + len(low_stock_items)
    score = max(0, 100 - (total_issues * 5))
    
    status = "Excellent"
    if score < 90: status = "Good"
    if score < 70: status = "Warning"
    if score < 50: status = "Critical"

    return jsonify({
        "score": score,
        "status": status,
        "expired_count": len(expired_batches),
        "low_stock_count": len(low_stock_items),
        "issues": total_issues
    })

# ➤ FIX: Audit Logs should show Stock Movement, NOT Admin Logins
@app.route("/api/compliance/audit-logs", methods=["GET"])
def get_audit_logs():
    query = {}
    if session.get("role") == "staff" and session.get("branch") != "All":
        query["branch"] = session.get("branch")

    # Fetch from consumption (Stock In/Out history)
    logs = list(consumption_collection.find(query).sort("date", -1).limit(50))
    for log in logs:
        log["_id"] = str(log["_id"])
    return jsonify(logs)

# ➤ NEW: Update Order Status (Approve / Reject / Receive)
@app.route("/api/orders/<order_id>/status", methods=["PUT"])
def update_order_status(order_id):
    # 1. Auth Check
    if "user_email" not in session: return jsonify({"error": "Unauthorized"}), 401
    
    role = session.get("role")
    data = request.json or {}
    new_status = data.get("status")
    
    # 2. Get Order
    order = orders_collection.find_one({"_id": ObjectId(order_id)})
    if not order: return jsonify({"error": "Order not found"}), 404

    current_status = order.get("status")

    # 3. Role Validations
    if new_status in ["approved", "rejected"] and role not in ["owner", "admin"]:
        return jsonify({"error": "Only Admins can approve/reject orders"}), 403

    # 4. LOGIC: If marking as 'Received', increase inventory stock
    if new_status == "received":
        if current_status == "received":
            return jsonify({"error": "Order already received"}), 400
            
        # Increase Stock
        item_name = order.get("item")
        branch = order.get("branch")
        qty = int(order.get("quantity", 0))
        
        result = inventory_collection.update_one(
            {"name": item_name, "branch": branch},
            {"$inc": {"quantity": qty}}
        )
        
        # Log it as "Stock In" in consumption/history
        consumption_collection.insert_one({
            "name": item_name,
            "date": datetime.now(),
            "quantity_used": qty,
            "direction": "in",
            "branch": branch,
            "reason_category": "Restock Order",
            "note": f"Order #{str(order_id)[-4:]} received"
        })

    # 5. Update Order Status
    orders_collection.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {
            "status": new_status,
            "updated_by": session.get("user_email"),
            "updated_at": datetime.now()
        }}
    )

    # 6. Audit Log
    log_behavior(session.get("user_email"), "Update Order", f"Changed order {order.get('item')} to {new_status}")

    return jsonify({"message": f"Order marked as {new_status}"}), 200

# //////////////////////////////////////// //
#      💰 FINANCE & ANALYTICS API          //
# //////////////////////////////////////// //

@app.route("/api/finances/summary", methods=["GET"])
def get_finance_summary():
    # 1. Scope Check
    role = session.get("role", "staff")
    branch = session.get("branch", "Main")
    
    query = {}
    if role == "staff" and branch != "All":
        query["branch"] = branch

    # ➤ HELPER: Safely convert anything to float (handles strings, nulls, etc.)
    def safe_num(val):
        try:
            if val is None: return 0.0
            return float(str(val).replace(',', '').strip())
        except (ValueError, TypeError):
            return 0.0

    try:
        # A. Calculate Current Inventory Asset Value
        inventory_items = list(inventory_collection.find(query, {"quantity": 1, "price": 1, "_id": 0}))
        
        total_asset_value = sum(
            (safe_num(i.get("quantity")) * safe_num(i.get("price"))) 
            for i in inventory_items
        )

        # B. INTELLIGENT TREND ANALYSIS (Last 6 Months)
        six_months_ago = datetime.now() - timedelta(days=180)
        
        history_query = {"date": {"$gte": six_months_ago}}
        if role == "staff" and branch != "All":
            history_query["branch"] = branch

        history = list(consumption_collection.find(history_query))
        
        # Get Price Map
        all_prices = {i["name"]: safe_num(i.get("price")) for i in inventory_collection.find({}, {"name": 1, "price": 1})}

        # Buckets
        monthly_data = {} 
        today = datetime.now()
        months_order = []
        
        for i in range(5, -1, -1):
            d = today - timedelta(days=i*30)
            key = d.strftime("%b") # Aug, Sep...
            months_order.append(key)
            monthly_data[key] = {"spend": 0.0, "usage": 0.0}

        current_month_spend = 0.0
        current_month_usage = 0.0
        current_month_key = today.strftime("%b")

        for record in history:
            r_date = record.get("date")
            if not r_date: continue
            
            # Handle string dates if necessary
            if isinstance(r_date, str):
                try: r_date = datetime.fromisoformat(r_date.replace("Z", ""))
                except: continue

            r_key = r_date.strftime("%b")
            if r_key not in monthly_data: continue

            item_name = record.get("name")
            qty = safe_num(record.get("quantity_used"))
            direction = record.get("direction")
            price = all_prices.get(item_name, 0.0)
            
            value = qty * price
            
            if direction == "in":
                monthly_data[r_key]["spend"] += value
                if r_key == current_month_key: current_month_spend += value
            else:
                monthly_data[r_key]["usage"] += value
                if r_key == current_month_key: current_month_usage += value

        return jsonify({
            "asset_value": total_asset_value,
            "monthly_spend": current_month_spend,
            "monthly_usage": current_month_usage,
            "chart_data": {
                "labels": months_order,
                "spend": [monthly_data[m]["spend"] for m in months_order],
                "usage": [monthly_data[m]["usage"] for m in months_order]
            },
            "currency": "PHP"
        })

    except Exception as e:
        print(f"Finance Error: {e}")
        return jsonify({"error": str(e)}), 500

# ---------- BACKGROUND TASKS ----------

def analytics_broadcaster():
    while True:
        try:
            socketio.emit("analytics_update", {"msg": "beat"}, namespace="/analytics")
        except Exception:
            pass
        time.sleep(10)

def start_background_tasks():
    socketio.start_background_task(target=analytics_broadcaster)

start_background_tasks()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
