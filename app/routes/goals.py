from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Goal, Challenge, Tip, Jolt, Reflection, User
from app.routes.auth import get_current_user_required
from app.utils.encryption import encrypt_field, decrypt_field
from app.services.llm import clean_goal_title

r = APIRouter(prefix="/api/goals", tags=["goals"])


class ChallengeOut(BaseModel):
    id: int
    text: str
    created_at: str

class TipOut(BaseModel):
    id: int
    text: str
    from_name: str
    created_at: str

class GoalOut(BaseModel):
    id: int
    title: str
    display_title: Optional[str] = None
    description: str
    category: str
    place: str
    reflection_question: str
    done_at: Optional[str] = None
    created_at: str
    updated_at: str
    challenges: List[ChallengeOut]
    tips: List[TipOut]

class GoalsList(BaseModel):
    goals: List[GoalOut]
    total: int

class Ok(BaseModel):
    status: str

class CreateGoalReq(BaseModel):
    title: str
    description: str = ""
    category: str = ""
    place: str = ""

class UpdateGoalReq(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None

class TextReq(BaseModel):
    text: str

class TipReq(BaseModel):
    text: str
    from_name: str = "You"


def _ts(dt):
    return dt.isoformat() if dt else ""

def _goal_out(g, db):
    cs = db.query(Challenge).filter(Challenge.goal_id == g.id).order_by(Challenge.created_at).all()
    ts = db.query(Tip).filter(Tip.goal_id == g.id).order_by(Tip.created_at).all()
    return GoalOut(
        id=g.id, title=g.title,
        display_title=g.display_title or g.title,
        description=decrypt_field(g.description) or "",
        category=g.category or "", place=g.place or "",
        reflection_question=g.reflection_question,
        done_at=_ts(g.done_at) or None,
        created_at=_ts(g.created_at), updated_at=_ts(g.updated_at),
        challenges=[ChallengeOut(id=c.id, text=decrypt_field(c.text) or "", created_at=_ts(c.created_at)) for c in cs],
        tips=[TipOut(id=t.id, text=decrypt_field(t.text) or "", from_name=t.from_name or "You", created_at=_ts(t.created_at)) for t in ts],
    )

def _own(gid, u, db):
    g = db.query(Goal).filter(Goal.id == gid, Goal.user_id == u.id).first()
    if not g: raise HTTPException(404, "goal not found")
    return g


@r.get("", response_model=GoalsList)
def list_goals(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    gs = (db.query(Goal).filter(Goal.user_id == u.id, Goal.done_at.is_(None))
          .order_by(Goal.created_at.desc()).all())
    return GoalsList(goals=[_goal_out(g, db) for g in gs], total=len(gs))


@r.get("/done", response_model=GoalsList)
def list_done(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    gs = (db.query(Goal).filter(Goal.user_id == u.id, Goal.done_at.isnot(None))
          .order_by(Goal.done_at.desc()).all())
    return GoalsList(goals=[_goal_out(g, db) for g in gs], total=len(gs))


@r.get("/export")
def export_data(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    gs = db.query(Goal).filter(Goal.user_id == u.id).order_by(Goal.created_at.desc()).all()
    out = []
    for g in gs:
        cs = db.query(Challenge).filter(Challenge.goal_id == g.id).all()
        ts = db.query(Tip).filter(Tip.goal_id == g.id).all()
        js = db.query(Jolt).filter(Jolt.goal_id == g.id).all()
        rs = db.query(Reflection).filter(Reflection.goal_id == g.id).all()
        out.append({
            "title": g.title,
            "description": decrypt_field(g.description) or "",
            "category": g.category or "",
            "place": g.place or "",
            "done_at": _ts(g.done_at) or None,
            "created_at": _ts(g.created_at),
            "challenges": [{"text": decrypt_field(c.text) or ""} for c in cs],
            "tips": [{"text": decrypt_field(t.text) or "", "from": t.from_name or ""} for t in ts],
            "jolts": [{"track": j.track_name, "format": j.speech_format, "created_at": _ts(j.created_at)} for j in js],
            "reflections": [{"question": r.question or "", "answer": decrypt_field(r.answer) or "", "created_at": _ts(r.created_at)} for r in rs],
        })
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "goals": out}


@r.post("", response_model=GoalOut)
def create_goal(req: CreateGoalReq, u: User = Depends(get_current_user_required),
                db: Session = Depends(get_db)):
    if not req.title or not req.title.strip():
        raise HTTPException(400, "title required")
    g = Goal(
        user_id=u.id,
        title=req.title.strip()[:200],
        display_title=clean_goal_title(req.title.strip()),
        description=encrypt_field(req.description.strip()) if req.description else None,
        category=req.category.strip() or None,
        place=req.place.strip() or None,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return _goal_out(g, db)


@r.get("/{gid}", response_model=GoalOut)
def get_goal(gid: int, u: User = Depends(get_current_user_required),
             db: Session = Depends(get_db)):
    return _goal_out(_own(gid, u, db), db)


@r.put("/{gid}", response_model=GoalOut)
def update_goal(gid: int, req: UpdateGoalReq, u: User = Depends(get_current_user_required),
                db: Session = Depends(get_db)):
    g = _own(gid, u, db)
    if req.title is not None:
        g.title = req.title.strip()[:200]
        g.display_title = clean_goal_title(req.title.strip())
    if req.description is not None:
        g.description = encrypt_field(req.description.strip()) if req.description.strip() else None
    db.commit()
    db.refresh(g)
    return _goal_out(g, db)


@r.delete("/{gid}", response_model=Ok)
def delete_goal(gid: int, u: User = Depends(get_current_user_required),
                db: Session = Depends(get_db)):
    g = _own(gid, u, db)
    db.query(Reflection).filter(Reflection.goal_id == g.id).delete()
    db.query(Jolt).filter(Jolt.goal_id == g.id).delete()
    db.query(Tip).filter(Tip.goal_id == g.id).delete()
    db.query(Challenge).filter(Challenge.goal_id == g.id).delete()
    db.delete(g)
    db.commit()
    return Ok(status="ok")


@r.put("/{gid}/done", response_model=GoalOut)
def toggle_done(gid: int, u: User = Depends(get_current_user_required),
                db: Session = Depends(get_db)):
    g = _own(gid, u, db)
    g.done_at = None if g.done_at else datetime.now(timezone.utc)
    db.commit()
    db.refresh(g)
    return _goal_out(g, db)


@r.post("/{gid}/challenges", response_model=GoalOut)
def add_challenge(gid: int, req: TextReq, u: User = Depends(get_current_user_required),
                  db: Session = Depends(get_db)):
    g = _own(gid, u, db)
    if not req.text.strip(): raise HTTPException(400, "text required")
    db.add(Challenge(goal_id=g.id, text=encrypt_field(req.text.strip())))
    db.commit()
    return _goal_out(g, db)


@r.put("/{gid}/challenges/{cid}", response_model=GoalOut)
def edit_challenge(gid: int, cid: int, req: TextReq,
                   u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    _own(gid, u, db)
    c = db.query(Challenge).filter(Challenge.id == cid, Challenge.goal_id == gid).first()
    if not c: raise HTTPException(404, "challenge not found")
    c.text = encrypt_field(req.text.strip())
    db.commit()
    return _goal_out(_own(gid, u, db), db)


@r.delete("/{gid}/challenges/{cid}", response_model=Ok)
def del_challenge(gid: int, cid: int, u: User = Depends(get_current_user_required),
                  db: Session = Depends(get_db)):
    _own(gid, u, db)
    c = db.query(Challenge).filter(Challenge.id == cid, Challenge.goal_id == gid).first()
    if not c: raise HTTPException(404, "challenge not found")
    db.delete(c)
    db.commit()
    return Ok(status="ok")


@r.post("/{gid}/tips", response_model=GoalOut)
def add_tip(gid: int, req: TipReq, u: User = Depends(get_current_user_required),
            db: Session = Depends(get_db)):
    g = _own(gid, u, db)
    if not req.text.strip(): raise HTTPException(400, "text required")
    db.add(Tip(goal_id=g.id, text=encrypt_field(req.text.strip()), from_name=req.from_name.strip() or "You"))
    db.commit()
    return _goal_out(g, db)


@r.put("/{gid}/tips/{tid}", response_model=GoalOut)
def edit_tip(gid: int, tid: int, req: TextReq,
             u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    _own(gid, u, db)
    t = db.query(Tip).filter(Tip.id == tid, Tip.goal_id == gid).first()
    if not t: raise HTTPException(404, "tip not found")
    t.text = encrypt_field(req.text.strip())
    db.commit()
    return _goal_out(_own(gid, u, db), db)


@r.delete("/{gid}/tips/{tid}", response_model=Ok)
def del_tip(gid: int, tid: int, u: User = Depends(get_current_user_required),
            db: Session = Depends(get_db)):
    _own(gid, u, db)
    t = db.query(Tip).filter(Tip.id == tid, Tip.goal_id == gid).first()
    if not t: raise HTTPException(404, "tip not found")
    db.delete(t)
    db.commit()
    return Ok(status="ok")
