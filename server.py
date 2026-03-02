from fastapi import FastAPI, APIRouter, HTTPException, Depends, Cookie, Response, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, field_validator
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import httpx
import resend
import asyncio

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Resend configuration
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")

# Security
security = HTTPBearer(auto_error=False)

logger = logging.getLogger(__name__)

# ============= MODELS =============

class User(BaseModel):
    user_id: str
    email: EmailStr
    name: str
    picture: Optional[str] = None
    role: Literal["admin", "school_admin", "teacher", "student", "guardian"]
    created_at: str

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: Literal["admin", "school_admin", "teacher", "student", "guardian"]

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class SessionExchangeRequest(BaseModel):
    session_id: str

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
    created_at: str

class TeacherProfileCreate(BaseModel):
    specialization: str
    grades: List[str]
    cities: List[str]
    availability: str
    remote_enabled: bool = True
    in_person_enabled: bool = True
    bio: Optional[str] = None

class StudentProfile(BaseModel):
    student_id: str
    user_id: str
    grade: str
    preferred_subjects: List[str] = []
    special_needs: Optional[str] = None
    created_at: str

class StudentProfileCreate(BaseModel):
    grade: str
    preferred_subjects: List[str] = []
    special_needs: Optional[str] = None

class School(BaseModel):
    school_id: str
    name: str
    admin_user_id: str
    city: str
    contact_email: Optional[EmailStr] = None
    created_at: str

class SchoolCreate(BaseModel):
    name: str
    city: str
    contact_email: Optional[EmailStr] = None

class TeachingRequest(BaseModel):
    request_id: str
    created_by_user_id: str
    service_type: Literal["substitute", "remote_school", "special_education"]
    subject: str
    grade: str
    mode: Literal["in_person", "remote"]
    city: Optional[str] = None
    date_time: str
    notes: Optional[str] = None
    status: Literal["new", "offered", "accepted", "assigned", "in_progress", "completed", "cancelled"] = "new"
    created_at: str

class TeachingRequestCreate(BaseModel):
    service_type: Literal["substitute", "remote_school", "special_education"]
    subject: str
    grade: str
    mode: Literal["in_person", "remote"]
    city: Optional[str] = None
    date_time: str
    notes: Optional[str] = None

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
    notes: Optional[str] = None
    status: Literal["scheduled", "in_progress", "completed", "cancelled"] = "scheduled"
    created_at: str
    completed_at: Optional[str] = None

class AssignmentUpdate(BaseModel):
    meeting_link: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[Literal["scheduled", "in_progress", "completed", "cancelled"]] = None

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
    
    session_doc = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
    if not session_doc:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    expires_at = session_doc["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    user_doc = await db.users.find_one({"user_id": session_doc["user_id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    
    return User(**user_doc)

async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

async def log_audit(user_id: str, action: str, entity_type: str, entity_id: str):
    """Log an audit entry."""
    log = {
        "log_id": f"log_{uuid.uuid4().hex[:12]}",
        "user_id": user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await db.audit_logs.insert_one(log)

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
async def register(user_data: UserCreate):
    """Register a new user with email/password."""
    existing_user = await db.users.find_one({"email": user_data.email}, {"_id": 0})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = bcrypt.hashpw(user_data.password.encode(), bcrypt.gensalt())
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    
    user_doc = {
        "user_id": user_id,
        "email": user_data.email,
        "name": user_data.name,
        "picture": None,
        "role": user_data.role,
        "password_hash": hashed_password.decode(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.users.insert_one(user_doc)
    await log_audit(user_id, "register", "user", user_id)
    
    user_doc.pop("password_hash")
    user_doc.pop("_id", None)
    return {"message": "User registered successfully", "user": user_doc}

@api_router.post("/auth/login")
async def login(response: Response, login_data: LoginRequest):
    """Login with email/password."""
    user_doc = await db.users.find_one({"email": login_data.email})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if not bcrypt.checkpw(login_data.password.encode(), user_doc["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    session_token = f"session_{uuid.uuid4().hex}"
    session_doc = {
        "user_id": user_doc["user_id"],
        "session_token": session_token,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.user_sessions.insert_one(session_doc)
    
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7 * 24 * 60 * 60
    )
    
    user_doc.pop("password_hash")
    user_doc.pop("_id")
    
    return {"message": "Login successful", "user": user_doc, "session_token": session_token}

@api_router.post("/auth/session")
async def exchange_session(response: Response, request: SessionExchangeRequest):
    """Exchange Google OAuth session_id for user data."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": request.session_id}
            )
            resp.raise_for_status()
            oauth_data = resp.json()
    except Exception as e:
        logger.error(f"Failed to exchange session: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid session_id")
    
    user_doc = await db.users.find_one({"email": oauth_data["email"]}, {"_id": 0})
    
    if not user_doc:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user_doc = {
            "user_id": user_id,
            "email": oauth_data["email"],
            "name": oauth_data["name"],
            "picture": oauth_data.get("picture"),
            "role": "student",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(user_doc)
        await log_audit(user_id, "register_oauth", "user", user_id)
    
    session_token = oauth_data["session_token"]
    session_doc = {
        "user_id": user_doc["user_id"],
        "session_token": session_token,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.user_sessions.insert_one(session_doc)
    
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7 * 24 * 60 * 60
    )
    
    user_doc.pop("_id", None)
    return {"message": "Session created", "user": user_doc}

@api_router.get("/auth/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user info."""
    return current_user

@api_router.post("/auth/logout")
async def logout(response: Response, current_user: User = Depends(get_current_user), request: Request = None):
    """Logout and clear session."""
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out successfully"}

# ============= USER ROUTES =============

@api_router.get("/users", response_model=List[User])
async def list_users(current_user: User = Depends(get_admin_user)):
    """List all users (admin only)."""
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(1000)
    return users

@api_router.put("/users/{user_id}")
async def update_user(user_id: str, role: str, current_user: User = Depends(get_admin_user)):
    """Update user role (admin only)."""
    result = await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"role": role}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    await log_audit(current_user.user_id, "update_user_role", "user", user_id)
    return {"message": "User updated successfully"}

# ============= TEACHER PROFILE ROUTES =============

@api_router.post("/teachers/profile", response_model=TeacherProfile)
async def create_teacher_profile(profile_data: TeacherProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update teacher profile."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can create teacher profiles")
    
    existing_profile = await db.teacher_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
    
    if existing_profile:
        await db.teacher_profiles.update_one(
            {"user_id": current_user.user_id},
            {"$set": profile_data.model_dump()}
        )
        existing_profile.update(profile_data.model_dump())
        return TeacherProfile(**existing_profile)
    else:
        teacher_id = f"teacher_{uuid.uuid4().hex[:12]}"
        profile_doc = {
            "teacher_id": teacher_id,
            "user_id": current_user.user_id,
            **profile_data.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.teacher_profiles.insert_one(profile_doc)
        await log_audit(current_user.user_id, "create_teacher_profile", "teacher_profile", teacher_id)
        profile_doc.pop("_id", None)
        return TeacherProfile(**profile_doc)

@api_router.get("/teachers/profile", response_model=TeacherProfile)
async def get_my_teacher_profile(current_user: User = Depends(get_current_user)):
    """Get my teacher profile."""
    profile = await db.teacher_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    return TeacherProfile(**profile)

@api_router.get("/teachers", response_model=List[TeacherProfile])
async def list_teachers(current_user: User = Depends(get_current_user)):
    """List all teachers."""
    teachers = await db.teacher_profiles.find({}, {"_id": 0}).to_list(1000)
    return teachers

# ============= STUDENT PROFILE ROUTES =============

@api_router.post("/students/profile", response_model=StudentProfile)
async def create_student_profile(profile_data: StudentProfileCreate, current_user: User = Depends(get_current_user)):
    """Create or update student profile."""
    if current_user.role not in ["student", "guardian"]:
        raise HTTPException(status_code=403, detail="Only students/guardians can create student profiles")
    
    existing_profile = await db.student_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
    
    if existing_profile:
        await db.student_profiles.update_one(
            {"user_id": current_user.user_id},
            {"$set": profile_data.model_dump()}
        )
        existing_profile.update(profile_data.model_dump())
        return StudentProfile(**existing_profile)
    else:
        student_id = f"student_{uuid.uuid4().hex[:12]}"
        profile_doc = {
            "student_id": student_id,
            "user_id": current_user.user_id,
            **profile_data.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.student_profiles.insert_one(profile_doc)
        await log_audit(current_user.user_id, "create_student_profile", "student_profile", student_id)
        profile_doc.pop("_id", None)
        return StudentProfile(**profile_doc)

@api_router.get("/students/profile", response_model=StudentProfile)
async def get_my_student_profile(current_user: User = Depends(get_current_user)):
    """Get my student profile."""
    profile = await db.student_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return StudentProfile(**profile)

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
    
    await db.schools.insert_one(school_doc)
    await log_audit(current_user.user_id, "create_school", "school", school_id)
    school_doc.pop("_id", None)
    return School(**school_doc)

@api_router.get("/schools", response_model=List[School])
async def list_schools(current_user: User = Depends(get_current_user)):
    """List schools."""
    schools = await db.schools.find({}, {"_id": 0}).to_list(1000)
    return schools

# ============= TEACHING REQUEST ROUTES =============

@api_router.post("/requests", response_model=TeachingRequest)
async def create_request(request_data: TeachingRequestCreate, current_user: User = Depends(get_current_user)):
    """Create a teaching request."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_doc = {
        "request_id": request_id,
        "created_by_user_id": current_user.user_id,
        **request_data.model_dump(),
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.teaching_requests.insert_one(request_doc)
    await log_audit(current_user.user_id, "create_request", "teaching_request", request_id)
    request_doc.pop("_id", None)
    return TeachingRequest(**request_doc)

@api_router.get("/requests", response_model=List[TeachingRequest])
async def list_requests(current_user: User = Depends(get_current_user)):
    """List teaching requests (filtered by role)."""
    if current_user.role == "teacher":
        requests = await db.teaching_requests.find(
            {"status": {"$in": ["new", "offered"]}},
            {"_id": 0}
        ).to_list(1000)
    elif current_user.role in ["student", "guardian"]:
        requests = await db.teaching_requests.find(
            {"created_by_user_id": current_user.user_id},
            {"_id": 0}
        ).to_list(1000)
    elif current_user.role in ["admin", "school_admin"]:
        requests = await db.teaching_requests.find({}, {"_id": 0}).to_list(1000)
    else:
        requests = []
    
    return requests

@api_router.get("/requests/{request_id}", response_model=TeachingRequest)
async def get_request(request_id: str, current_user: User = Depends(get_current_user)):
    """Get a specific teaching request."""
    request_doc = await db.teaching_requests.find_one({"request_id": request_id}, {"_id": 0})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Request not found")
    return TeachingRequest(**request_doc)

@api_router.put("/requests/{request_id}/status")
async def update_request_status(request_id: str, status: str, current_user: User = Depends(get_current_user)):
    """Update request status."""
    result = await db.teaching_requests.update_one(
        {"request_id": request_id},
        {"$set": {"status": status}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Request not found")
    
    await log_audit(current_user.user_id, f"update_request_status_{status}", "teaching_request", request_id)
    return {"message": "Request status updated"}

# ============= OFFER ROUTES =============

@api_router.post("/requests/{request_id}/offer", response_model=RequestOffer)
async def create_offer(request_id: str, current_user: User = Depends(get_current_user)):
    """Teacher accepts a request."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can accept requests")
    
    request_doc = await db.teaching_requests.find_one({"request_id": request_id}, {"_id": 0})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Request not found")
    
    if request_doc["status"] not in ["new", "offered"]:
        raise HTTPException(status_code=400, detail="Request is no longer available")
    
    teacher_profile = await db.teacher_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
    if not teacher_profile:
        raise HTTPException(status_code=400, detail="Teacher profile not found")
    
    existing_offer = await db.request_offers.find_one(
        {"request_id": request_id, "teacher_id": teacher_profile["teacher_id"]},
        {"_id": 0}
    )
    if existing_offer:
        raise HTTPException(status_code=400, detail="You already made an offer for this request")
    
    offer_id = f"offer_{uuid.uuid4().hex[:12]}"
    offer_doc = {
        "offer_id": offer_id,
        "request_id": request_id,
        "teacher_id": teacher_profile["teacher_id"],
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.request_offers.insert_one(offer_doc)
    
    await db.teaching_requests.update_one(
        {"request_id": request_id},
        {"$set": {"status": "offered"}}
    )
    
    offer_count = await db.request_offers.count_documents({"request_id": request_id})
    if offer_count == 1:
        await db.request_offers.update_one(
            {"offer_id": offer_id},
            {"$set": {"status": "accepted"}}
        )
        
        await db.teaching_requests.update_one(
            {"request_id": request_id},
            {"$set": {"status": "accepted"}}
        )
        
        assignment_id = f"assign_{uuid.uuid4().hex[:12]}"
        assignment_doc = {
            "assignment_id": assignment_id,
            "request_id": request_id,
            "teacher_id": teacher_profile["teacher_id"],
            "student_id": request_doc["created_by_user_id"],
            "meeting_link": None,
            "notes": None,
            "status": "scheduled",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None
        }
        await db.assignments.insert_one(assignment_doc)
        
        requester = await db.users.find_one({"user_id": request_doc["created_by_user_id"]}, {"_id": 0})
        if requester and RESEND_API_KEY:
            html = f"<h2>تم قبول طلبك التعليمي</h2><p>تم تعيين معلم لطلبك. يرجى تسجيل الدخول لعرض التفاصيل.</p>"
            await send_email_notification(requester["email"], "تم قبول طلبك - منصة بديل", html)
        
        await log_audit(current_user.user_id, "accept_request", "teaching_request", request_id)
    
    offer_doc.pop("_id", None)
    return RequestOffer(**offer_doc)

@api_router.get("/requests/{request_id}/offers", response_model=List[RequestOffer])
async def list_offers(request_id: str, current_user: User = Depends(get_current_user)):
    """List offers for a request."""
    offers = await db.request_offers.find({"request_id": request_id}, {"_id": 0}).to_list(1000)
    return offers

# ============= ASSIGNMENT ROUTES =============

@api_router.get("/assignments", response_model=List[Assignment])
async def list_assignments(current_user: User = Depends(get_current_user)):
    """List my assignments."""
    if current_user.role == "teacher":
        teacher_profile = await db.teacher_profiles.find_one({"user_id": current_user.user_id}, {"_id": 0})
        if not teacher_profile:
            return []
        assignments = await db.assignments.find(
            {"teacher_id": teacher_profile["teacher_id"]},
            {"_id": 0}
        ).to_list(1000)
    elif current_user.role in ["student", "guardian"]:
        assignments = await db.assignments.find(
            {"student_id": current_user.user_id},
            {"_id": 0}
        ).to_list(1000)
    elif current_user.role in ["admin", "school_admin"]:
        assignments = await db.assignments.find({}, {"_id": 0}).to_list(1000)
    else:
        assignments = []
    
    return assignments

@api_router.get("/assignments/{assignment_id}", response_model=Assignment)
async def get_assignment(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Get a specific assignment."""
    assignment = await db.assignments.find_one({"assignment_id": assignment_id}, {"_id": 0})
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return Assignment(**assignment)

@api_router.put("/assignments/{assignment_id}", response_model=Assignment)
async def update_assignment(assignment_id: str, update_data: AssignmentUpdate, current_user: User = Depends(get_current_user)):
    """Update assignment (meeting link, notes, status)."""
    update_dict = {k: v for k, v in update_data.model_dump().items() if v is not None}
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    result = await db.assignments.update_one(
        {"assignment_id": assignment_id},
        {"$set": update_dict}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    assignment = await db.assignments.find_one({"assignment_id": assignment_id}, {"_id": 0})
    await log_audit(current_user.user_id, "update_assignment", "assignment", assignment_id)
    return Assignment(**assignment)

@api_router.put("/assignments/{assignment_id}/complete")
async def complete_assignment(assignment_id: str, current_user: User = Depends(get_current_user)):
    """Mark assignment as completed."""
    assignment = await db.assignments.find_one({"assignment_id": assignment_id}, {"_id": 0})
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    result = await db.assignments.update_one(
        {"assignment_id": assignment_id},
        {"$set": {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    await db.teaching_requests.update_one(
        {"request_id": assignment["request_id"]},
        {"$set": {"status": "completed"}}
    )
    
    await log_audit(current_user.user_id, "complete_assignment", "assignment", assignment_id)
    return {"message": "Assignment completed successfully"}

# ============= REPORTS ROUTES =============

@api_router.get("/reports/stats", response_model=DashboardStats)
async def get_dashboard_stats(current_user: User = Depends(get_current_user)):
    """Get dashboard statistics."""
    total_requests = await db.teaching_requests.count_documents({})
    completed_sessions = await db.assignments.count_documents({"status": "completed"})
    active_teachers = await db.teacher_profiles.count_documents({})
    active_students = await db.student_profiles.count_documents({})
    pending_requests = await db.teaching_requests.count_documents({"status": {"$in": ["new", "offered"]}})
    
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
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
