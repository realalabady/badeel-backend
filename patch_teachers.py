"""One-time script to add verification_status to existing teacher profiles."""
import os, json
from pathlib import Path
import firebase_admin
from firebase_admin import credentials, firestore

ROOT_DIR = Path(__file__).resolve().parent

if not firebase_admin._apps:
    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_creds_json:
        cred = credentials.Certificate(json.loads(firebase_creds_json))
    else:
        cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", str(ROOT_DIR / "serviceAccountKey.json"))
        cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()
docs = db.collection("teacher_profiles").stream()
count = 0
for doc in docs:
    d = doc.to_dict()
    if "verification_status" not in d:
        doc.reference.update({"verification_status": "pending"})
        print(f"  Patched: {doc.id}")
        count += 1
    else:
        print(f"  OK: {doc.id} status={d.get('verification_status')}")

print(f"Done. Patched {count} documents.")
