import os
import uuid
from typing import Optional, List
from dotenv import load_dotenv
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, Column, String, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./dev.db"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    leader_id = Column(String(36), nullable=False)
    project_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project_image = Column(String(1024), nullable=True)
    leader_image = Column(String(1024), nullable=True)


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

router = APIRouter()


class ProjectCreateResponse(BaseModel):
    id: str
    leader_id: str
    project_name: str
    description: str | None
    project_image_url: str | None
    leader_image_url: str | None


def _supabase_client():
    try:
        from supabase import create_client

        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        return None


def _upload_file_to_bucket(supabase, bucket: str, file: UploadFile, dest_path: str) -> str:
    data = file.file.read()
    # try create bucket if not exists
    try:
        # ensure bucket exists
        try:
            supabase.storage.get_bucket(bucket)
        except Exception:
            supabase.storage.create_bucket(bucket, public=True)

        supabase.storage.from_(bucket).upload(dest_path, data)
        # construct public URL
        if SUPABASE_URL:
            # SUPABASE_URL is like https://xyz.supabase.co
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{dest_path}"
        else:
            public_url = dest_path
        return public_url
    finally:
        file.file.close()


@router.post("/projects", response_model=ProjectCreateResponse)
async def create_project(
    leader_id: str = Form(...),
    project_name: str = Form(...),
    description: str = Form(None),
    project_image: UploadFile = File(...),
    leader_image: UploadFile = File(...),
):
    supabase = _supabase_client()
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase client not configured (check env vars)")

    db = SessionLocal()
    try:
        # upload images
        proj_filename = f"projects/{uuid.uuid4()}_{project_image.filename}"
        lead_filename = f"leaders/{uuid.uuid4()}_{leader_image.filename}"

        project_image_url = _upload_file_to_bucket(supabase, SUPABASE_BUCKET, project_image, proj_filename)
        leader_image_url = _upload_file_to_bucket(supabase, SUPABASE_BUCKET, leader_image, lead_filename)

        proj = Project(
            leader_id=leader_id,
            project_name=project_name,
            description=description,
            project_image=project_image_url,
            leader_image=leader_image_url,
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)

        return ProjectCreateResponse(
            id=proj.id,
            leader_id=proj.leader_id,
            project_name=proj.project_name,
            description=proj.description,
            project_image_url=proj.project_image,
            leader_image_url=proj.leader_image,
        )
    finally:
        db.close()


@router.get("/projects/leader/{leader_id}", response_model=list[ProjectCreateResponse])
def get_projects_by_leader(leader_id: str):
    db = SessionLocal()
    try:
        rows = db.query(Project).filter(Project.leader_id == leader_id).all()
        return [
            ProjectCreateResponse(
                id=r.id,
                leader_id=r.leader_id,
                project_name=r.project_name,
                description=r.description,
                project_image_url=r.project_image,
                leader_image_url=r.leader_image,
            )
            for r in rows
        ]
    finally:
        db.close()


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    """Delete a project and its images from Supabase storage (if configured)."""
    supabase = _supabase_client()
    db = SessionLocal()
    try:
        proj = db.get(Project, project_id)
        if not proj:
            raise HTTPException(status_code=404, detail="project not found")

        def _extract_path(url: str) -> str | None:
            if not url:
                return None
            prefix = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/"
            if url.startswith(prefix):
                return url[len(prefix):]
            return None

        proj_path = _extract_path(proj.project_image)
        lead_path = _extract_path(proj.leader_image)

        if supabase and SUPABASE_BUCKET:
            try:
                paths = [p for p in (proj_path, lead_path) if p]
                if paths:
                    supabase.storage.from_(SUPABASE_BUCKET).remove(paths)
            except Exception:
                # best-effort: ignore storage delete errors
                pass

        db.delete(proj)
        db.commit()
        return {"detail": "deleted"}
    finally:
        db.close()
