from fastapi import FastAPI, APIRouter, HTTPException, Depends, Cookie, Response, Request, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, firestore
import os
import logging
import json
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, field_validator
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import httpx
import resend
import asyncio
from collections import defaultdict
import time

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# ============= RATE LIMITER =============
class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        self.requests[key] = [t for t in self.requests[key] if now - t < self.window_seconds]
        if len(self.requests[key]) >= self.max_requests:
            return False
        self.requests[key].append(now)
        return True

auth_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)

# Firebase initialization
firebase_creds_json = os.environ.get('FIREBASE_CREDENTIALS')
if firebase_creds_json:
    cred_dict = json.loads(firebase_creds_json)
    cred = credentials.Certificate(cred_dict)
else:
    cred_path = os.environ.get('FIREBASE_CREDENTIALS_PATH', str(ROOT_DIR / 'serviceAccountKey.json'))
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)
db = firestore.client()

# Resend configuration
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")

# Uploads directory
UPLOADS_DIR = ROOT_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# Security
security = HTTPBearer(auto_error=False)

logger = logging.getLogger(__name__)

# ============= MODELS =============

class User(BaseModel):
    user_id: str
    email: EmailStr
    name: str
    phone: Optional[str] = None
    city: Optional[str] = None
    picture: Optional[str] = None
    role: Literal["admin", "school_admin", "teacher", "student", "guardian"]
    linked_student_id: Optional[str] = None  # For guardians linking to their child's account
    created_at: str

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    phone: Optional[str] = None
    city: Optional[str] = None
    role: Literal["admin", "school_admin", "teacher", "student", "guardian"]

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class SessionExchangeRequest(BaseModel):
    session_id: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str

class TeacherProfile(BaseModel):
    teacher_id: str
    user_id: str
    specialization: str
    grades: List[str]
    cities: List[str]
    availability: str
    remote_enabled: bool = True
    in_person_enabled: bool = True
    bio: Optional[str] = None
    education_stages: Optional[List[str]] = None
    certificate_url: Optional[str] = None
    availability_schedule: Optional[dict] = None
    region: Optional[str] = None
    verification_status: Literal["pending", "verified", "rejected"] = "pending"
    created_at: str

class TeacherProfileCreate(BaseModel):
    specialization: str
    grades: List[str]
    cities: List[str]
    availability: str
    remote_enabled: bool = True
    in_person_enabled: bool = True
    bio: Optional[str] = None
    education_stages: Optional[List[str]] = None
    certificate_url: Optional[str] = None
    availability_schedule: Optional[dict] = None
    region: Optional[str] = None

class StudentProfile(BaseModel):
    student_id: str
    user_id: str
    age: Optional[int] = None
    grade: str
    preferred_subjects: List[str] = []
    special_needs: Optional[str] = None
    created_at: str

class StudentProfileCreate(BaseModel):
    age: Optional[int] = None
    grade: str
    preferred_subjects: List[str] = []
    special_needs: Optional[str] = None

class School(BaseModel):
    school_id: str
    name: str
    admin_user_id: str
    city: str
    contact_email: Optional[EmailStr] = None
    school_type: Optional[Literal["government", "private", "special_education"]] = None
    education_stage: Optional[str] = None
    created_at: str

class SchoolCreate(BaseModel):
    name: str
    city: str
    contact_email: Optional[EmailStr] = None
    school_type: Optional[Literal["government", "private", "special_education"]] = None
    education_stage: Optional[str] = None

class TeachingRequest(BaseModel):
    request_id: str
    created_by_user_id: str
    service_type: Literal["substitute", "remote_school", "special_education", "private_lesson"]
    subject: str
    grade: Optional[str] = None
    mode: Literal["in_person", "remote"]
    city: Optional[str] = None
    date_time: Optional[str] = None
    notes: Optional[str] = None
    school_type: Optional[Literal["government", "private", "special_education"]] = None
    education_stage: Optional[str] = None
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    coverage_duration: Optional[str] = None
    school_name: Optional[str] = None
    status: Literal["new", "offered", "accepted", "assigned", "in_progress", "completed", "cancelled"] = "new"
    created_at: str

class TeachingRequestCreate(BaseModel):
    service_type: Literal["substitute", "remote_school", "special_education", "private_lesson"]
    subject: str
    grade: Optional[str] = None
    mode: Literal["in_person", "remote"]
    city: Optional[str] = None
    date_time: Optional[str] = None
    notes: Optional[str] = None
    school_type: Optional[Literal["government", "private", "special_education"]] = None
    education_stage: Optional[str] = None
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    coverage_duration: Optional[str] = None
    school_name: Optional[str] = None

class SubstituteSubscription(BaseModel):
    subscription_id: str
    user_id: str
    status: Literal["pending", "active", "inactive", "expired"] = "pending"
    requested_at: str
    approved_at: Optional[str] = None
    expires_at: Optional[str] = None

class SchoolSubscription(BaseModel):
    subscription_id: str
    school_id: str
    admin_user_id: str
    plan: Literal["basic", "pro", "advance"]
    billing_cycle: Literal["monthly", "annual"]
    request_limit: int  # 5, 10, or -1 (unlimited)
    requests_used: int = 0
    status: Literal["active", "inactive", "expired"] = "active"
    payment_status: Literal["paid", "unpaid"] = "unpaid"
    started_at: str
    expires_at: str

class SchoolSubscriptionCreate(BaseModel):
    plan: Literal["basic", "pro", "advance"]
    billing_cycle: Literal["monthly", "annual"]

SCHOOL_PLAN_LIMITS = {"basic": 5, "advance": 10, "pro": -1}

class SubstituteProfileCreate(BaseModel):
    specializations: List[str]
    education_stages: List[str]  # early_childhood, primary, middle, secondary, special_education
    city: Optional[str] = None
    region: Optional[str] = None
    availability_mode: Literal["in_person", "remote", "both"] = "both"
    availability_schedule: Optional[dict] = None  # {"sunday": true, "monday": true, ...}
    bio: Optional[str] = None
    certificate_url: Optional[str] = None

class SubstituteProfile(BaseModel):
    profile_id: str
    user_id: str
    specializations: List[str]
    education_stages: List[str]
    city: Optional[str] = None
    region: Optional[str] = None
    availability_mode: Literal["in_person", "remote", "both"] = "both"
    availability_schedule: Optional[dict] = None
    bio: Optional[str] = None
    certificate_url: Optional[str] = None
    created_at: str

class RequestOffer(BaseModel):
    offer_id: str
    request_id: str
    teacher_id: str
    status: Literal["pending", "accepted", "declined"] = "pending"
    created_at: str

class Assignment(BaseModel):
    assignment_id: str
    request_id: str
    teacher_id: str
    student_id: str
    meeting_link: Optional[str] = None
    jitsi_room: Optional[str] = None
    notes: Optional[str] = None
    status: Literal["scheduled", "in_progress", "completed", "cancelled"] = "scheduled"
    created_at: str
    completed_at: Optional[str] = None
    teacher_name: Optional[str] = None
    student_name: Optional[str] = None

class AssignmentUpdate(BaseModel):
    meeting_link: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[Literal["scheduled", "in_progress", "completed", "cancelled"]] = None

class Review(BaseModel):
    review_id: str
    assignment_id: str
    reviewer_id: str
    teacher_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None
    created_at: str

class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None

class Notification(BaseModel):
    notification_id: str
    user_id: str
    type: str
    title: str
    message: str
    link: Optional[str] = None
    is_read: bool = False
    created_at: str

class FileDocument(BaseModel):
    file_id: str
    uploader_id: str
    assignment_id: Optional[str] = None
    filename: str
    original_name: str
    content_type: str
    size: int
    url: str
    created_at: str

class Payment(BaseModel):
    payment_id: str
    assignment_id: str
    payer_id: str
    teacher_id: str
    amount: float
    currency: str = "SAR"
    status: Literal["pending", "paid", "refunded"] = "pending"
    method: Optional[str] = None
    notes: Optional[str] = None
    created_at: str
    paid_at: Optional[str] = None

class PaymentCreate(BaseModel):
    assignment_id: str
    amount: float
    method: Optional[str] = None
    notes: Optional[str] = None

class ChatMessage(BaseModel):
    message_id: str
    assignment_id: str
    sender_id: str
    sender_name: Optional[str] = None
    content: str
    created_at: str

class AuditLog(BaseModel):
    log_id: str
    user_id: str
    action: str
    entity_type: str
    entity_id: str
    timestamp: str

class DashboardStats(BaseModel):
    total_requests: int
    completed_sessions: int
    active_teachers: int
    active_students: int
    pending_requests: int

# ============= AUTH HELPERS =============

async def get_current_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> User:
    """Get current user from cookie or Authorization header."""
    session_token = request.cookies.get("session_token")
    
    if not session_token and credentials:
        session_token = credentials.credentials
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    sessions_ref = db.collection('user_sessions')
    query = sessions_ref.where('session_token', '==', session_token).limit(1)
    session_docs = query.stream()
    session_doc = None
    for doc in session_docs:
        session_doc = doc.to_dict()
        break
    
    if not session_doc:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    expires_at = session_doc["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    user_ref = db.collection('users').document(session_doc["user_id"])
    user_snap = user_ref.get()
    if not user_snap.exists:
        raise HTTPException(status_code=401, detail="User not found")
    
    return User(**user_snap.to_dict())

async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

async def log_audit(user_id: str, action: str, entity_type: str, entity_id: str):
    """Log an audit entry."""
    log_id = f"log_{uuid.uuid4().hex[:12]}"
    log = {
        "log_id": log_id,
        "user_id": user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    db.collection('audit_logs').document(log_id).set(log)

async def create_notification(user_id: str, notif_type: str, title: str, message: str, link: str = None):
    """Create an in-app notification for a user."""
    notif_id = f"notif_{uuid.uuid4().hex[:12]}"
    notif = {
        "notification_id": notif_id,
        "user_id": user_id,
        "type": notif_type,
        "title": title,
        "message": message,
        "link": link,
        "is_read": False,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection('notifications').document(notif_id).set(notif)

async def send_email_notification(recipient_email: str, subject: str, html_content: str):
    """Send email notification (non-blocking)."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured, skipping email")
        return
    
    params = {
        "from": SENDER_EMAIL,
        "to": [recipient_email],
        "subject": subject,
        "html": html_content
    }
    
    try:
        await asyncio.to_thread(resend.Emails.send, params)
        logger.info(f"Email sent to {recipient_email}")
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")

# ============= AUTH ROUTES =============

@api_router.post("/auth/register")
async def register(request: Request, user_data: UserCreate):
    """Register a new user with email/password."""
    client_ip = request.client.host if request.client else "unknown"
    if not auth_rate_limiter.is_allowed(f"register:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    # Password strength validation
    password = user_data.password
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not any(c.isupper() for c in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one uppercase letter")
    if not any(c.islower() for c in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one lowercase letter")
    if not any(c.isdigit() for c in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one number")

    # Check if email already exists
    users_ref = db.collection('users')
    query = users_ref.where('email', '==', user_data.email).limit(1)
    existing = list(query.stream())
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = bcrypt.hashpw(user_data.password.encode(), bcrypt.gensalt())
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    
    user_doc = {
        "user_id": user_id,
        "email": user_data.email,
        "name": user_data.name,
        "phone": user_data.phone,
        "city": user_data.city,
        "picture": None,
        "role": user_data.role,
        "password_hash": hashed_password.decode(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    db.collection('users').document(user_id).set(user_doc)
    await log_audit(user_id, "register", "user", user_id)
    
    response_doc = {k: v for k, v in user_doc.items() if k != "password_hash"}
    return {"message": "User registered successfully", "user": response_doc}

@api_router.post("/auth/login")
async def login(request: Request, response: Response, login_data: LoginRequest):
    """Login with email/password."""
    client_ip = request.client.host if request.client else "unknown"
    if not auth_rate_limiter.is_allowed(f"login:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    users_ref = db.collection('users')
    query = users_ref.where('email', '==', login_data.email).limit(1)
    user_docs = list(query.stream())
    
    if not user_docs:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    user_doc = user_docs[0].to_dict()
    
    if not bcrypt.checkpw(login_data.password.encode(), user_doc["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    session_token = f"session_{uuid.uuid4().hex}"
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    session_doc = {
        "user_id": user_doc["user_id"],
        "session_token": session_token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    db.collection('user_sessions').document(session_id).set(session_doc)
    
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7 * 24 * 60 * 60
    )
    
    response_doc = {k: v for k, v in user_doc.items() if k != "password_hash"}
    
    return {"message": "Login successful", "user": response_doc}

@api_router.post("/auth/session")
async def exchange_session(response: Response, request: SessionExchangeRequest):
    """Exchange Google OAuth session_id for user data."""
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": request.session_id}
            )
            resp.raise_for_status()
            oauth_data = resp.json()
    except Exception as e:
        logger.error(f"Failed to exchange session: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid session_id")
    
    # Check if user exists
    users_ref = db.collection('users')
    query = users_ref.where('email', '==', oauth_data["email"]).limit(1)
    existing = list(query.stream())
    
    if existing:
        user_doc = existing[0].to_dict()
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user_doc = {
            "user_id": user_id,
            "email": oauth_data["email"],
            "name": oauth_data["name"],
            "picture": oauth_data.get("picture"),
            "role": "student",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        db.collection('users').document(user_id).set(user_doc)
        await log_audit(user_id, "register_oauth", "user", user_id)
    
    session_token = oauth_data["session_token"]
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    session_doc = {
        "user_id": user_doc["user_id"],
        "session_token": session_token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    db.collection('user_sessions').document(session_id).set(session_doc)
    
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7 * 24 * 60 * 60
    )
    
    return {"message": "Session created", "user": user_doc}

@api_router.post("/auth/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    """Send password reset code to email."""
    # Find user by email
    users_ref = db.collection('users')
    query = users_ref.where('email', '==', data.email).limit(1)
    user_docs = list(query.stream())
    
    if not user_docs:
        # Don't reveal if email exists or not for security
        return {"message": "If the email exists, a reset code has been sent"}
    
    user_doc = user_docs[0].to_dict()
    
    # Generate 6-digit code
    import random
    code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    
    # Store reset code with 15 min expiry
    reset_id = f"reset_{uuid.uuid4().hex[:12]}"
    reset_doc = {
        "reset_id": reset_id,
        "user_id": user_doc["user_id"],
        "email": data.email,
        "code": code,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "used": False,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection('password_resets').document(reset_id).set(reset_doc)
    
    # Send email with code
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #2C5F2D;">إعادة تعيين كلمة المرور - Password Reset</h2>
        <p>مرحباً {user_doc.get('name', '')},</p>
        <p>رمز إعادة تعيين كلمة المرور الخاص بك هو:</p>
        <p>Your password reset code is:</p>
        <div style="background: #f5f5f5; padding: 20px; text-align: center; font-size: 32px; letter-spacing: 8px; font-weight: bold; margin: 20px 0;">
            {code}
        </div>
        <p>هذا الرمز صالح لمدة 15 دقيقة.</p>
        <p>This code is valid for 15 minutes.</p>
        <p style="color: #666; font-size: 12px;">إذا لم تطلب هذا الرمز، يرجى تجاهل هذه الرسالة.</p>
        <p style="color: #666; font-size: 12px;">If you didn't request this, please ignore this email.</p>
    </div>
    """
    
    await send_email_notification(
        data.email,
        "بديل - رمز إعادة تعيين كلمة المرور | Badeel - Password Reset Code",
        html_content
    )
    
    await log_audit(user_doc["user_id"], "forgot_password", "auth", reset_id)
    
    return {"message": "If the email exists, a reset code has been sent"}

@api_router.post("/auth/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """Reset password using code."""
    # Find valid reset code
    resets_ref = db.collection('password_resets')
    query = resets_ref.where('email', '==', data.email).where('code', '==', data.code).where('used', '==', False).limit(1)
    reset_docs = list(query.stream())
    
    if not reset_docs:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    
    reset_doc = reset_docs[0].to_dict()
    
    # Check expiry
    expires_at = datetime.fromisoformat(reset_doc["expires_at"].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Code has expired")
    
    # Update password
    password_hash = bcrypt.hashpw(data.new_password.encode(), bcrypt.gensalt()).decode()
    user_ref = db.collection('users').document(reset_doc["user_id"])
    user_ref.update({"password_hash": password_hash})
    
    # Mark reset code as used
    reset_docs[0].reference.update({"used": True})
    
    await log_audit(reset_doc["user_id"], "reset_password", "auth", reset_doc["reset_id"])
    
    return {"message": "Password reset successfully"}

@api_router.get("/auth/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user info with profile completion status."""
    user_dict = current_user.model_dump()

    # Check if profile exists for student/teacher/guardian
    if current_user.role == "student":
        query = db.collection('student_profiles').where('user_id', '==', current_user.user_id).limit(1)
        user_dict["profile_completed"] = len(list(query.stream())) > 0
    elif current_user.role == "guardian":
        query = db.collection('guardian_profiles').where('user_id', '==', current_user.user_id).limit(1)
        user_dict["profile_completed"] = len(list(query.stream())) > 0
    elif current_user.role == "teacher":
        query = db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1)
        user_dict["profile_completed"] = len(list(query.stream())) > 0
    else:
        user_dict["profile_completed"] = True

    return user_dict

@api_router.post("/auth/logout")
async def logout(response: Response, current_user: User = Depends(get_current_user), request: Request = None):
    """Logout and clear session."""
    session_token = request.cookies.get("session_token")
    if session_token:
        sessions_ref = db.collection('user_sessions')
        query = sessions_ref.where('session_token', '==', session_token).limit(1)
        for doc in query.stream():
            doc.reference.delete()
    
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out successfully"}

# ============= USER ROUTES =============

class AdminCreateUser(BaseModel):
    name: str
    email: EmailStr
    role: Literal["admin", "school_admin", "teacher", "student", "guardian"]
    phone: Optional[str] = None
    city: Optional[str] = None
    password: Optional[str] = None

class AdminUpdateUser(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Literal["admin", "school_admin", "teacher", "student", "guardian"]] = None
    phone: Optional[str] = None
    city: Optional[str] = None

@api_router.get("/users", response_model=List[User])
async def list_users(current_user: User = Depends(get_admin_user)):
    """List all users (admin only)."""
    users = []
    for doc in db.collection('users').stream():
        d = doc.to_dict()
        d.pop("password_hash", None)
        users.append(d)
    return users

@api_router.post("/users")
async def create_user(data: AdminCreateUser, current_user: User = Depends(get_admin_user)):
    """Admin creates a new user."""
    # Check email uniqueness
    existing = list(db.collection('users').where('email', '==', data.email).limit(1).stream())
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")
    
    user_id = str(uuid.uuid4())
    password = data.password or "Badeel@2026"
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    user_doc = {
        "user_id": user_id,
        "email": data.email,
        "name": data.name,
        "role": data.role,
        "phone": data.phone or "",
        "city": data.city or "",
        "picture": "",
        "linked_student_id": None,
        "password_hash": password_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db.collection('users').document(user_id).set(user_doc)
    await log_audit(current_user.user_id, "create_user", "user", user_id)
    
    result = {k: v for k, v in user_doc.items() if k != "password_hash"}
    return result

@api_router.put("/users/{user_id}")
async def update_user(user_id: str, data: AdminUpdateUser, current_user: User = Depends(get_admin_user)):
    """Admin updates user fields (name, email, role, phone, city)."""
    user_ref = db.collection('users').document(user_id)
    if not user_ref.get().exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    # If changing email, check uniqueness
    if "email" in updates:
        existing = list(db.collection('users').where('email', '==', updates['email']).limit(1).stream())
        if existing and existing[0].to_dict().get('user_id') != user_id:
            raise HTTPException(status_code=400, detail="Email already exists")
    
    user_ref.update(updates)
    await log_audit(current_user.user_id, "update_user", "user", user_id)
    return {"message": "User updated successfully"}

@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, current_user: User = Depends(get_admin_user)):
    """Admin deletes a user. Cannot delete self."""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    
    user_ref = db.collection('users').document(user_id)
    if not user_ref.get().exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_ref.delete()
    await log_audit(current_user.user_id, "delete_user", "user", user_id)
    return {"message": "User deleted successfully"}

# ============= TEACHER PROFILE ROUTES =============

@api_router.post("/teachers/profile", response_model=TeacherProfile)
async def create_teacher_profile(profile_data: TeacherProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update teacher profile."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can create teacher profiles")
    
    # Check for existing profile
    query = db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1)
    existing = list(query.stream())
    
    if existing:
        existing_doc = existing[0].to_dict()
        existing[0].reference.update(profile_data.model_dump())
        existing_doc.update(profile_data.model_dump())
        result = TeacherProfile(**existing_doc)
    else:
        teacher_id = f"teacher_{uuid.uuid4().hex[:12]}"
        profile_doc = {
            "teacher_id": teacher_id,
            "user_id": current_user.user_id,
            **profile_data.model_dump(),
            "verification_status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        db.collection('teacher_profiles').document(teacher_id).set(profile_doc)
        await log_audit(current_user.user_id, "create_teacher_profile", "teacher_profile", teacher_id)
        result = TeacherProfile(**profile_doc)

    # Sync substitute_profiles if substitute fields present
    if profile_data.education_stages:
        avail_mode = "both"
        if profile_data.remote_enabled and not profile_data.in_person_enabled:
            avail_mode = "remote"
        elif profile_data.in_person_enabled and not profile_data.remote_enabled:
            avail_mode = "in_person"

        sub_data = {
            "specializations": [profile_data.specialization],
            "education_stages": profile_data.education_stages,
            "city": profile_data.cities[0] if profile_data.cities else None,
            "region": profile_data.region,
            "availability_mode": avail_mode,
            "availability_schedule": profile_data.availability_schedule,
            "bio": profile_data.bio,
            "certificate_url": profile_data.certificate_url,
        }
        existing_sub = list(db.collection('substitute_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
        if existing_sub:
            existing_sub[0].reference.update(sub_data)
        else:
            profile_id = f"subprof_{uuid.uuid4().hex[:12]}"
            sub_data.update({
                "profile_id": profile_id,
                "user_id": current_user.user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            db.collection('substitute_profiles').document(profile_id).set(sub_data)

    return result

@api_router.get("/teachers/profile", response_model=TeacherProfile)
async def get_my_teacher_profile(current_user: User = Depends(get_current_user)):
    """Get my teacher profile (merged with substitute fields)."""
    query = db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1)
    docs = list(query.stream())
    if not docs:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    data = docs[0].to_dict()

    # Merge substitute profile fields if they exist
    sub_docs = list(db.collection('substitute_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
    if sub_docs:
        sp = sub_docs[0].to_dict()
        if not data.get('education_stages'):
            data['education_stages'] = sp.get('education_stages')
        if not data.get('certificate_url'):
            data['certificate_url'] = sp.get('certificate_url')
        if not data.get('availability_schedule'):
            data['availability_schedule'] = sp.get('availability_schedule')
        if not data.get('region'):
            data['region'] = sp.get('region')

    return TeacherProfile(**data)

@api_router.get("/teachers")
async def list_teachers(
    specialization: Optional[str] = None,
    city: Optional[str] = None,
    grade: Optional[str] = None,
    remote: Optional[bool] = None,
    in_person: Optional[bool] = None,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 12,
    current_user: User = Depends(get_current_user)
):
    """List/search teachers with optional filters."""
    # Gate school_admin: require active+paid subscription
    if current_user.role == "school_admin":
        sub_docs = list(db.collection('school_subscriptions').where('admin_user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).stream())
        if not sub_docs or sub_docs[0].to_dict().get('payment_status') != 'paid':
            raise HTTPException(status_code=403, detail="Active paid subscription required to browse teachers")
    
    teachers = [doc.to_dict() for doc in db.collection('teacher_profiles').stream()]
    
    # Apply filters
    if specialization:
        teachers = [t for t in teachers if t.get('specialization', '').lower() == specialization.lower()]
    if city:
        teachers = [t for t in teachers if city.lower() in [c.lower() for c in t.get('cities', [])]]
    if grade:
        teachers = [t for t in teachers if grade.lower() in [g.lower() for g in t.get('grades', [])]]
    if remote is not None:
        teachers = [t for t in teachers if t.get('remote_enabled') == remote]
    if in_person is not None:
        teachers = [t for t in teachers if t.get('in_person_enabled') == in_person]
    
    # Enrich with user name
    for teacher in teachers:
        user_docs = list(db.collection('users').where('user_id', '==', teacher.get('user_id')).limit(1).stream())
        if user_docs:
            u = user_docs[0].to_dict()
            teacher['name'] = u.get('name', '')
            teacher['email'] = u.get('email', '')
    
    # Text search on name/specialization/bio
    if search:
        search_lower = search.lower()
        teachers = [t for t in teachers if (
            search_lower in t.get('name', '').lower() or
            search_lower in t.get('specialization', '').lower() or
            search_lower in t.get('bio', '').lower()
        )]
    
    total = len(teachers)
    start = (page - 1) * per_page
    end = start + per_page
    items = teachers[start:end]
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 1
    }

@api_router.get("/teachers/pending")
async def list_pending_teachers(current_user: User = Depends(get_current_user)):
    """List teachers pending verification (admin only)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins")
    
    docs = db.collection('teacher_profiles').where('verification_status', '==', 'pending').stream()
    teachers = []
    for doc in docs:
        t = doc.to_dict()
        # Enrich with user name
        user_docs = list(db.collection('users').where('user_id', '==', t.get('user_id')).limit(1).stream())
        if user_docs:
            u = user_docs[0].to_dict()
            t['name'] = u.get('name', '')
            t['email'] = u.get('email', '')
        teachers.append(t)
    return teachers

@api_router.get("/teachers/{teacher_id}")
async def get_teacher(teacher_id: str, current_user: User = Depends(get_current_user)):
    """Get a single teacher profile with user info and reviews."""
    doc = db.collection('teacher_profiles').document(teacher_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Teacher not found")
    teacher = doc.to_dict()
    # Enrich with user name/email
    user_docs = list(db.collection('users').where('user_id', '==', teacher.get('user_id')).limit(1).stream())
    if user_docs:
        u = user_docs[0].to_dict()
        teacher['name'] = u.get('name', '')
        teacher['email'] = u.get('email', '')
    # Attach reviews
    review_docs = list(db.collection('reviews').where('teacher_id', '==', teacher_id).stream())
    reviews = [r.to_dict() for r in review_docs]
    teacher['reviews'] = sorted(reviews, key=lambda r: r.get('created_at', ''), reverse=True)
    teacher['average_rating'] = round(sum(r.get('rating', 0) for r in reviews) / len(reviews), 1) if reviews else None
    return teacher

@api_router.put("/teachers/{teacher_id}/verify")
async def verify_teacher(teacher_id: str, status: str, current_user: User = Depends(get_current_user)):
    """Admin verifies or rejects a teacher profile."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can verify teachers")
    if status not in ["verified", "rejected"]:
        raise HTTPException(status_code=400, detail="Status must be 'verified' or 'rejected'")
    
    ref = db.collection('teacher_profiles').document(teacher_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    
    ref.update({"verification_status": status})
    
    # Notify the teacher
    teacher = snap.to_dict()
    title = "تم التحقق من حسابك" if status == "verified" else "تم رفض التحقق"
    message = "تم التحقق من ملفك التعليمي بنجاح" if status == "verified" else "تم رفض التحقق من ملفك التعليمي"
    await create_notification(teacher.get("user_id"), "verification_update", title, message)
    
    await log_audit(current_user.user_id, f"verify_teacher_{status}", "teacher_profile", teacher_id)
    return {"message": f"Teacher {status}"}

# ============= STUDENT PROFILE ROUTES =============

@api_router.post("/students/profile", response_model=StudentProfile)
async def create_student_profile(profile_data: StudentProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update student profile."""
    if current_user.role not in ["student", "guardian"]:
        raise HTTPException(status_code=403, detail="Only students/guardians can create student profiles")
    
    query = db.collection('student_profiles').where('user_id', '==', current_user.user_id).limit(1)
    existing = list(query.stream())
    
    if existing:
        existing_doc = existing[0].to_dict()
        existing[0].reference.update(profile_data.model_dump())
        existing_doc.update(profile_data.model_dump())
        return StudentProfile(**existing_doc)
    else:
        student_id = f"student_{uuid.uuid4().hex[:12]}"
        profile_doc = {
            "student_id": student_id,
            "user_id": current_user.user_id,
            **profile_data.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        db.collection('student_profiles').document(student_id).set(profile_doc)
        await log_audit(current_user.user_id, "create_student_profile", "student_profile", student_id)
        return StudentProfile(**profile_doc)

@api_router.get("/students/profile", response_model=StudentProfile)
async def get_my_student_profile(current_user: User = Depends(get_current_user)):
    """Get my student profile."""
    query = db.collection('student_profiles').where('user_id', '==', current_user.user_id).limit(1)
    docs = list(query.stream())
    if not docs:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return StudentProfile(**docs[0].to_dict())

# ============= GUARDIAN ROUTES =============

class GuardianLinkRequest(BaseModel):
    student_email: EmailStr

class GuardianProfileCreate(BaseModel):
    phone: Optional[str] = None
    relationship: str  # "father", "mother", "other"
    notes: Optional[str] = None

@api_router.post("/guardians/profile")
async def create_guardian_profile(profile_data: GuardianProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update guardian profile."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians can create guardian profiles")
    
    query = db.collection('guardian_profiles').where('user_id', '==', current_user.user_id).limit(1)
    existing = list(query.stream())
    
    if existing:
        existing[0].reference.update(profile_data.model_dump())
        doc = existing[0].to_dict()
        doc.update(profile_data.model_dump())
        return doc
    else:
        profile_id = f"guardian_{uuid.uuid4().hex[:12]}"
        doc = {
            "profile_id": profile_id,
            "user_id": current_user.user_id,
            **profile_data.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        db.collection('guardian_profiles').document(profile_id).set(doc)
        return doc

@api_router.get("/guardians/profile")
async def get_guardian_profile(current_user: User = Depends(get_current_user)):
    """Get guardian profile."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians")
    query = db.collection('guardian_profiles').where('user_id', '==', current_user.user_id).limit(1)
    docs = list(query.stream())
    if not docs:
        raise HTTPException(status_code=404, detail="Guardian profile not found")
    return docs[0].to_dict()

@api_router.post("/guardians/link")
async def link_guardian_to_student(link_data: GuardianLinkRequest, current_user: User = Depends(get_current_user)):
    """Send a link request to a student (requires student approval)."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians can link to students")
    
    # Find student by email
    student_query = db.collection('users').where('email', '==', link_data.student_email).where('role', '==', 'student').limit(1)
    student_docs = list(student_query.stream())
    
    if not student_docs:
        raise HTTPException(status_code=404, detail="Student not found with this email")
    
    student_data = student_docs[0].to_dict()
    student_id = student_data['user_id']
    
    # Check if already linked
    user_doc = db.collection('users').document(current_user.user_id).get()
    if user_doc.exists and user_doc.to_dict().get('linked_student_id') == student_id:
        raise HTTPException(status_code=400, detail="Already linked to this student")
    
    # Check for existing pending request
    existing = db.collection('guardian_link_requests').where('guardian_id', '==', current_user.user_id).where('student_id', '==', student_id).where('status', '==', 'pending').limit(1)
    if list(existing.stream()):
        raise HTTPException(status_code=400, detail="Link request already pending")
    
    link_id = f"link_{uuid.uuid4().hex[:12]}"
    link_doc = {
        "link_id": link_id,
        "guardian_id": current_user.user_id,
        "guardian_name": current_user.name or current_user.email,
        "guardian_email": current_user.email,
        "student_id": student_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection('guardian_link_requests').document(link_id).set(link_doc)
    
    # Notify the student
    await create_notification(
        student_id, "guardian_link_request",
        "طلب ربط ولي أمر",
        f"{current_user.name or current_user.email} يريد الربط بحسابك كولي أمر",
        "/student"
    )
    
    await log_audit(current_user.user_id, "request_link_student", "user", student_id)
    return {"message": "Link request sent, waiting for student approval", "status": "pending"}

@api_router.get("/guardians/link/status")
async def get_guardian_link_status(current_user: User = Depends(get_current_user)):
    """Get the guardian's current link request status."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians")
    
    # Check if already linked
    user_doc = db.collection('users').document(current_user.user_id).get()
    if user_doc.exists:
        linked_id = user_doc.to_dict().get('linked_student_id')
        if linked_id:
            student_doc = db.collection('users').document(linked_id).get()
            if student_doc.exists:
                s = student_doc.to_dict()
                return {"status": "approved", "student_id": linked_id, "student_name": s.get('name', ''), "student_email": s.get('email', '')}
    
    # Check for pending request
    pending = db.collection('guardian_link_requests').where('guardian_id', '==', current_user.user_id).where('status', '==', 'pending').limit(1)
    docs = list(pending.stream())
    if docs:
        d = docs[0].to_dict()
        return {"status": "pending", "student_id": d.get('student_id')}
    
    return {"status": "none"}

@api_router.get("/students/guardian-requests")
async def get_pending_guardian_requests(current_user: User = Depends(get_current_user)):
    """Get pending guardian link requests for a student."""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students")
    
    docs = db.collection('guardian_link_requests').where('student_id', '==', current_user.user_id).where('status', '==', 'pending').stream()
    return [d.to_dict() for d in docs]

@api_router.post("/students/guardian-requests/{link_id}/respond")
async def respond_to_guardian_request(link_id: str, body: dict, current_user: User = Depends(get_current_user)):
    """Student approves or rejects a guardian link request."""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students")
    
    action = body.get("action")  # "approve" or "reject"
    if action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
    
    doc_ref = db.collection('guardian_link_requests').document(link_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Link request not found")
    
    link_data = doc.to_dict()
    if link_data['student_id'] != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not your request")
    if link_data['status'] != 'pending':
        raise HTTPException(status_code=400, detail="Request already processed")
    
    if action == "approve":
        doc_ref.update({"status": "approved"})
        # Link the guardian to the student
        guardian_ref = db.collection('users').document(link_data['guardian_id'])
        guardian_ref.update({'linked_student_id': current_user.user_id})
        
        await create_notification(
            link_data['guardian_id'], "guardian_link_approved",
            "تم قبول طلب الربط",
            f"{current_user.name or current_user.email} وافق على ربط حسابك",
            "/profile"
        )
        await log_audit(current_user.user_id, "approve_guardian_link", "guardian_link", link_id)
        return {"message": "Guardian linked successfully"}
    else:
        doc_ref.update({"status": "rejected"})
        await create_notification(
            link_data['guardian_id'], "guardian_link_rejected",
            "تم رفض طلب الربط",
            f"تم رفض طلب ربط حسابك بالطالب",
            "/profile"
        )
        await log_audit(current_user.user_id, "reject_guardian_link", "guardian_link", link_id)
        return {"message": "Link request rejected"}

@api_router.get("/guardians/child/activity")
async def get_child_activity(current_user: User = Depends(get_current_user)):
    """Get the linked child's requests, assignments, and profile for guardian monitoring."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians can access this")

    user_doc = db.collection('users').document(current_user.user_id).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = user_doc.to_dict()
    linked_student_id = user_data.get('linked_student_id')
    if not linked_student_id:
        return {"linked": False}

    # Fetch student user doc
    student_doc = db.collection('users').document(linked_student_id).get()
    if not student_doc.exists:
        return {"linked": False}
    student_data = student_doc.to_dict()

    # Fetch student profile
    profile_data = {}
    profile_q = db.collection('student_profiles').where('user_id', '==', linked_student_id).limit(1).get()
    if profile_q:
        profile_data = profile_q[0].to_dict()

    # Fetch child's requests
    requests_q = db.collection('teaching_requests').where('student_id', '==', linked_student_id).get()
    child_requests = []
    for doc in requests_q:
        r = doc.to_dict()
        r['request_id'] = doc.id
        child_requests.append(r)
    child_requests.sort(key=lambda x: x.get('created_at', ''), reverse=True)

    # Fetch child's assignments
    assignments_q = db.collection('assignments').where('student_id', '==', linked_student_id).get()
    child_assignments = []
    for doc in assignments_q:
        a = doc.to_dict()
        a['assignment_id'] = doc.id
        child_assignments.append(a)
    child_assignments.sort(key=lambda x: x.get('created_at', ''), reverse=True)

    return {
        "linked": True,
        "student": {
            "student_id": linked_student_id,
            "name": student_data.get('name', ''),
            "email": student_data.get('email', ''),
            "grade": profile_data.get('grade', ''),
            "age": profile_data.get('age', ''),
            "subjects": profile_data.get('preferred_subjects', []),
        },
        "requests": child_requests,
        "assignments": child_assignments,
    }

@api_router.get("/guardians/student")
async def get_linked_student(current_user: User = Depends(get_current_user)):
    """Get the linked student for a guardian."""
    if current_user.role != "guardian":
        raise HTTPException(status_code=403, detail="Only guardians can access this")
    
    # Fetch fresh user data to get linked_student_id
    user_doc = db.collection('users').document(current_user.user_id).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_data = user_doc.to_dict()
    linked_student_id = user_data.get('linked_student_id')
    
    if not linked_student_id:
        raise HTTPException(status_code=404, detail="No linked student found")
    
    # Fetch student details
    student_doc = db.collection('users').document(linked_student_id).get()
    if not student_doc.exists:
        raise HTTPException(status_code=404, detail="Linked student not found")
    
    student_data = student_doc.to_dict()
    return {
        "student_id": linked_student_id,
        "name": student_data.get('name', ''),
        "email": student_data.get('email', ''),
        "phone": student_data.get('phone', ''),
        "city": student_data.get('city', '')
    }

# ============= SCHOOL ROUTES =============

@api_router.post("/schools", response_model=School)
async def create_school(school_data: SchoolCreate, current_user: User = Depends(get_current_user)):
    """Create a school (school_admin only)."""
    if current_user.role != "school_admin":
        raise HTTPException(status_code=403, detail="Only school admins can create schools")
    
    school_id = f"school_{uuid.uuid4().hex[:12]}"
    school_doc = {
        "school_id": school_id,
        "admin_user_id": current_user.user_id,
        **school_data.model_dump(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    db.collection('schools').document(school_id).set(school_doc)
    await log_audit(current_user.user_id, "create_school", "school", school_id)
    return School(**school_doc)

@api_router.get("/schools", response_model=List[School])
async def list_schools(current_user: User = Depends(get_current_user)):
    """List schools."""
    schools = [doc.to_dict() for doc in db.collection('schools').stream()]
    return schools

# ============= TEACHING REQUEST ROUTES =============

@api_router.post("/requests", response_model=TeachingRequest)
async def create_request(request_data: TeachingRequestCreate, current_user: User = Depends(get_current_user)):
    """Create a teaching request."""
    # Gate school_admin: require active+paid subscription with available limit
    if current_user.role == "school_admin":
        sub_docs = list(db.collection('school_subscriptions').where('admin_user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).stream())
        if not sub_docs:
            raise HTTPException(status_code=403, detail="Active subscription required to create requests")
        sub = sub_docs[0].to_dict()
        if sub.get('payment_status') != 'paid':
            raise HTTPException(status_code=403, detail="Subscription payment required")
        if sub.get('request_limit', -1) != -1 and sub.get('requests_used', 0) >= sub['request_limit']:
            raise HTTPException(status_code=403, detail="Request limit reached for your plan")
    
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_doc = {
        "request_id": request_id,
        "created_by_user_id": current_user.user_id,
        **request_data.model_dump(),
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    # Auto-fill school info for substitute requests from school admins
    if request_data.service_type == "substitute" and current_user.role == "school_admin":
        school_docs = list(db.collection('schools').where('admin_user_id', '==', current_user.user_id).limit(1).get())
        if school_docs:
            school = school_docs[0].to_dict()
            request_doc["school_name"] = school.get("name", "")
            request_doc["school_type"] = school.get("school_type")
            request_doc["education_stage"] = school.get("education_stage")
            if school.get("city"):
                request_doc["city"] = school["city"]

    db.collection('teaching_requests').document(request_id).set(request_doc)
    
    # Increment requests_used for school_admin
    if current_user.role == "school_admin":
        sub_docs = list(db.collection('school_subscriptions').where('admin_user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).stream())
        if sub_docs:
            sub_ref = sub_docs[0].reference
            sub_ref.update({"requests_used": firestore.Increment(1)})
    
    await log_audit(current_user.user_id, "create_request", "teaching_request", request_id)
    return TeachingRequest(**request_doc)

@api_router.get("/requests")
async def list_requests(page: int = 1, per_page: int = 12, service_type: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """List teaching requests (filtered by role) with pagination."""
    ref = db.collection('teaching_requests')
    
    if current_user.role == "teacher":
        docs = list(ref.where('status', 'in', ['new', 'offered']).stream())
    elif current_user.role in ["student", "guardian"]:
        docs = list(ref.where('created_by_user_id', '==', current_user.user_id).stream())
    elif current_user.role == "school_admin":
        docs = list(ref.where('created_by_user_id', '==', current_user.user_id).stream())
    elif current_user.role == "admin":
        docs = list(ref.stream())
    else:
        docs = []
    
    all_items = [doc.to_dict() for doc in docs]
    if service_type:
        all_items = [item for item in all_items if item.get("service_type") == service_type]
    total = len(all_items)
    start = (page - 1) * per_page
    end = start + per_page
    items = all_items[start:end]
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0
    }

@api_router.get("/requests/{request_id}", response_model=TeachingRequest)
async def get_request(request_id: str, current_user: User = Depends(get_current_user)):
    """Get a specific teaching request."""
    doc = db.collection('teaching_requests').document(request_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Request not found")
    return TeachingRequest(**doc.to_dict())

VALID_REQUEST_TRANSITIONS = {
    "new": ["offered", "cancelled"],
    "offered": ["accepted", "cancelled"],
    "accepted": ["assigned", "cancelled"],
    "assigned": ["in_progress", "cancelled"],
    "in_progress": ["completed", "cancelled"],
    "completed": [],
    "cancelled": [],
}

VALID_ASSIGNMENT_TRANSITIONS = {
    "scheduled": ["in_progress", "cancelled"],
    "in_progress": ["completed", "cancelled"],
    "completed": [],
    "cancelled": [],
}

@api_router.put("/requests/{request_id}/status")
async def update_request_status(request_id: str, status: str, current_user: User = Depends(get_current_user)):
    """Update request status with valid transition enforcement."""
    valid_statuses = {"new", "offered", "accepted", "assigned", "in_progress", "completed", "cancelled"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    ref = db.collection('teaching_requests').document(request_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Request not found")

    current_status = snap.to_dict().get("status", "new")
    allowed = VALID_REQUEST_TRANSITIONS.get(current_status, [])
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot transition from '{current_status}' to '{status}'")

    ref.update({"status": status})
    await log_audit(current_user.user_id, f"update_request_status_{status}", "teaching_request", request_id)
    return {"message": "Request status updated"}

@api_router.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str, current_user: User = Depends(get_current_user)):
    """Cancel a request. Only the request owner can cancel, and only if status is new or offered."""
    ref = db.collection('teaching_requests').document(request_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Request not found")
    
    request_data = snap.to_dict()
    if request_data["created_by_user_id"] != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the request owner or admin can cancel")
    
    if request_data["status"] not in ["new", "offered"]:
        raise HTTPException(status_code=400, detail="Only new or offered requests can be cancelled")
    
    ref.update({"status": "cancelled"})
    await log_audit(current_user.user_id, "cancel_request", "teaching_request", request_id)
    return {"message": "Request cancelled successfully"}

# ============= OFFER ROUTES =============

@api_router.post("/requests/{request_id}/offer", response_model=RequestOffer)
async def create_offer(request_id: str, current_user: User = Depends(get_current_user)):
    """Teacher accepts a request."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can accept requests")
    
    req_ref = db.collection('teaching_requests').document(request_id)
    req_snap = req_ref.get()
    if not req_snap.exists:
        raise HTTPException(status_code=404, detail="Request not found")
    request_doc = req_snap.to_dict()
    
    if request_doc["status"] not in ["new", "offered"]:
        raise HTTPException(status_code=400, detail="Request is no longer available")
    
    # Get teacher profile
    t_query = db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1)
    t_docs = list(t_query.stream())
    if not t_docs:
        raise HTTPException(status_code=400, detail="Teacher profile not found")
    teacher_profile = t_docs[0].to_dict()
    
    # Check if teacher is verified by admin
    if teacher_profile.get("verification_status", "pending") != "verified":
        raise HTTPException(status_code=403, detail="Your profile is pending admin approval. Please wait for verification.")
    
    # If substitute request, require active subscription
    if request_doc.get("service_type") == "substitute":
        sub_q = db.collection('substitute_subscriptions').where('user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).get()
        if not list(sub_q):
            raise HTTPException(status_code=403, detail="Active subscription required to offer on substitute requests")
    
    # Check for existing offer
    o_query = db.collection('request_offers').where('request_id', '==', request_id).where('teacher_id', '==', teacher_profile["teacher_id"]).limit(1)
    if list(o_query.stream()):
        raise HTTPException(status_code=400, detail="You already made an offer for this request")
    
    offer_id = f"offer_{uuid.uuid4().hex[:12]}"
    offer_doc = {
        "offer_id": offer_id,
        "request_id": request_id,
        "teacher_id": teacher_profile["teacher_id"],
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    db.collection('request_offers').document(offer_id).set(offer_doc)
    req_ref.update({"status": "offered"})
    
    await log_audit(current_user.user_id, "create_offer", "teaching_request", request_id)
    
    # Notify the request owner about new offer
    await create_notification(
        request_doc.get("requester_id"),
        "new_offer",
        "عرض جديد",
        f"تقدم معلم بعرض لطلبك",
        f"/requests/{request_id}"
    )
    
    return RequestOffer(**offer_doc)

@api_router.post("/offers/{offer_id}/accept")
async def accept_offer(offer_id: str, current_user: User = Depends(get_current_user)):
    """Requester accepts a specific offer, creating an assignment and declining other offers."""
    offer_ref = db.collection('request_offers').document(offer_id)
    offer_snap = offer_ref.get()
    if not offer_snap.exists:
        raise HTTPException(status_code=404, detail="Offer not found")
    
    offer_data = offer_snap.to_dict()
    request_id = offer_data["request_id"]
    
    req_ref = db.collection('teaching_requests').document(request_id)
    req_snap = req_ref.get()
    if not req_snap.exists:
        raise HTTPException(status_code=404, detail="Request not found")
    request_doc = req_snap.to_dict()
    
    # Only the request owner or admin can accept an offer
    if request_doc["created_by_user_id"] != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the request owner can accept offers")
    
    if offer_data["status"] != "pending":
        raise HTTPException(status_code=400, detail="Offer is no longer pending")

    if request_doc["status"] not in ["offered"]:
        raise HTTPException(status_code=400, detail="Request is not in offered status")
    
    # Accept this offer
    offer_ref.update({"status": "accepted"})
    req_ref.update({"status": "accepted"})
    
    # Decline all other pending offers for this request
    other_offers = list(db.collection('request_offers').where('request_id', '==', request_id).stream())
    for o in other_offers:
        o_data = o.to_dict()
        if o_data["offer_id"] != offer_id and o_data["status"] == "pending":
            db.collection('request_offers').document(o_data["offer_id"]).update({"status": "declined"})
    
    # Create assignment
    # Look up teacher's user_id from their profile
    teacher_profile_doc = db.collection('teacher_profiles').document(offer_data["teacher_id"]).get()
    teacher_user_id = teacher_profile_doc.to_dict().get("user_id") if teacher_profile_doc.exists else offer_data["teacher_id"]
    assignment_id = f"assign_{uuid.uuid4().hex[:12]}"
    assignment_doc = {
        "assignment_id": assignment_id,
        "request_id": request_id,
        "teacher_id": teacher_user_id,
        "teacher_profile_id": offer_data["teacher_id"],
        "student_id": request_doc["created_by_user_id"],
        "meeting_link": None,
        "jitsi_room": f"badeel-{assignment_id}",
        "notes": None,
        "status": "scheduled",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None
    }
    db.collection('assignments').document(assignment_id).set(assignment_doc)
    
    # Send email notification
    user_ref = db.collection('users').document(request_doc["created_by_user_id"])
    requester_snap = user_ref.get()
    if requester_snap.exists and RESEND_API_KEY:
        requester = requester_snap.to_dict()
        html = f"<h2>تم قبول طلبك التعليمي</h2><p>تم تعيين معلم لطلبك. يرجى تسجيل الدخول لعرض التفاصيل.</p>"
        await send_email_notification(requester["email"], "تم قبول طلبك - منصة بديل", html)
    
    await log_audit(current_user.user_id, "accept_offer", "teaching_request", request_id)
    
    # Notify the accepted teacher
    # Find teacher user_id from teacher_profiles
    tp_query = db.collection('teacher_profiles').where('teacher_id', '==', offer.get('teacher_id')).limit(1)
    tp_docs = list(tp_query.stream())
    if tp_docs:
        teacher_user_id = tp_docs[0].to_dict().get('user_id')
        await create_notification(
            teacher_user_id,
            "offer_accepted",
            "تم قبول عرضك",
            "تم قبول عرضك وإنشاء جلسة جديدة",
            f"/assignments/{assignment_id}"
        )
    
    return {"message": "Offer accepted, assignment created"}

@api_router.get("/requests/{request_id}/offers")
async def list_offers(request_id: str, current_user: User = Depends(get_current_user)):
    """List offers for a request with teacher info."""
    docs = list(db.collection('request_offers').where('request_id', '==', request_id).stream())
    enriched = []
    for doc in docs:
        offer = doc.to_dict()
        # Lookup teacher user name via teacher_profiles -> users
        teacher_name = "Unknown"
        teacher_specialization = ""
        tp_query = db.collection('teacher_profiles').where('teacher_id', '==', offer['teacher_id']).limit(1)
        tp_docs = list(tp_query.stream())
        if tp_docs:
            tp = tp_docs[0].to_dict()
            teacher_specialization = tp.get("specialization", "")
            user_snap = db.collection('users').document(tp['user_id']).get()
            if user_snap.exists:
                teacher_name = user_snap.to_dict().get("name", "Unknown")
        offer["teacher_name"] = teacher_name
        offer["teacher_specialization"] = teacher_specialization
        enriched.append(offer)
    return enriched

# ============= ASSIGNMENT ROUTES =============

def _resolve_assignment_names(assignment_data: dict) -> dict:
    """Resolve teacher_name and student_name from IDs."""
    # Resolve teacher name
    teacher_id = assignment_data.get("teacher_id")
    if teacher_id:
        t_docs = list(db.collection('teacher_profiles').where('teacher_id', '==', teacher_id).limit(1).stream())
        if t_docs:
            tp = t_docs[0].to_dict()
            user_snap = db.collection('users').document(tp.get("user_id", "")).get()
            if user_snap.exists:
                assignment_data["teacher_name"] = user_snap.to_dict().get("name", "")
    # Resolve student name
    student_id = assignment_data.get("student_id")
    if student_id:
        user_snap = db.collection('users').document(student_id).get()
        if user_snap.exists:
            assignment_data["student_name"] = user_snap.to_dict().get("name", "")
    return assignment_data

@api_router.get("/assignments")
async def list_assignments(page: int = 1, per_page: int = 12, current_user: User = Depends(get_current_user)):
    """List my assignments with pagination."""
    ref = db.collection('assignments')
    
    if current_user.role == "teacher":
        t_query = db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1)
        t_docs = list(t_query.stream())
        if not t_docs:
            return {"items": [], "total": 0, "page": 1, "per_page": per_page, "total_pages": 0}
        teacher_profile = t_docs[0].to_dict()
        docs = list(ref.where('teacher_id', '==', teacher_profile["teacher_id"]).stream())
    elif current_user.role in ["student", "guardian"]:
        docs = list(ref.where('student_id', '==', current_user.user_id).stream())
    elif current_user.role in ["admin", "school_admin"]:
        docs = list(ref.stream())
    else:
        docs = []
    
    all_items = [_resolve_assignment_names(doc.to_dict()) for doc in docs]
    total = len(all_items)
    start = (page - 1) * per_page
    end = start + per_page
    items = all_items[start:end]
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0
    }

@api_router.get("/assignments/{assignment_id}", response_model=Assignment)
async def get_assignment(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Get a specific assignment."""
    doc = db.collection('assignments').document(assignment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return Assignment(**_resolve_assignment_names(doc.to_dict()))

@api_router.put("/assignments/{assignment_id}", response_model=Assignment)
async def update_assignment(assignment_id: str, update_data: AssignmentUpdate, current_user: User = Depends(get_current_user)):
    """Update assignment (meeting link, notes, status) with valid transition enforcement."""
    update_dict = {k: v for k, v in update_data.model_dump().items() if v is not None}
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    ref = db.collection('assignments').document(assignment_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Enforce valid status transitions
    if "status" in update_dict:
        current_status = snap.to_dict().get("status", "scheduled")
        new_status = update_dict["status"]
        allowed = VALID_ASSIGNMENT_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            raise HTTPException(status_code=400, detail=f"Cannot transition from '{current_status}' to '{new_status}'")
    
    ref.update(update_dict)
    
    updated = ref.get().to_dict()
    await log_audit(current_user.user_id, "update_assignment", "assignment", assignment_id)
    return Assignment(**updated)

@api_router.put("/assignments/{assignment_id}/complete")
async def complete_assignment(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Mark assignment as completed."""
    ref = db.collection('assignments').document(assignment_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    assignment = snap.to_dict()
    
    current_status = assignment.get("status", "scheduled")
    if current_status not in ["scheduled", "in_progress"]:
        raise HTTPException(status_code=400, detail=f"Cannot complete assignment with status '{current_status}'")
    
    ref.update({
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat()
    })
    
    req_ref = db.collection('teaching_requests').document(assignment["request_id"])
    if req_ref.get().exists:
        req_ref.update({"status": "completed"})
    
    await log_audit(current_user.user_id, "complete_assignment", "assignment", assignment_id)
    
    # Notify the student that session is complete
    await create_notification(
        assignment.get("student_id"),
        "assignment_completed",
        "اكتملت الجلسة",
        "تم إكمال جلستك التعليمية. يمكنك تقييم المعلم الآن.",
        f"/assignments/{assignment_id}"
    )
    
    return {"message": "Assignment completed successfully"}

# ============= REVIEW ROUTES =============

@api_router.post("/assignments/{assignment_id}/review")
async def create_review(assignment_id: str, review_data: ReviewCreate, current_user: User = Depends(get_current_user)):
    """Create a review for a completed assignment."""
    # Get assignment
    assignment_doc = db.collection('assignments').document(assignment_id).get()
    if not assignment_doc.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment = assignment_doc.to_dict()
    
    if assignment.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Can only review completed assignments")
    
    # Only the student (or guardian) can review
    if assignment.get("student_id") != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the student can review")
    
    # Check for existing review
    existing = list(db.collection('reviews').where('assignment_id', '==', assignment_id).where('reviewer_id', '==', current_user.user_id).limit(1).stream())
    if existing:
        raise HTTPException(status_code=400, detail="You already reviewed this assignment")
    
    review_id = f"review_{uuid.uuid4().hex[:12]}"
    review_doc = {
        "review_id": review_id,
        "assignment_id": assignment_id,
        "reviewer_id": current_user.user_id,
        "teacher_id": assignment.get("teacher_id"),
        "rating": review_data.rating,
        "comment": review_data.comment,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection('reviews').document(review_id).set(review_doc)
    await log_audit(current_user.user_id, "create_review", "review", review_id)
    return review_doc

@api_router.get("/assignments/{assignment_id}/review")
async def get_assignment_review(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Get the review for a specific assignment."""
    reviews = list(db.collection('reviews').where('assignment_id', '==', assignment_id).limit(1).stream())
    if not reviews:
        return None
    return reviews[0].to_dict()

@api_router.get("/teachers/{teacher_id}/reviews")
async def get_teacher_reviews(teacher_id: str, current_user: User = Depends(get_current_user)):
    """Get all reviews for a teacher with average rating."""
    reviews = [doc.to_dict() for doc in db.collection('reviews').where('teacher_id', '==', teacher_id).stream()]
    
    # Enrich with reviewer names
    for review in reviews:
        user_docs = list(db.collection('users').where('user_id', '==', review.get('reviewer_id')).limit(1).stream())
        if user_docs:
            review['reviewer_name'] = user_docs[0].to_dict().get('name', '')
    
    avg_rating = sum(r.get('rating', 0) for r in reviews) / len(reviews) if reviews else 0
    
    return {
        "reviews": reviews,
        "average_rating": round(avg_rating, 1),
        "total_reviews": len(reviews)
    }

# ============= NOTIFICATION ROUTES =============

@api_router.get("/notifications")
async def get_notifications(current_user: User = Depends(get_current_user)):
    """Get notifications for current user."""
    try:
        docs = list(db.collection('notifications').where('user_id', '==', current_user.user_id).stream())
        notifications = [doc.to_dict() for doc in docs]
        # Sort in Python to avoid Firestore composite index requirement
        notifications.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return notifications[:50]
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}")
        return []

@api_router.put("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, current_user: User = Depends(get_current_user)):
    """Mark a notification as read."""
    ref = db.collection('notifications').document(notification_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif = snap.to_dict()
    if notif.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not your notification")
    ref.update({"is_read": True})
    return {"message": "Marked as read"}

@api_router.put("/notifications/read-all")
async def mark_all_notifications_read(current_user: User = Depends(get_current_user)):
    """Mark all notifications as read for current user."""
    docs = db.collection('notifications').where('user_id', '==', current_user.user_id).where('is_read', '==', False).stream()
    for doc in docs:
        doc.reference.update({"is_read": True})
    return {"message": "All notifications marked as read"}

# ============= FILE UPLOAD ROUTES =============

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".txt", ".pptx", ".xlsx"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

@api_router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    assignment_id: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user)
):
    """Upload a file (optionally linked to an assignment)."""
    # Validate extension
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type {ext} not allowed")
    
    # Read and validate size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    safe_filename = f"{file_id}{ext}"
    file_path = UPLOADS_DIR / safe_filename
    
    with open(file_path, "wb") as f:
        f.write(content)
    
    file_doc = {
        "file_id": file_id,
        "uploader_id": current_user.user_id,
        "assignment_id": assignment_id,
        "filename": safe_filename,
        "original_name": file.filename,
        "content_type": file.content_type or "application/octet-stream",
        "size": len(content),
        "url": f"/uploads/{safe_filename}",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db.collection('files').document(file_id).set(file_doc)
    await log_audit(current_user.user_id, "upload_file", "file", file_id)
    return file_doc

@api_router.get("/files")
async def list_files(assignment_id: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """List files, optionally filtered by assignment."""
    if assignment_id:
        docs = db.collection('files').where('assignment_id', '==', assignment_id).stream()
    else:
        docs = db.collection('files').where('uploader_id', '==', current_user.user_id).stream()
    return [doc.to_dict() for doc in docs]

@api_router.delete("/files/{file_id}")
async def delete_file(file_id: str, current_user: User = Depends(get_current_user)):
    """Delete a file."""
    ref = db.collection('files').document(file_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="File not found")
    file_doc = snap.to_dict()
    if file_doc.get("uploader_id") != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Delete from filesystem
    file_path = UPLOADS_DIR / file_doc["filename"]
    if file_path.exists():
        file_path.unlink()
    
    ref.delete()
    await log_audit(current_user.user_id, "delete_file", "file", file_id)
    return {"message": "File deleted"}

# ============= PAYMENT ROUTES =============

@api_router.post("/payments")
async def create_payment(payment_data: PaymentCreate, current_user: User = Depends(get_current_user)):
    """Create a payment record for an assignment."""
    # Verify assignment exists
    assignment_ref = db.collection('assignments').document(payment_data.assignment_id)
    assignment_snap = assignment_ref.get()
    if not assignment_snap.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment = assignment_snap.to_dict()
    
    # Only the student (payer) or admin can create payment
    if assignment.get("student_id") != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    payment_doc = {
        "payment_id": payment_id,
        "assignment_id": payment_data.assignment_id,
        "payer_id": current_user.user_id,
        "teacher_id": assignment.get("teacher_id"),
        "amount": payment_data.amount,
        "currency": "SAR",
        "status": "pending",
        "method": payment_data.method,
        "notes": payment_data.notes,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paid_at": None
    }
    db.collection('payments').document(payment_id).set(payment_doc)
    await log_audit(current_user.user_id, "create_payment", "payment", payment_id)
    return payment_doc

@api_router.get("/payments")
async def list_payments(current_user: User = Depends(get_current_user)):
    """List payments for current user."""
    if current_user.role == "admin":
        docs = db.collection('payments').stream()
    elif current_user.role == "teacher":
        # Get teacher_id
        tp = list(db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1).stream())
        if tp:
            teacher_id = tp[0].to_dict().get('teacher_id')
            docs = db.collection('payments').where('teacher_id', '==', teacher_id).stream()
        else:
            docs = []
    else:
        docs = db.collection('payments').where('payer_id', '==', current_user.user_id).stream()
    
    return [doc.to_dict() for doc in docs]

@api_router.put("/payments/{payment_id}/pay")
async def mark_payment_paid(payment_id: str, current_user: User = Depends(get_current_user)):
    """Mark a payment as paid."""
    ref = db.collection('payments').document(payment_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment = snap.to_dict()
    
    if payment.get("payer_id") != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    ref.update({
        "status": "paid",
        "paid_at": datetime.now(timezone.utc).isoformat()
    })
    
    # Notify teacher
    tp_query = db.collection('teacher_profiles').where('teacher_id', '==', payment.get('teacher_id')).limit(1)
    tp_docs = list(tp_query.stream())
    if tp_docs:
        teacher_user_id = tp_docs[0].to_dict().get('user_id')
        await create_notification(
            teacher_user_id,
            "payment_received",
            "تم استلام الدفع",
            f"تم دفع {payment.get('amount')} ريال",
            f"/payments"
        )
    
    await log_audit(current_user.user_id, "mark_payment_paid", "payment", payment_id)
    return {"message": "Payment marked as paid"}

@api_router.get("/payments/assignment/{assignment_id}")
async def get_assignment_payment(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Get payment for a specific assignment."""
    docs = list(db.collection('payments').where('assignment_id', '==', assignment_id).limit(1).stream())
    if docs:
        return docs[0].to_dict()
    return None

# ============= CHAT ROUTES =============

@api_router.post("/chat/{assignment_id}")
async def send_message(assignment_id: str, body: dict, current_user: User = Depends(get_current_user)):
    """Send a chat message for an assignment."""
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    # Verify user is part of this assignment
    doc = db.collection('assignments').document(assignment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment = doc.to_dict()
    if current_user.user_id not in [assignment.get('teacher_id'), assignment.get('student_id')]:
        if current_user.role != 'admin':
            raise HTTPException(status_code=403, detail="Not part of this assignment")
    msg_id = str(uuid.uuid4())
    msg = {
        "message_id": msg_id,
        "assignment_id": assignment_id,
        "sender_id": current_user.user_id,
        "sender_name": current_user.name or current_user.email,
        "content": content,
        "created_at": datetime.utcnow().isoformat(),
    }
    db.collection('chat_messages').document(msg_id).set(msg)
    # Notify the other party
    other_id = assignment.get('teacher_id') if current_user.user_id == assignment.get('student_id') else assignment.get('student_id')
    if other_id:
        await create_notification(other_id, "new_message", f"رسالة جديدة من {msg['sender_name']}", content[:100], f"/assignments/{assignment_id}")
    return msg

@api_router.get("/chat/{assignment_id}")
async def get_messages(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Get chat messages for an assignment."""
    doc = db.collection('assignments').document(assignment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment = doc.to_dict()
    if current_user.user_id not in [assignment.get('teacher_id'), assignment.get('student_id')]:
        if current_user.role != 'admin':
            raise HTTPException(status_code=403, detail="Not part of this assignment")
    try:
        docs = db.collection('chat_messages').where('assignment_id', '==', assignment_id).order_by('created_at').stream()
        return [d.to_dict() for d in docs]
    except Exception:
        # Fallback if composite index not available — sort in Python
        docs = db.collection('chat_messages').where('assignment_id', '==', assignment_id).stream()
        messages = [d.to_dict() for d in docs]
        messages.sort(key=lambda m: m.get('created_at', ''))
        return messages

# ============= SUBSTITUTE TEACHER MODULE =============

@api_router.post("/substitute/subscribe")
async def subscribe_substitute(current_user: User = Depends(get_current_user)):
    """Teacher requests a substitute subscription. Auto-approved if teacher is verified."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can subscribe")

    # Check teacher verification status
    teacher_docs = list(db.collection('teacher_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
    is_verified = False
    if teacher_docs:
        is_verified = teacher_docs[0].to_dict().get('verification_status') == 'verified'

    # Check for existing subscription
    existing = list(db.collection('substitute_subscriptions').where('user_id', '==', current_user.user_id).limit(1).get())
    if existing:
        data = existing[0].to_dict()
        if data.get('status') in ['pending', 'active']:
            return {"message": "Subscription already exists", "status": data['status'], "subscription_id": data['subscription_id']}
        # If inactive/expired, allow re-request
        if is_verified:
            now = datetime.now(timezone.utc).isoformat()
            existing[0].reference.update({
                "status": "active",
                "requested_at": now,
                "approved_at": now,
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            })
            return {"message": "Subscription auto-approved", "status": "active", "subscription_id": data['subscription_id']}
        else:
            existing[0].reference.update({"status": "pending", "requested_at": datetime.now(timezone.utc).isoformat()})
            return {"message": "Subscription request renewed", "status": "pending", "subscription_id": data['subscription_id']}

    now = datetime.now(timezone.utc).isoformat()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"

    if is_verified:
        # Auto-approve for verified teachers
        sub_doc = {
            "subscription_id": sub_id,
            "user_id": current_user.user_id,
            "status": "active",
            "requested_at": now,
            "approved_at": now,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        }
        db.collection('substitute_subscriptions').document(sub_id).set(sub_doc)
        await log_audit(current_user.user_id, "auto_approve_subscription", "substitute_subscription", sub_id)
        return {"message": "Subscription auto-approved", "status": "active", "subscription_id": sub_id}
    else:
        # Pending approval for unverified teachers
        sub_doc = {
            "subscription_id": sub_id,
            "user_id": current_user.user_id,
            "status": "pending",
            "requested_at": now,
            "approved_at": None,
            "expires_at": None,
        }
        db.collection('substitute_subscriptions').document(sub_id).set(sub_doc)
        await log_audit(current_user.user_id, "request_subscription", "substitute_subscription", sub_id)

        # Notify admins only for pending
        admin_docs = db.collection('users').where('role', '==', 'admin').get()
        for admin_doc in admin_docs:
            await create_notification(
                admin_doc.id,
                "subscription_request",
                "طلب اشتراك جديد",
                f"المعلم {current_user.name} يطلب الاشتراك في نظام المعلم البديل",
                "/admin"
            )

        return {"message": "Subscription requested", "status": "pending", "subscription_id": sub_id}

@api_router.get("/substitute/subscription")
async def get_my_subscription(current_user: User = Depends(get_current_user)):
    """Get current teacher's substitute subscription status."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can check subscription")

    docs = list(db.collection('substitute_subscriptions').where('user_id', '==', current_user.user_id).limit(1).get())
    if not docs:
        return {"status": "none"}

    data = docs[0].to_dict()
    # Check if active subscription has expired
    if data.get('status') == 'active' and data.get('expires_at'):
        if datetime.now(timezone.utc).isoformat() > data['expires_at']:
            docs[0].reference.update({"status": "expired"})
            data['status'] = 'expired'
    return data

# ============= ADMIN REQUEST MANAGEMENT =============

@api_router.get("/admin/requests")
async def admin_list_requests(page: int = 1, per_page: int = 20, status: Optional[str] = None, service_type: Optional[str] = None, current_user: User = Depends(get_admin_user)):
    """Admin lists all requests with optional filters."""
    docs = list(db.collection('teaching_requests').stream())
    all_items = [doc.to_dict() for doc in docs]
    if status:
        all_items = [i for i in all_items if i.get("status") == status]
    if service_type:
        all_items = [i for i in all_items if i.get("service_type") == service_type]
    # Sort by created_at descending
    all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    total = len(all_items)
    start = (page - 1) * per_page
    items = all_items[start:start + per_page]
    return {"items": items, "total": total, "page": page, "per_page": per_page, "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0}

class AdminUpdateRequest(BaseModel):
    status: Optional[Literal["new", "offered", "accepted", "assigned", "in_progress", "completed", "cancelled"]] = None

@api_router.put("/admin/requests/{request_id}")
async def admin_update_request(request_id: str, data: AdminUpdateRequest, current_user: User = Depends(get_admin_user)):
    """Admin updates request status."""
    ref = db.collection('teaching_requests').document(request_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Request not found")
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    ref.update(updates)
    await log_audit(current_user.user_id, "admin_update_request", "teaching_request", request_id)
    return {"message": "Request updated"}

@api_router.delete("/admin/requests/{request_id}")
async def admin_delete_request(request_id: str, current_user: User = Depends(get_admin_user)):
    """Admin deletes a request."""
    ref = db.collection('teaching_requests').document(request_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Request not found")
    ref.delete()
    await log_audit(current_user.user_id, "admin_delete_request", "teaching_request", request_id)
    return {"message": "Request deleted"}

@api_router.get("/admin/subscriptions")
async def admin_list_subscriptions(status: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Admin lists subscription requests."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    ref = db.collection('substitute_subscriptions')
    if status:
        docs = list(ref.where('status', '==', status).get())
    else:
        docs = list(ref.get())

    result = []
    for doc in docs:
        sub = doc.to_dict()
        # Enrich with teacher info
        user_snap = db.collection('users').document(sub['user_id']).get()
        if user_snap.exists:
            u = user_snap.to_dict()
            sub['teacher_name'] = u.get('name', '')
            sub['teacher_email'] = u.get('email', '')
            sub['teacher_phone'] = u.get('phone', '')
        # Enrich with substitute profile
        prof_docs = list(db.collection('substitute_profiles').where('user_id', '==', sub['user_id']).limit(1).get())
        if prof_docs:
            p = prof_docs[0].to_dict()
            sub['specializations'] = p.get('specializations', [])
            sub['education_stages'] = p.get('education_stages', [])
            sub['city'] = p.get('city', '')
            sub['region'] = p.get('region', '')
            sub['availability_mode'] = p.get('availability_mode', '')
            sub['bio'] = p.get('bio', '')
            sub['certificate_url'] = p.get('certificate_url', '')
        result.append(sub)
    return result

@api_router.post("/admin/subscriptions/{subscription_id}/approve")
async def admin_approve_subscription(subscription_id: str, current_user: User = Depends(get_current_user)):
    """Admin approves a subscription request — active for 30 days."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    ref = db.collection('substitute_subscriptions').document(subscription_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Subscription not found")

    now = datetime.now(timezone.utc)
    ref.update({
        "status": "active",
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
    })
    await log_audit(current_user.user_id, "approve_subscription", "substitute_subscription", subscription_id)

    # Notify teacher
    sub_data = snap.to_dict()
    await create_notification(
        sub_data['user_id'],
        "subscription_approved",
        "تم تفعيل اشتراكك",
        "تم قبول اشتراكك في نظام المعلم البديل. يمكنك الآن استعراض الطلبات.",
        "/substitute"
    )
    return {"message": "Subscription approved"}

@api_router.post("/admin/subscriptions/{subscription_id}/reject")
async def admin_reject_subscription(subscription_id: str, current_user: User = Depends(get_current_user)):
    """Admin rejects a subscription request."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    ref = db.collection('substitute_subscriptions').document(subscription_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Subscription not found")

    ref.update({"status": "inactive"})
    await log_audit(current_user.user_id, "reject_subscription", "substitute_subscription", subscription_id)

    sub_data = snap.to_dict()
    await create_notification(
        sub_data['user_id'],
        "subscription_rejected",
        "تم رفض طلب الاشتراك",
        "تم رفض طلب اشتراكك في نظام المعلم البديل.",
        "/substitute"
    )
    return {"message": "Subscription rejected"}

# ---- Substitute Profile ----

@api_router.post("/substitute/profile")
async def create_substitute_profile(profile_data: SubstituteProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update substitute teacher profile."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can create substitute profiles")

    existing = list(db.collection('substitute_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
    if existing:
        existing[0].reference.update(profile_data.model_dump())
        doc = existing[0].to_dict()
        doc.update(profile_data.model_dump())
        return doc

    profile_id = f"subprof_{uuid.uuid4().hex[:12]}"
    doc = {
        "profile_id": profile_id,
        "user_id": current_user.user_id,
        **profile_data.model_dump(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db.collection('substitute_profiles').document(profile_id).set(doc)
    await log_audit(current_user.user_id, "create_substitute_profile", "substitute_profile", profile_id)
    return doc

@api_router.get("/substitute/profile")
async def get_my_substitute_profile(current_user: User = Depends(get_current_user)):
    """Get my substitute profile."""
    docs = list(db.collection('substitute_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
    if not docs:
        raise HTTPException(status_code=404, detail="Substitute profile not found")
    return docs[0].to_dict()

@api_router.get("/substitute/profile/{user_id}")
async def get_substitute_profile_by_user(user_id: str, current_user: User = Depends(get_current_user)):
    """Get a teacher's substitute profile (for schools viewing offers)."""
    docs = list(db.collection('substitute_profiles').where('user_id', '==', user_id).limit(1).get())
    if not docs:
        raise HTTPException(status_code=404, detail="Substitute profile not found")
    return docs[0].to_dict()

# ---- Substitute Request Listing (subscription-gated) ----

@api_router.get("/substitute/requests")
async def list_substitute_requests(page: int = 1, per_page: int = 12, current_user: User = Depends(get_current_user)):
    """List available substitute requests for subscribed teachers, with optional matching filters."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can view substitute requests")

    # Require active subscription
    sub_docs = list(db.collection('substitute_subscriptions').where('user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).get())
    if not sub_docs:
        raise HTTPException(status_code=403, detail="Active subscription required to view substitute requests")

    # Fetch substitute requests that are open
    docs = list(db.collection('teaching_requests').where('service_type', '==', 'substitute').get())
    all_items = [d.to_dict() for d in docs if d.to_dict().get('status') in ['new', 'offered']]

    # Optionally filter by teacher's substitute profile
    profile_docs = list(db.collection('substitute_profiles').where('user_id', '==', current_user.user_id).limit(1).get())
    if profile_docs:
        profile = profile_docs[0].to_dict()
        teacher_stages = set(profile.get('education_stages', []))
        teacher_specs = set(profile.get('specializations', []))
        teacher_city = profile.get('city', '')
        # Filter: match stage OR specialization OR city (broad matching)
        filtered = []
        for req in all_items:
            stage_match = not teacher_stages or req.get('education_stage', '') in teacher_stages or not req.get('education_stage')
            spec_match = not teacher_specs or req.get('subject', '') in teacher_specs or not req.get('subject')
            city_match = not teacher_city or req.get('city', '') == teacher_city or not req.get('city')
            if stage_match or spec_match or city_match:
                filtered.append(req)
        all_items = filtered

    all_items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    total = len(all_items)
    start = (page - 1) * per_page
    items = all_items[start:start + per_page]
    return {"items": items, "total": total, "page": page, "per_page": per_page, "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0}

# ============= SCHOOL SUBSCRIPTION ROUTES =============

@api_router.post("/school-subscriptions")
async def create_school_subscription(data: SchoolSubscriptionCreate, current_user: User = Depends(get_current_user)):
    """School admin subscribes to a plan. Auto-approved."""
    if current_user.role != "school_admin":
        raise HTTPException(status_code=403, detail="Only school admins can subscribe")
    
    # Find the school for this admin
    school_docs = list(db.collection('schools').where('admin_user_id', '==', current_user.user_id).limit(1).stream())
    if not school_docs:
        raise HTTPException(status_code=400, detail="No school found. Set up your school first.")
    school = school_docs[0].to_dict()
    
    # Check if already has active subscription
    existing = list(db.collection('school_subscriptions').where('admin_user_id', '==', current_user.user_id).where('status', '==', 'active').limit(1).stream())
    if existing:
        raise HTTPException(status_code=400, detail="You already have an active subscription")
    
    now = datetime.now(timezone.utc)
    if data.billing_cycle == "monthly":
        expires = now + timedelta(days=30)
    else:
        expires = now + timedelta(days=365)
    
    sub_id = str(uuid.uuid4())
    sub_doc = {
        "subscription_id": sub_id,
        "school_id": school["school_id"],
        "admin_user_id": current_user.user_id,
        "plan": data.plan,
        "billing_cycle": data.billing_cycle,
        "request_limit": SCHOOL_PLAN_LIMITS[data.plan],
        "requests_used": 0,
        "status": "active",
        "payment_status": "unpaid",
        "started_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }
    db.collection('school_subscriptions').document(sub_id).set(sub_doc)
    await log_audit(current_user.user_id, "create_school_subscription", "school_subscription", sub_id)
    return sub_doc

@api_router.get("/school-subscriptions/mine")
async def get_my_school_subscription(current_user: User = Depends(get_current_user)):
    """Get current school admin's active subscription."""
    if current_user.role != "school_admin":
        raise HTTPException(status_code=403, detail="Only school admins")
    
    docs = list(db.collection('school_subscriptions').where('admin_user_id', '==', current_user.user_id).stream())
    if not docs:
        return None
    
    # Return the most recent / active one
    subs = [d.to_dict() for d in docs]
    # Check for expiry
    now = datetime.now(timezone.utc).isoformat()
    for s in subs:
        if s['status'] == 'active' and s.get('expires_at') and now > s['expires_at']:
            db.collection('school_subscriptions').document(s['subscription_id']).update({"status": "expired"})
            s['status'] = 'expired'
    
    active = [s for s in subs if s['status'] == 'active']
    if active:
        return active[0]
    # Return the latest one even if expired/inactive
    subs.sort(key=lambda x: x.get('started_at', ''), reverse=True)
    return subs[0] if subs else None

@api_router.get("/admin/school-subscriptions")
async def admin_list_school_subscriptions(status: Optional[str] = None, current_user: User = Depends(get_admin_user)):
    """Admin lists all school subscriptions."""
    docs = list(db.collection('school_subscriptions').stream())
    subs = [d.to_dict() for d in docs]
    
    # Enrich with school name
    for s in subs:
        school_docs = list(db.collection('schools').where('school_id', '==', s.get('school_id')).limit(1).stream())
        if school_docs:
            s['school_name'] = school_docs[0].to_dict().get('name', '')
        user_docs = list(db.collection('users').where('user_id', '==', s.get('admin_user_id')).limit(1).stream())
        if user_docs:
            s['admin_name'] = user_docs[0].to_dict().get('name', '')
            s['admin_email'] = user_docs[0].to_dict().get('email', '')
    
    if status:
        subs = [s for s in subs if s.get('status') == status]
    subs.sort(key=lambda x: x.get('started_at', ''), reverse=True)
    return subs

class AdminUpdateSchoolSub(BaseModel):
    status: Optional[Literal["active", "inactive", "expired"]] = None
    payment_status: Optional[Literal["paid", "unpaid"]] = None

@api_router.put("/admin/school-subscriptions/{subscription_id}")
async def admin_update_school_subscription(subscription_id: str, data: AdminUpdateSchoolSub, current_user: User = Depends(get_admin_user)):
    """Admin updates school subscription (status, payment)."""
    ref = db.collection('school_subscriptions').document(subscription_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Subscription not found")
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    ref.update(updates)
    await log_audit(current_user.user_id, "admin_update_school_sub", "school_subscription", subscription_id)
    return {"message": "Subscription updated"}

# ============= REPORTS ROUTES =============

@api_router.get("/reports/stats", response_model=DashboardStats)
async def get_dashboard_stats(current_user: User = Depends(get_current_user)):
    """Get dashboard statistics."""
    total_requests = len(list(db.collection('teaching_requests').stream()))
    completed_sessions = len(list(db.collection('assignments').where('status', '==', 'completed').stream()))
    active_teachers = len(list(db.collection('teacher_profiles').stream()))
    active_students = len(list(db.collection('student_profiles').stream()))
    pending_requests = len(list(db.collection('teaching_requests').where('status', 'in', ['new', 'offered']).stream()))
    
    return DashboardStats(
        total_requests=total_requests,
        completed_sessions=completed_sessions,
        active_teachers=active_teachers,
        active_students=active_students,
        pending_requests=pending_requests
    )

# ============= ROOT ROUTE =============

@api_router.get("/")
async def root():
    return {"message": "منصة بديل التعليمية API"}

# Include router in app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o.strip() for o in os.environ.get('CORS_ORIGINS', 'http://localhost:3000,http://localhost:3001').split(',')],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
