from typing import Optional, List
import os
import uuid
import hashlib

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from sqlalchemy import (
	create_engine,
	Column,
	String,
	ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError


load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./dev.db"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
	__tablename__ = "users"
	id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
	full_name = Column(String(255), nullable=False)
	email = Column(String(255), nullable=False, unique=True, index=True)
	password = Column(String(128), nullable=False)
	role = Column(String(50), nullable=False)
	status = Column(String(50), nullable=False)


class Member(Base):
	__tablename__ = "members"
	id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
	leader_id = Column(String(36), ForeignKey("users.id"), nullable=False)
	member_id = Column(String(36), ForeignKey("users.id"), nullable=False)
	status = Column(String(50), nullable=False)
	profile_image = Column(String(1024), nullable=True)


Base.metadata.create_all(bind=engine)


class UserCreate(BaseModel):
	full_name: str
	email: EmailStr
	password: str
	role: str
	status: str
	leader_id: Optional[str] = None


class UserResponse(BaseModel):
	id: str
	full_name: str
	email: EmailStr
	role: str
	status: str


class LoginRequest(BaseModel):
	email: EmailStr
	password: str


class LeaderResponse(BaseModel):
	id: str
	full_name: str



class MemberInfo(BaseModel):
	member_id: str
	full_name: str
	email: EmailStr
	profile_image: Optional[str] = None
	status: str


app = FastAPI(title="Pitching-backend")

# Enable CORS for all origins
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

# include projects router
try:
	from projects import router as projects_router

	app.include_router(projects_router)
except Exception:
	# don't fail import if projects module can't be loaded in certain environments
	pass


@app.get("/health", tags=["health"])
async def health():
	return {"status": "ok"}


def _hash_password(raw: str) -> str:
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _supabase_client():
	try:
		from supabase import create_client

		SUPABASE_URL = os.getenv("SUPABASE_URL")
		SUPABASE_KEY = os.getenv("SUPABASE_KEY")
		if not SUPABASE_URL or not SUPABASE_KEY:
			return None
		return create_client(SUPABASE_URL, SUPABASE_KEY)
	except Exception:
		return None


@app.post("/members/{member_id}/profile-image")
async def upload_member_profile_image(member_id: str, file: UploadFile = File(...)):
	"""Upload a profile image for a member and update the `members.profile_image` field."""
	supabase = _supabase_client()
	if not supabase:
		raise HTTPException(status_code=500, detail="Supabase client not configured (check SUPABASE_URL and SUPABASE_KEY)")

	db = SessionLocal()
	try:
		member = db.query(Member).filter(Member.member_id == member_id).first()
		if not member:
			raise HTTPException(status_code=404, detail="member not found")

		# prepare destination path
		dest_path = f"members/{uuid.uuid4()}_{file.filename}"
		data = await file.read()

		# ensure bucket exists and upload
		bucket = os.getenv("SUPABASE_BUCKET")
		if not bucket:
			raise HTTPException(status_code=500, detail="SUPABASE_BUCKET not configured")

		try:
			try:
				supabase.storage.get_bucket(bucket)
			except Exception:
				supabase.storage.create_bucket(bucket, public=True)

			supabase.storage.from_(bucket).upload(dest_path, data)
		finally:
			await file.close()

		supabase_url = os.getenv("SUPABASE_URL")
		if supabase_url:
			public_url = f"{supabase_url}/storage/v1/object/public/{bucket}/{dest_path}"
		else:
			public_url = dest_path

		# update member record
		member.profile_image = public_url
		db.add(member)
		db.commit()
		db.refresh(member)

		return {"member_id": member_id, "profile_image": public_url}
	finally:
		db.close()


@app.post("/users", response_model=UserResponse)
def create_user(payload: UserCreate):
	# If creating a member, require leader_id and give a friendly message if missing
	if payload.role.lower() == "member" and not payload.leader_id:
		raise HTTPException(status_code=400, detail="please select your leader")

	db = SessionLocal()
	try:
		# If role is member, verify leader exists before creating the new user
		if payload.role.lower() == "member":
			leader = db.get(User, payload.leader_id)
			if not leader:
				raise HTTPException(status_code=400, detail="leader_id does not reference an existing user")

		hashed = _hash_password(payload.password)
		user = User(
			full_name=payload.full_name,
			email=payload.email,
			password=hashed,
			role=payload.role,
			status=payload.status,
		)

		# Add user and member (if applicable) in one transaction so both must succeed
		db.add(user)
		db.flush()  # assign PK for user.id without committing

		if payload.role.lower() == "member":
			member = Member(leader_id=payload.leader_id, member_id=user.id, status=payload.status)
			db.add(member)

		db.commit()
		db.refresh(user)

		return UserResponse(
			id=user.id,
			full_name=user.full_name,
			email=user.email,
			role=user.role,
			status=user.status,
		)
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=400, detail="email already exists")
	finally:
		db.close()



@app.post("/login", response_model=UserResponse)
def login(payload: LoginRequest):
	db = SessionLocal()
	try:
		user = db.query(User).filter_by(email=payload.email).first()
		if not user:
			raise HTTPException(status_code=401, detail="invalid email or password")

		if _hash_password(payload.password) != user.password:
			raise HTTPException(status_code=401, detail="invalid email or password")

		# Block login for pending accounts and return specific messages
		if user.role.lower() == "member":
			member = db.query(Member).filter_by(member_id=user.id).first()
			if member and member.status.lower() == "pending":
				leader = db.get(User, member.leader_id)
				leader_name = leader.full_name if leader else "your team leader"
				raise HTTPException(status_code=403, detail=f"Waiting for your team leader {leader_name} to approve")
			# prefer member.status when available
			status = member.status if member else user.status
		else:
			if user.status.lower() == "pending":
				raise HTTPException(status_code=403, detail="Waiting for account approved")
			status = user.status

		return UserResponse(
			id=user.id,
			full_name=user.full_name,
			email=user.email,
			role=user.role,
			status=status,
		)
	finally:
		db.close()


@app.get("/leaders", response_model=List[LeaderResponse])
def get_leaders():
	db = SessionLocal()
	try:
		leaders = db.query(User).filter(User.role.ilike("leader")).all()
		return [LeaderResponse(id=u.id, full_name=u.full_name) for u in leaders]
	finally:
		db.close()


@app.get("/members/leader/{leader_id}", response_model=List[MemberInfo])
def get_members_by_leader(leader_id: str):
	db = SessionLocal()
	try:
		rows = (
			db.query(User, Member)
			.join(Member, Member.member_id == User.id)
			.filter(Member.leader_id == leader_id)
			.all()
		)
		result = []
		for user, member in rows:
			result.append(
				MemberInfo(member_id=member.member_id, full_name=user.full_name, email=user.email, profile_image=member.profile_image, status=member.status)
			)
		return result
	finally:
		db.close()


class StatusUpdate(BaseModel):
	status: str


@app.get("/users", response_model=List[UserResponse])
def list_users():
	db = SessionLocal()
	try:
		users = db.query(User).all()
		return [
			UserResponse(
				id=u.id, full_name=u.full_name, email=u.email, role=u.role, status=u.status
			)
			for u in users
		]
	finally:
		db.close()


@app.patch("/users/{user_id}/status", response_model=UserResponse)
def update_user_status(user_id: str, payload: StatusUpdate):
	db = SessionLocal()
	try:
		user = db.get(User, user_id)
		if not user:
			raise HTTPException(status_code=404, detail="user not found")

		# Update user status
		user.status = payload.status
		db.add(user)

		# If the user is a member, also update the members table for that member_id
		if user.role and user.role.lower() == "member":
			members = db.query(Member).filter(Member.member_id == user.id).all()
			for m in members:
				m.status = payload.status
				db.add(m)

		db.commit()
		db.refresh(user)

		return UserResponse(
			id=user.id, full_name=user.full_name, email=user.email, role=user.role, status=user.status
		)
	finally:
		db.close()


if __name__ == "__main__":
	import uvicorn

	uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

