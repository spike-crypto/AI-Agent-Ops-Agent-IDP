from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_hr
from app.models import ROLES, User
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class UserIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)
    role: str = "interviewer"


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "is_active": u.is_active}


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower().strip()))
    # Same message whether the email or the password is wrong.
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    return {"access_token": create_access_token(user.id, user.role), "token_type": "bearer",
            "user": user_out(user)}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "A user with that email already exists")
    user = User(email=email, name=body.name, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    return user_out(user)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, db: Session = Depends(get_db), me_: User = Depends(require_hr)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == me_.id:
        raise HTTPException(400, "You cannot deactivate yourself")
    user.is_active = False
    db.commit()
    return user_out(user)
