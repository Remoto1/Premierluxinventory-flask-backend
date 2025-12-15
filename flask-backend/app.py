from flask import Flask, request, jsonify, session, render_template, redirect
from flask_cors import CORS
from pymongo import MongoClient
import os
import google.generativeai as genai
import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from datetime import datetime, timezone, timedelta
from flask_socketio import SocketIO
import threading
import time
import uuid
import string
import random

# ---------- Flask + CORS Setup ----------

# WE TELL FLASK TO LOOK UP ONE FOLDER (../frontend) FOR TEMPLATES
app = Flask(__name__)

CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*")
app.secret_key = "premierlux_secret_key"

# ---------- Gemini setup ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("Gemini API key not set! Some AI routes will fail.")

# ---------- MongoDB setup ----------
MONGO_URI = "mongodb+srv://dbirolliverhernandez_db_user:yqHWCWJwNxKofjHs@cluster0.bgmzgav.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI)
db = client["premierlux"]

ai_dashboard_collection = db["ai_dashboard"]
inventory_collection = db["inventory"]
branches_collection = db["branches"]
batches_collection = db["batches"]
consumption_collection = db["consumption"]
suppliers_collection = db["suppliers"]
orders_collection = db["orders"]
users_collection = db["users"]
audit_collection = db["audit_logs"]


# ---------- USER SETUP (Auto-Fix) ----------

# 1. Create Default Owner if database is empty
if users_collection.count_documents({}) == 0:
    users_collection.insert_one({
        "name": "System Owner",
        "email": "owner@example.com",
        "password": "owner123",
        "role": "owner",
        "branch": "All",
        "created_at": datetime.now()
    })

# 2. AUTO-FIX: Promote existing "Super Admin" to Owner
# This fixes the issue where your old account is stuck as "Admin"
existing_admin = users_collection.find_one({"email": "admin@example.com"})
if existing_admin and existing_admin.get("role") != "owner":
    print("Promoting Super Admin to Owner...")
    users_collection.update_one(
        {"email": "admin@example.com"},
        {"$set": {"role": "owner", "branch": "All"}}
    )


# GET CURRENT USER INFO
@app.route("/api/me", methods=["GET"])
def get_current_user():
    if "user_email" not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    return jsonify({
        "name": session.get("user_name"),
        "email": session.get("user_email"),
        "role": session.get("role"),
        "branch": session.get("branch")
    })
# ---------- PAGE ROUTES (Serving HTML) ----------

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    email = data.get("email")
    password = data.get("password")

    user = users_collection.find_one({"email": email})

    if user and user.get("password") == password:
        session.permanent = True
        session["user_email"] = user["email"]
        session["user_name"] = user.get("name", "User")
        session["role"] = user.get("role", "staff")
        session["branch"] = user.get("branch", "Main") # <--- SAVE BRANCH
        
        log_behavior(user["email"], "Login", "User logged into the system")
        
        return jsonify({
            "message": "Login successful",
            "role": user.get("role"),
            "name": user.get("name"),
            "branch": user.get("branch")
        }), 200

    return jsonify({"error": "Invalid email or password"}), 401
# 2. Protected Dashboard (Home)
@app.route("/")
def home():
    # If NOT logged in, kick them to login page
    if "user_email" not in session:
        return redirect("/login")
    
    # If logged in, show the dashboard
    return render_template("index.html")

# Serve the Login Page (HTML)
@app.route("/login")
def login_page():
    return render_template("login.html")


# ---------- USER MANAGEMENT & LOGGING ----------

# Helper Function: Behavioral Logging
def log_behavior(user_email, action, details):
    audit_collection.insert_one({
        "user": user_email,
        "action": action,
        "details": details,
        "timestamp": datetime.now()
    })

@app.route("/api/users", methods=["GET"])
def get_users():
    # Only allow admins to see user list (Basic check)
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    users = list(users_collection.find({}, {"password": 0})) # Exclude passwords
    for u in users:
        u["_id"] = str(u["_id"])
    return jsonify(users), 200

@app.route("/api/users", methods=["POST"])
def create_user():
    # Allow Owner and Admin to create users
    if session.get("role") not in ["owner", "admin"]:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    if not data.get("email") or not data.get("password"):
        return jsonify({"error": "Email and Password required"}), 400
    
    if users_collection.find_one({"email": data["email"]}):
        return jsonify({"error": "Email already exists"}), 400

    new_user = {
        "name": data.get("name", "New User"),
        "email": data["email"],
        "password": data["password"],
        "role": data.get("role", "staff"),
        "branch": data.get("branch", "Main"), # <--- NEW FIELD
        "created_at": datetime.now()
    }
    
    users_collection.insert_one(new_user)
    log_behavior(session.get("user_email"), "Create User", f"Created {data['role']} for {data.get('branch')}")
    
    return jsonify({"message": "User created"}), 201

@app.route("/api/users/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    current_role = session.get("role")
    
    # 1. Security Check: Only Owner and Admin can access delete functions
    if current_role not in ["owner", "admin"]:
        return jsonify({"error": "Unauthorized"}), 403

    from bson.objectid import ObjectId
    target_user = users_collection.find_one({"_id": ObjectId(user_id)})
    
    if not target_user:
        return jsonify({"error": "User not found"}), 404

    target_role = target_user.get("role")

    # 2. Hierarchy Rules
    # Rule A: No one can delete the Owner
    if target_role == "owner":
        return jsonify({"error": "Cannot delete the System Owner"}), 403
    
    # Rule B: Admins CANNOT delete other Admins (Only Owner can)
    if current_role == "admin" and target_role == "admin":
        return jsonify({"error": "Only the Owner can delete Administrators"}), 403

    # Rule C: Admins can only delete Staff
    if current_role == "admin" and target_role != "staff":
        return jsonify({"error": "Admins can only delete Staff accounts"}), 403

    # If we pass all checks, proceed
    users_collection.delete_one({"_id": ObjectId(user_id)})
    
    log_behavior(session.get("user_email"), "Delete User", f"Deleted user {target_user.get('email')}")
    return jsonify({"message": "User deleted"}), 200
# ---------- INVENTORY CRUD + QUERIES ----------

@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    items = list(inventory_collection.find({}, {"_id": 0}))
    return jsonify(items)

@app.route("/api/inventory", methods=["POST"])
def add_inventory():
    data = request.json or {}
    inventory_collection.insert_one(data)
    return jsonify({"message": "Item added"}), 201

@app.route("/api/inventory/<string:item_name>", methods=["DELETE"])
def delete_inventory(item_name):
    inventory_collection.delete_one({"name": item_name})
    batches_collection.delete_many({"item_name": item_name})
    
    return jsonify({"message": "Item and associated batches deleted"})

@app.route("/api/inventory/branch/<string:branch_name>", methods=["GET"])
def get_inventory_by_branch(branch_name):
    items = list(inventory_collection.find({"branch": branch_name}, {"_id": 0}))
    return jsonify(items)

@app.route("/api/inventory/lowstock", methods=["GET"])
def get_low_stock():
    items = list(
        inventory_collection.find(
            {"$expr": {"$lte": ["$quantity", "$reorder_level"]}},
            {"_id": 0},
        )
    )
    return jsonify(items)

@app.route("/api/inventory/<name>/adjust", methods=["POST"])
def adjust_inventory(name):
    data = request.json or {}
    branch = data.get("branch")
    delta = int(data.get("delta", 0))
    # NEW: Get the category (default to 'Manual Adjustment' if missing)
    reason_cat = data.get("reason_category", "Manual Adjustment") 
    note = data.get("note", "")

    if delta == 0:
        return jsonify({"error": "non-zero delta required"}), 400

    query = {"name": name}
    if branch:
        query["branch"] = branch

    inv = inventory_collection.find_one(query)
    if not inv:
        return jsonify({"error": "item not found"}), 404

    new_qty = max(0, int(inv.get("quantity", 0)) + delta)
    inventory_collection.update_one(
        {"_id": inv["_id"]},
        {"$set": {"quantity": new_qty}},
    )

    # SAVE TO LOGS
    consumption_collection.insert_one({
        "name": name,
        "date": datetime.utcnow(),
        "quantity_used": abs(delta),
        "direction": "out" if delta < 0 else "in",
        "branch": branch or inv.get("branch"),
        "reason_category": reason_cat,  # <--- SAVING IT HERE
        "note": note
    })

    return jsonify({"status": "ok", "quantity": new_qty})

# ---------- BATCHES ----------
@app.route("/api/batches", methods=["GET"])
def get_batches():
    # Fetch all fields. Convert _id to string for JSON compatibility.
    batches = list(batches_collection.find({}))
    for b in batches:
        b['_id'] = str(b['_id']) # Convert ObjectId to string
    return jsonify(batches), 200

@app.route("/api/batches", methods=["POST"])
def create_batch():
    data = request.get_json(force=True)

    if not data or "item_name" not in data or "branch" not in data:
        return jsonify({"error": "item_name and branch are required"}), 400

    # --- AUTOMATION LOGIC START ---
    
    # 1. Auto-Generate Batch # (Format: BATCH-YYYYMMDD-XXXX)
    # If user provided one, use it. Otherwise, generate it.
    auto_batch = data.get("batch_number")
    if not auto_batch:
        date_str = datetime.now().strftime("%Y%m%d")
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        auto_batch = f"BTN-{date_str}-{suffix}"

    # 2. Auto-Generate Lot # (Format: LOT-YYYYMMDD)
    auto_lot = data.get("lot_number")
    if not auto_lot:
        auto_lot = f"LOT-{datetime.now().strftime('%Y%m%d')}"

    # 3. Auto-Generate Supplier Batch (Placeholder if empty)
    auto_supp = data.get("supplier_batch")
    if not auto_supp:
        auto_supp = "SUP-NA-" + datetime.now().strftime("%H%M") # e.g. SUP-NA-1430

    # 4. Auto-Generate QR ID (Unique UUID)
    auto_qr = data.get("qr_code_id")
    if not auto_qr:
        auto_qr = str(uuid.uuid4())[:8].upper() # Short unique ID e.g. A1B2C3D4

    # --- AUTOMATION LOGIC END ---

    batch_doc = {
        "item_name": data.get("item_name"),
        "sku": data.get("sku"),
        "branch": data.get("branch"),
        "current_stock": data.get("current_stock", 0),
        "monthly_usage": data.get("monthly_usage", 0),
        "price": data.get("price", 0),
        "reorder_level": data.get("reorder_level", 0),
        
        # USE AUTO VALUES HERE
        "batch_number": auto_batch,
        "lot_number": auto_lot,
        "supplier_batch": auto_supp,
        "qr_code_id": auto_qr,
        
        "mfg_date": data.get("mfg_date") or None,
        "exp_date": data.get("exp_date") or None,
        "category": data.get("category") or "Uncategorized",
    }

    result = batches_collection.insert_one(batch_doc)
    batch_doc["_id"] = str(result.inserted_id)

    # Update main inventory count
    inventory_collection.update_one(
        {"name": batch_doc["item_name"], "branch": batch_doc["branch"]},
        {
            "$setOnInsert": {
                "reorder_level": batch_doc["reorder_level"],
                "price": batch_doc["price"],
            },
            "$set": { "category": batch_doc["category"] },
            "$inc": { "quantity": batch_doc["current_stock"] },
        },
        upsert=True,
    )

    return jsonify({"status": "ok", "batch": batch_doc}), 201

# ---------- AI: SIMPLE FORECASTING ----------

@app.route("/api/forecast/<item_name>", methods=["GET"])
def forecast_item(item_name):
    try:
        history = list(
            consumption_collection.find(
                {"name": item_name},
                {"_id": 0, "date": 1, "quantity_used": 1},
            )
        )

        if not history:
            return jsonify(
                {
                    "item": item_name,
                    "message": "No consumption history found for this item.",
                    "forecast": [],
                }
            ), 200

        history_sorted = sorted(
            history,
            key=lambda x: datetime.strptime(x["date"], "%Y-%m-%d"),
        )
        y = np.array([h["quantity_used"] for h in history_sorted], dtype=float)

        if len(y) < 3:
            avg = float(np.mean(y))
            forecast_values = [avg] * 7
        else:
            model = ExponentialSmoothing(y, trend=None, seasonal=None)
            model_fit = model.fit(optimized=True)
            forecast_values = model_fit.forecast(7).tolist()

        return jsonify(
            {
                "item": item_name,
                "history_points": len(y),
                "forecast_horizon_days": 7,
                "daily_forecast": forecast_values,
            }
        ), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- AI DASHBOARD ----------

@app.route("/api/ai/dashboard", methods=["GET"])
def get_ai_dashboard():
    doc = ai_dashboard_collection.find_one({"_id": "summary"}) or {}
    if doc:
        doc["updated_at"] = doc.get("updated_at")
    return jsonify({
        "summary_text": doc.get("summary_text", ""),
        "risk_text": doc.get("risk_text", ""),
        "updated_at": doc.get("updated_at"),
    }), 200

@app.route("/api/ai/dashboard/refresh", methods=["POST"])
def refresh_ai_dashboard():
    items = list(inventory_collection.find({}, {"_id": 0}))
    total_items = len(items)
    total_cost = float(sum((i.get("price", 0) or 0) * (i.get("quantity", 0) or 0) for i in items))
    low_stock = [i for i in items if i.get("quantity", 0) <= i.get("reorder_level", 0)]

    inventory_brief = {
        "total_items": total_items,
        "total_cost": total_cost,
        "low_stock_count": len(low_stock),
        "low_stock_names": [i.get("name") for i in low_stock][:10],
    }

    if total_items == 0:
        summary_text = (
            "No items in inventory yet. Add your first batch to start tracking stock and cost."
        )
        risk_text = (
            "Main risk is missing data. Add core consumables and instruments first to build history."
        )
    else:
        low_names_str = ", ".join(inventory_brief["low_stock_names"]) or "none"
        summary_text = (
            f"Inventory has {total_items} items with an estimated total cost of ₱{total_cost:.2f}. "
            f"{len(low_stock)} items are at or below reorder level."
        )
        risk_text = (
            f"Focus on low stock items ({low_names_str}). "
            "Plan purchase orders in the next 7 days to avoid stockouts and spread costs."
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    ai_dashboard_collection.update_one(
        {"_id": "summary"},
        {
            "$set": {
                "summary_text": summary_text,
                "risk_text": risk_text,
                "updated_at": now_iso,
                "total_items": total_items,
                "total_cost": total_cost,
                "low_stock_count": len(low_stock),
            }
        },
        upsert=True,
    )

    return jsonify({
        "summary_text": summary_text,
        "risk_text": risk_text,
        "updated_at": now_iso,
        "total_items": total_items,
        "total_cost": total_cost,
        "low_stock_count": len(low_stock),
    }), 200

# ---------- CHAT ----------

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    if not GEMINI_API_KEY:
        return jsonify(
            {
                "type": "info",
                "text": "Gemini API key is not configured. Ask your admin to set GEMINI_API_KEY.",
            }
        ), 200

    inventory = list(inventory_collection.find({}, {"_id": 0}).limit(50))
    lower_msg = user_message.lower()

    if "low stock" in lower_msg or "reorder" in lower_msg:
        low_stock_items = list(
            inventory_collection.find(
                {"$expr": {"$lte": ["$quantity", "$reorder_level"]}},
                {"_id": 0},
            )
        )
        return jsonify(
            {
                "type": "low_stock_summary",
                "text": f"Found {len(low_stock_items)} low stock or reorder items.",
                "items": low_stock_items,
            }
        ), 200

    try:
        genai_reply = genai.generate_content(
            model="gemini-1.0-pro", contents=user_message
        )
        answer_text = (
            genai_reply.text if hasattr(genai_reply, "text") else str(genai_reply)
        )
        return jsonify({"type": "llm_answer", "text": answer_text}), 200
    except Exception as e:
        return jsonify(
            {"type": "error", "text": "Gemini call failed.", "details": str(e)}
        ), 500

# ---------- BRANCHES ----------

@app.route("/api/branches", methods=["GET"])
def get_branches():
    branches = list(branches_collection.find({}, {"_id": 0}))
    return jsonify(branches)

@app.route("/api/branches", methods=["POST"])
def add_branch():
    data = request.json or {}
    if not data.get("name"):
        return jsonify({"error": "Branch name is required"}), 400

    doc = {
        "name": data["name"],
        "address": data.get("address", ""),
        "manager": data.get("manager", ""),
    }
    branches_collection.insert_one(doc)
    return jsonify({"message": "Branch added"}), 201

# ---------- KPI ENDPOINTS ----------

@app.route("/api/low-stock-count", methods=["GET"])
def api_low_stock_count():
    try:
        count = inventory_collection.count_documents({
            "$expr": {"$lte": ["$quantity", "$reorder_level"]}
        })
        return jsonify({"count": int(count)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/total-inventory", methods=["GET"])
def api_total_inventory():
    try:
        cursor = inventory_collection.find({}, {"price": 1, "quantity": 1})
        total_value = 0.0
        for doc in cursor:
            price = float(doc.get("price") or 0)
            qty = float(doc.get("quantity") or 0)
            total_value += price * qty
        return jsonify({"value": total_value}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/branches-count", methods=["GET"])
def api_branches_count():
    try:
        count = branches_collection.count_documents({})
        return jsonify({"count": int(count)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- SUPPLIERS ----------

@app.route("/api/suppliers", methods=["GET"])
def get_suppliers():
    suppliers = list(suppliers_collection.find({}, {"_id": 0}))
    return jsonify(suppliers), 200


# ... inside app.py ...

@app.route("/api/suppliers", methods=["POST"])
def add_supplier():
    data = request.json or {}
    if not data.get("name"):
        return jsonify({"error": "Supplier name is required"}), 400

    doc = {
        "name": data["name"],
        "contact": data.get("contact", ""),
        "phone": data.get("phone", ""),
        "lead_time_days": data.get("lead_time_days", 0),
        # NEW FIELDS ADDED HERE
        "website": data.get("website", ""),
        "notes": data.get("notes", "")
    }
    suppliers_collection.insert_one(doc)
    return jsonify({"message": "Supplier added"}), 201

@app.route("/api/suppliers/<string:name>", methods=["POST"])
def update_supplier(name):
    data = request.json or {}
    
    update_fields = {
        "contact": data.get("contact"),
        "phone": data.get("phone"),
        "lead_time_days": data.get("lead_time_days"),
        # NEW FIELDS ADDED HERE
        "website": data.get("website"),
        "notes": data.get("notes")
    }
    
    # Remove None values
    update_fields = {k: v for k, v in update_fields.items() if v is not None}

    result = suppliers_collection.update_one(
        {"name": name},
        {"$set": update_fields}
    )
    
    if result.matched_count == 0:
        return jsonify({"error": "Supplier not found"}), 404
        
    return jsonify({"message": "Supplier updated"}), 200


@app.route("/api/suppliers/<string:name>", methods=["DELETE"])
def delete_supplier(name):
    result = suppliers_collection.delete_one({"name": name})
    if result.deleted_count == 0:
        return jsonify({"error": "Supplier not found"}), 404
    return jsonify({"message": "Supplier deleted"}), 200


# ---------- ORDERS (Restock Requests) ----------

@app.route("/api/orders", methods=["GET"])
def get_orders():
    # Fetch all orders, newest first
    orders = list(orders_collection.find({}).sort("created_at", -1))
    for o in orders:
        o["_id"] = str(o["_id"])  # Convert ObjectId to string
    return jsonify(orders), 200

@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.json or {}
    
    # Simple validation
    if not data.get("item") or not data.get("quantity"):
        return jsonify({"error": "Item and Quantity are required"}), 400

    # Ensure status is set
    if "status" not in data:
        data["status"] = "pending"
    
    # Save to database
    result = orders_collection.insert_one(data)
    data["_id"] = str(result.inserted_id)

    return jsonify({"message": "Order created successfully", "order": data}), 201

# ---------- ALERTS ----------

@app.post("/api/alerts/<alert_id>/acknowledge")
def acknowledge_alert(alert_id):
    user_id = request.headers.get("X-User-Id", "demo-admin")
    user_name = request.headers.get("X-User-Name", "Demo Admin")

    doc = {
        "alert_id": alert_id,
        "user_id": user_id,
        "user_name": user_name,
        "acknowledged_at": datetime.utcnow().isoformat() + "Z",
    }

    db.alert_acknowledgements.insert_one(doc)
    return jsonify({"status": "ok"})

@app.get("/api/alerts")
def get_alerts():
    user_id = request.headers.get("X-User-Id", "demo-admin")

    alerts = []
    low_by_branch = {}

    items = list(inventory_collection.find({}))

    for item in items:
        name = item.get("name")
        branch = item.get("branch", "Main branch")
        qty = item.get("quantity", 0)
        reorder = item.get("reorder_level", 0)
        expiry = item.get("expiry_date")

        if reorder and qty <= reorder:
            alerts.append({
                "id": f"low-stock-{branch}-{name}",
                "type": "low_stock",
                "severity": "high",
                "title": f"Low stock: {name} – {branch}",
                "description": f"Current stock {qty}, below reorder point {reorder}.",
                "branch": branch,
            })
            low_by_branch[branch] = low_by_branch.get(branch, 0) + 1

        if expiry and expiry_within_days(expiry, 30):
            alerts.append({
                "id": f"expiry-{branch}-{name}",
                "type": "expiry_risk",
                "severity": "medium",
                "title": f"Expiry soon: {name} – {branch}",
                "description": "Batch expiring within 30 days.",
                "branch": branch,
            })

    for branch, count in low_by_branch.items():
        if count >= 3:
            alerts.append({
                "id": f"branch-low-{branch}",
                "type": "branch_low_stock",
                "severity": "high",
                "title": f"Branch alert: {branch} has {count} low‑stock items",
                "description": "Review this branch inventory and create replenishment orders.",
                "branch": branch,
            })

    acked_ids = {
        doc["alert_id"]
        for doc in db.alert_acknowledgements.find(
            {"user_id": user_id}, {"alert_id": 1, "_id": 0}
        )
    }

    visible_alerts = [a for a in alerts if a["id"] not in acked_ids]
    return jsonify(visible_alerts)

def expiry_within_days(expiry_value, days):
    if not expiry_value:
        return False
    if isinstance(expiry_value, datetime):
        expiry_dt = expiry_value
    else:
        try:
            expiry_dt = datetime.fromisoformat(str(expiry_value))
        except ValueError:
            return False
    return expiry_dt <= datetime.utcnow() + timedelta(days=days)

# ---------- REPLENISHMENT ----------

@app.get("/api/replenishment/recommendations")
def get_replenishment_recommendations():
    items = list(inventory_collection.find({}))

    recommendations = []

    for item in items:
        name = item.get("name")
        branch = item.get("branch", "Main")
        qty = item.get("quantity", 0)
        reorder = item.get("reorder_level", 0)

        avg_daily_usage = item.get("avg_daily_usage", 1)
        lead_time_days = item.get("lead_time_days", 7)
        safety_stock = item.get("safety_stock", reorder or 0)

        reorder_point = avg_daily_usage * lead_time_days + safety_stock

        trigger_level = max(reorder, reorder_point)
        if qty <= trigger_level:
            target_stock = avg_daily_usage * (lead_time_days + 7) + safety_stock
            suggested_qty = max(int(target_stock - qty), 0)

            if suggested_qty > 0:
                recommendations.append({
                    "name": name,
                    "branch": branch,
                    "current_quantity": qty,
                    "reorder_level": reorder,
                    "reorder_point": reorder_point,
                    "suggested_order_qty": suggested_qty,
                })

    return jsonify(recommendations)

# ---------- ANALYTICS REST ENDPOINTS ----------

@app.get("/analytics/overview")
def analytics_overview():
    new_items = inventory_collection.count_documents({
        "created_at": {"$gte": datetime.now() - timedelta(days=7)}
    })

    batches_7d = batches_collection.count_documents({
        "mfg_date": {"$gte": datetime.now() - timedelta(days=7)}
    })

    total_items = inventory_collection.count_documents({})
    branches = branches_collection.count_documents({})

    return jsonify({
        "new_items": new_items,
        "batches_7d": batches_7d,
        "total_items": total_items,
        "branches": branches,
    })

@app.get("/analytics/movement")
def analytics_movement():
    today = datetime.now()
    start_date = today - timedelta(days=6)

    labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    stock_in = [0] * 7
    stock_out = [0] * 7
    low_stock = [0] * 7

    batches = list(batches_collection.find({}))
    for b in batches:
        mfg = b.get("mfg_date")
        if not mfg:
            continue
        if isinstance(mfg, str):
            mfg = datetime.fromisoformat(mfg)
        if mfg < start_date:
            continue
        day = mfg.weekday()
        stock_in[day] += b.get("current_stock", 0)

    usage = list(consumption_collection.find({}))
    for u in usage:
        used_date = u.get("date")
        if not used_date:
            continue
        if isinstance(used_date, str):
            used_date = datetime.fromisoformat(used_date)
        if used_date < start_date:
            continue
        day = used_date.weekday()
        qty = u.get("quantity_used", 0)
        if u.get("direction") == "in":
            stock_in[day] += qty
        else:
            stock_out[day] += qty

    low_stock_count = inventory_collection.count_documents({
        "$expr": {"$lte": ["$quantity", "$reorder_level"]}
    })
    low_stock = [low_stock_count] * 7

    return jsonify({
        "labels": labels,
        "stock_in": stock_in,
        "stock_out": stock_out,
        "low_stock": low_stock,
    })

@app.get("/analytics/movement-monthly")
def analytics_movement_monthly():
    now = datetime.now()
    months = []
    for i in range(11, -1, -1):
        first = (now.replace(day=1) - timedelta(days=30 * i))
        months.append((first.year, first.month))

    month_labels = [datetime(y, m, 1).strftime("%b %Y") for (y, m) in months]
    month_in = [0] * 12
    month_out = [0] * 12

    oldest_year, oldest_month = months[0]
    since = datetime(oldest_year, oldest_month, 1)

    usage = list(consumption_collection.find({"date": {"$gte": since}}))
    for u in usage:
        d = u.get("date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d)
        ym = (d.year, d.month)
        if ym not in months:
            continue
        idx = months.index(ym)
        qty = u.get("quantity_used", 1)
        if u.get("direction") == "in":
            month_in[idx] += qty
        else:
            month_out[idx] += qty

    return jsonify({
        "labels": month_labels,
        "stock_in": month_in,
        "stock_out": month_out,
    })


@app.get("/analytics/category")
def analytics_category():
    pipeline = [
        {"$match": {"category": {"$ne": None}}},
        {"$group": {"_id": "$category", "total": {"$sum": "$quantity"}}},
        {"$project": {"_id": 0, "id": "$_id", "total": 1}},
    ]
    return jsonify(list(inventory_collection.aggregate(pipeline)))


@app.get("/analytics/low-stock")
def analytics_low_stock():
    return jsonify(list(inventory_collection.find(
        {"$expr": {"$lte": ["$quantity", "$reorder_level"]}},
        {"_id": 0, "name": 1, "quantity": 1},
    )))

@app.get("/analytics/top-products")
def analytics_top_products():
    return jsonify(list(consumption_collection.aggregate([
        {"$group": {"_id": "$name", "used": {"$sum": "$quantity_used"}}},
        {"$sort": {"used": -1}},
        {"$limit": 5},
    ])))

@app.route("/api/analytics/branch-stock", methods=["GET"])
def analytics_branch_stock():
    pipeline = [
        {"$group": {
            "_id": "$branch",
            "total_qty": {"$sum": "$quantity"},
        }},
        {"$sort": {"_id": 1}},
    ]
    results = list(inventory_collection.aggregate(pipeline))
    labels = [r["_id"] or "Unassigned" for r in results]
    values = [r["total_qty"] for r in results]
    return jsonify({"labels": labels, "values": values}), 200

# ---------- SOCKET ANALYTICS BROADCASTER ----------

def build_analytics_payload():
    new_items = inventory_collection.count_documents({
        "created_at": {"$gte": datetime.now() - timedelta(days=7)}
    })
    batches_7d = batches_collection.count_documents({
        "mfg_date": {"$gte": datetime.now() - timedelta(days=7)}
    })
    total_items = inventory_collection.count_documents({})
    branches = branches_collection.count_documents({})

    overview = {
        "new_items": new_items,
        "batches_7d": batches_7d,
        "total_items": total_items,
        "branches": branches,
    }

    today = datetime.now()
    start_week = today - timedelta(days=6)
    week_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    week_in = [0] * 7
    week_out = [0] * 7

    batches = list(batches_collection.find({"mfg_date": {"$gte": start_week}}))
    for b in batches:
        d = b.get("mfg_date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d)
        if d < start_week:
            continue
        day = d.weekday()  # 0=Mon..6=Sun
        week_in[day] += b.get("current_stock", b.get("quantity", 0) or 0)

    usage = list(consumption_collection.find({"date": {"$gte": start_week}}))
    for u in usage:
        d = u.get("date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d)
        if d < start_week:
            continue
        day = d.weekday()
        qty = u.get("quantity_used", 1)
        if u.get("direction") == "in":
            week_in[day] += qty
        else:
            week_out[day] += qty

    weekly_movement = {
        "labels": week_labels,
        "stock_in": week_in,
        "stock_out": week_out,
    }

    now = datetime.now()
    months = []
    for i in range(11, -1, -1):
        first_of_month = (now.replace(day=1) - timedelta(days=30 * i))
        months.append((first_of_month.year, first_of_month.month))

    month_labels = [
        datetime(y, m, 1).strftime("%b %Y")
        for (y, m) in months
    ]
    month_in = [0] * 12
    month_out = [0] * 12

    oldest_year, oldest_month = months[0]
    since = datetime(oldest_year, oldest_month, 1)

    usage_all = list(consumption_collection.find({"date": {"$gte": since}}))
    for u in usage_all:
        d = u.get("date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d)
        ym = (d.year, d.month)
        if ym not in months:
            continue
        idx = months.index(ym)
        qty = u.get("quantity_used", 1)
        if u.get("direction") == "in":
            month_in[idx] += qty
        else:
            month_out[idx] += qty

    monthly_movement = {
        "labels": month_labels,
        "stock_in": month_in,
        "stock_out": month_out,
    }

    low_stock_rows = list(inventory_collection.find(
        {"$expr": {"$lte": ["$quantity", "$reorder_level"]}},
        {"_id": 0, "name": 1, "quantity": 1},
    ))

    top_products = list(consumption_collection.aggregate([
        {"$group": {"_id": "$name", "used": {"$sum": "$quantity_used"}}},
        {"$sort": {"used": -1}},
        {"$limit": 5},
    ]))

    return {
        "overview": overview,
        "movement": weekly_movement,
        "movement_monthly": monthly_movement,
        "low_stock": low_stock_rows,
        "top_products": top_products,
    }


def analytics_broadcaster():
    while True:
        try:
            payload = build_analytics_payload()
            socketio.emit("analytics_update", payload, namespace="/analytics")
        except Exception as e:
            print("Analytics broadcaster error:", e)
        time.sleep(5)


@socketio.on("connect", namespace="/analytics")
def analytics_connect():
    print("Client connected to analytics")

# ---------- COMPLIANCE API ----------

@app.route("/api/compliance/overview", methods=["GET"])
def get_compliance_overview():
    # 1. Check for Expired Items (Critical Compliance Issue)
    today = datetime.now()
    # Find batches where exp_date < today
    expired_batches = list(batches_collection.find({
        "exp_date": {"$lt": today.strftime("%Y-%m-%d")}
    }))
    
    # 2. Check for Low Stock (Operational Compliance Issue)
    low_stock_items = list(inventory_collection.find(
        {"$expr": {"$lte": ["$quantity", "$reorder_level"]}}
    ))

    total_issues = len(expired_batches) + len(low_stock_items)
    
    # Calculate Score (Simple logic: Start at 100, deduct 5 per issue, min 0)
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

@app.route("/api/compliance/audit-logs", methods=["GET"])
def get_audit_logs():
    # Fetch recent stock movements (last 50)
    logs = list(consumption_collection.find({}).sort("date", -1).limit(50))
    
    # Clean up _id for JSON
    for log in logs:
        log["_id"] = str(log["_id"])
        
    return jsonify(logs)


@app.route("/debug/batch", methods=["POST"])
def debug_batch():
    data = request.get_json(force=True)
    try:
        batch_doc = {
            "item_name": data.get("item_name"),
            "branch": data.get("branch"),
            "current_stock": data.get("current_stock", 0),
            "category": data.get("category") or "Uncategorized",
        }
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    # ---------- AUTHENTICATION APIs (Missing!) ----------


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"message": "Logged out"}), 200

# ---------- Run server ----------

if __name__ == "__main__":
    t = threading.Thread(target=analytics_broadcaster, daemon=True)
    t.start()
    socketio.run(app, debug=True)