"""One-time script to create an admin user."""
from server import db, bcrypt
import uuid
from datetime import datetime, timezone

existing = list(db.collection('users').where('role', '==', 'admin').limit(1).stream())
if existing:
    u = existing[0].to_dict()
    print("Admin already exists:", u["email"])
else:
    user_id = "user_" + uuid.uuid4().hex[:12]
    hashed = bcrypt.hashpw("Admin123".encode(), bcrypt.gensalt())
    doc = {
        "user_id": user_id,
        "email": "admin@badeel.com",
        "name": "Admin",
        "phone": "",
        "city": "",
        "picture": None,
        "role": "admin",
        "password_hash": hashed.decode(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection("users").document(user_id).set(doc)
    print("Admin created: admin@badeel.com / Admin123")
