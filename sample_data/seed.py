"""
Sample Data Seeder for Smart-Attendance API
Usage:
    python sample_data/seed.py
Loads sample_data/sample_data.json into MongoDB, converting ISO strings to BSON Date objects.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment configuration
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

SAMPLE_DATA_FILE = os.path.join(os.path.dirname(__file__), "sample_data.json")

def seed():
    if not os.path.exists(SAMPLE_DATA_FILE):
        print(f"Error: Sample data file not found at {SAMPLE_DATA_FILE}")
        sys.exit(1)

    print(f"Connecting to MongoDB at {MONGO_URI} (db: '{MONGO_DB}')...")
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    with open(SAMPLE_DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    employees = data.get("employees", [])
    attendance_logs = data.get("attendance_logs", [])

    print(f"Seeding {len(employees)} employees...")
    for emp in employees:
        doc = dict(emp)
        doc["created_at"] = datetime.now(timezone.utc)
        db.employees.update_one(
            {"emp_code": doc["emp_code"]},
            {"$set": doc},
            upsert=True
        )

    print(f"Seeding {len(attendance_logs)} attendance logs...")
    for log in attendance_logs:
        doc = dict(log)
        # Convert ISO date strings to datetime objects
        if doc.get("punch_in_iso"):
            doc["punch_in"] = datetime.fromisoformat(doc.pop("punch_in_iso").replace("Z", "+00:00"))
        else:
            doc.pop("punch_in_iso", None)
            doc["punch_in"] = None

        if doc.get("punch_out_iso"):
            doc["punch_out"] = datetime.fromisoformat(doc.pop("punch_out_iso").replace("Z", "+00:00"))
        else:
            doc.pop("punch_out_iso", None)
            doc["punch_out"] = None

        db.attendance_logs.update_one(
            {"emp_code": doc["emp_code"], "date": doc["date"]},
            {"$set": doc},
            upsert=True
        )

    print("=" * 60)
    print("Sample data successfully seeded!")
    print(f"Total employees in database: {db.employees.count_documents({})}")
    print(f"Total attendance logs in database: {db.attendance_logs.count_documents({})}")
    print("=" * 60)

if __name__ == "__main__":
    seed()
