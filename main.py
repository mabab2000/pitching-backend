from typing import Optional, List
import os
import uuid
import hashlib

from fastapi import FastAPI, HTTPException
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


app = FastAPI(title="Pitching-backend")


@app.get("/health", tags=["health"])
async def health():
	return {"status": "ok"}


def _hash_password(raw: str) -> str:
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


if __name__ == "__main__":
	import uvicorn

	uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

