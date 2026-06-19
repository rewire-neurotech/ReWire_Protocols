import os
from pathlib import Path
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg

try:
    from app.db import engine, SessionLocal, get_db
    from app.models import Base, PromoCode, PushSubscription
except Exception:
    engine = None
    Base = None
    SessionLocal = None
    PromoCode = None
    PushSubscription = None
    get_db = None


def _seed_promos():
    if SessionLocal is None or PromoCode is None:
        return
    codes = ["CHILLS50", "JOLTER", "FRIEND"]
    db = SessionLocal()
    try:
        for c in codes:
            if not db.query(PromoCode).filter(PromoCode.code == c).first():
                db.add(PromoCode(code=c, max_uses=0, is_active=True))
        db.commit()
    except Exception as e:
        print(f"[startup] promo seed error: {e}")
        db.rollback()
    finally:
        db.close()


app = FastAPI(
    title="ReWire",
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


os.makedirs(cfg.out_dir_path, exist_ok=True)
app.mount("/public", StaticFiles(directory=str(cfg.out_dir_path)), name="public")

assets_path = Path(__file__).parent / "assets"
if assets_path.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")

if Base is not None and engine is not None:
    try:
        Base.metadata.create_all(bind=engine)
        _seed_promos()
    except Exception as e:
        print(f"[startup] db error: {e}")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "rewire"}


from app.routes.auth import r as auth_r
app.include_router(auth_r)

from app.routes.auth import get_current_user_required as _require_user

from app.routes.goals import r as goals_r
app.include_router(goals_r)

from app.routes.jolt import r as jolt_r
app.include_router(jolt_r)

from app.routes.subscription import r as sub_r
app.include_router(sub_r)


_SW_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let data = {title: 'ReWire', body: 'Your Jolt is ready'};
  try { data = e.data.json(); } catch(_) {}
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/assets/logo.png',
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(cs => {
    for (const c of cs) { if (c.url.includes(self.location.origin)) return c.focus(); }
    return clients.openWindow('/');
  }));
});
""".strip()

@app.get("/sw.js")
def serve_sw():
    return Response(content=_SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


class PushSubReq(BaseModel):
    endpoint: str
    p256dh: str
    auth: str

@app.get("/api/push/vapid-key")
def get_vapid_key():
    return {"vapid_public_key": cfg.VAPID_PUBLIC_KEY or ""}

@app.post("/api/push/subscribe")
def push_subscribe(req: PushSubReq, db: Session = Depends(get_db), user=Depends(_require_user)):
    existing = db.query(PushSubscription).filter(
        PushSubscription.user_id == user.id,
        PushSubscription.endpoint == req.endpoint,
    ).first()
    if existing:
        existing.p256dh = req.p256dh
        existing.auth = req.auth
    else:
        db.add(PushSubscription(
            user_id=user.id, endpoint=req.endpoint,
            p256dh=req.p256dh, auth=req.auth,
        ))
    db.commit()
    return {"status": "ok"}

@app.post("/api/push/unsubscribe")
def push_unsubscribe(req: PushSubReq, db: Session = Depends(get_db), user=Depends(_require_user)):
    sub = db.query(PushSubscription).filter(
        PushSubscription.user_id == user.id,
        PushSubscription.endpoint == req.endpoint,
    ).first()
    if sub:
        db.delete(sub)
        db.commit()
    return {"status": "ok"}


FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

@app.get("/")
def serve_frontend():
    if FRONTEND.exists():
        return FileResponse(str(FRONTEND), media_type="text/html")
    return {"error": "frontend not found"}