import json
import os
from pathlib import Path
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

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
    version="5.0.0",
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
        # migrate: add display_title column if missing
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE goals ADD COLUMN display_title VARCHAR(200)"
                ))
                conn.commit()
                print("[migrate] added display_title column to goals")
        except Exception:
            pass  # column already exists
        # migrate: add stripe_customer_id to users
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR(200)"
                ))
                conn.commit()
                print("[migrate] added stripe_customer_id column to users")
        except Exception:
            pass  # column already exists
        # migrate: add stripe_session_id to subscriptions
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE subscriptions ADD COLUMN stripe_session_id VARCHAR(200)"
                ))
                conn.commit()
                print("[migrate] added stripe_session_id column to subscriptions")
        except Exception:
            pass  # column already exists
        # migrate: add canceling to entitlements
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE entitlements ADD COLUMN canceling BOOLEAN DEFAULT 0"
                ))
                conn.commit()
                print("[migrate] added canceling column to entitlements")
        except Exception:
            pass  # column already exists
        # migrate: add chills to journal_entries
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE journal_entries ADD COLUMN chills VARCHAR(10)"
                ))
                conn.commit()
                print("[migrate] added chills column to journal_entries")
        except Exception:
            pass  # column already exists
    except Exception as e:
        print(f"[startup] db error: {e}")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "rewire"}


from app.routes.auth import r as auth_r
app.include_router(auth_r)

from app.routes.auth import get_current_user_required as _require_user

# --- v4 routers (kept mounted; retired at final cleanup once v5 is fully cut over) ---
from app.routes.goals import r as goals_r
app.include_router(goals_r)

from app.routes.jolt import r as jolt_r
app.include_router(jolt_r)

from app.routes.subscription import r as sub_r
app.include_router(sub_r)

# --- v5 routers ---
from app.routes.protocols import r as protocols_r
app.include_router(protocols_r)

from app.routes.protocol_jolt import r as protocol_jolt_r
app.include_router(protocol_jolt_r)

# Recover any jolts orphaned by the previous shutdown (restart, deploy,
# OOM, /tmp-limit kill): mark stuck rows as error so clients fail fast
# instead of polling a dead jolt, and sweep leftover scratch directories.
# Idempotent, so it is safe under multiple uvicorn workers.
try:
    from app.routes.jolt import recover_orphaned_jolts
    recover_orphaned_jolts()
except Exception as e:
    print(f"[startup] orphan recovery error: {e}")

# Same recovery for v5 protocol jolts.
try:
    from app.tasks import recover_orphaned_protocol_jolts
    recover_orphaned_protocol_jolts()
except Exception as e:
    print(f"[startup] protocol orphan recovery error: {e}")


_SW_JS = """
var CACHE_NAME = 'rewire-v5-v1';
var APP_SHELL = ['/', '/assets/logo.png'];

self.addEventListener('install', function(e) {
  e.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(APP_SHELL);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE_NAME; })
            .map(function(k) { return caches.delete(k); })
      );
    }).then(function() { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function(e) {
  if (e.request.method !== 'GET') return;
  var url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname === '/sw.js' || url.pathname === '/manifest.json') return;
  e.respondWith(
    fetch(e.request).then(function(resp) {
      if (resp.ok) {
        var clone = resp.clone();
        caches.open(CACHE_NAME).then(function(cache) { cache.put(e.request, clone); });
      }
      return resp;
    }).catch(function() {
      return caches.match(e.request).then(function(r) { return r || caches.match('/'); });
    })
  );
});

self.addEventListener('push', function(e) {
  var data = {title: 'ReWire', body: 'Your jolt is ready'};
  try { data = e.data.json(); } catch(err) {}
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/assets/logo.png',
    badge: '/assets/logo.png',
    vibrate: [100, 50, 100],
    data: {url: '/'}
  }));
});

self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(function(cs) {
      for (var i = 0; i < cs.length; i++) {
        if (cs[i].url.indexOf(self.location.origin) !== -1) return cs[i].focus();
      }
      return clients.openWindow('/');
    })
  );
});

self.addEventListener('pushsubscriptionchange', function(e) {
  /* The browser rotated or expired the push subscription. Re-subscribe
     and tell the server to swap the old endpoint for the new one.
     This uses /api/push/renew (keyed by the old endpoint) because the
     service worker has no access to the user's JWT. */
  var oldSub = e.oldSubscription;
  var oldEndpoint = oldSub ? oldSub.endpoint : '';
  var opts = {userVisibleOnly: true};
  if (oldSub && oldSub.options && oldSub.options.applicationServerKey) {
    opts.applicationServerKey = oldSub.options.applicationServerKey;
  }
  e.waitUntil(
    self.registration.pushManager.subscribe(opts).then(function(sub) {
      var j = sub.toJSON();
      return fetch('/api/push/renew', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          old_endpoint: oldEndpoint,
          endpoint: j.endpoint,
          p256dh: j.keys.p256dh,
          auth: j.keys.auth
        })
      });
    }).catch(function(err) {
      console.log('[SW] pushsubscriptionchange renewal failed:', err);
    })
  );
});
""".strip()

@app.get("/sw.js")
def serve_sw():
    return Response(content=_SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
def serve_manifest():
    manifest = {
        "name": "ReWire",
        "short_name": "ReWire",
        "description": "Behavioral activation powered by music and AI",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#fbf9f4",
        "theme_color": "#1c1a16",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/assets/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/assets/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    return Response(
        content=json.dumps(manifest),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"}
    )


class PushSubReq(BaseModel):
    endpoint: str
    p256dh: str
    auth: str

class PushRenewReq(BaseModel):
    old_endpoint: str
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

@app.post("/api/push/renew")
def push_renew(req: PushRenewReq, db: Session = Depends(get_db)):
    """
    Called by the service worker on pushsubscriptionchange, which runs in
    the background without access to the user's JWT. The old endpoint is
    a long, unguessable URL known only to the browser and our DB, so
    possession of it authorizes updating that one row in place.
    """
    old = (req.old_endpoint or "").strip()
    if not old:
        raise HTTPException(400, "old_endpoint required")

    sub = db.query(PushSubscription).filter(
        PushSubscription.endpoint == old,
    ).first()
    if not sub:
        # Old endpoint unknown (already cleaned up or never registered).
        # Nothing to renew without a user context.
        raise HTTPException(404, "subscription not found")

    sub.endpoint = req.endpoint
    sub.p256dh = req.p256dh
    sub.auth = req.auth
    db.commit()
    print(f"[push] renewed subscription {sub.id} for user {sub.user_id}")
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


# >>> TEMPORARY ENDPOINT — DELETE AFTER DOWNLOADING PRIMER SAMPLES <<<
@app.get("/api/admin/mix-primer")
def mix_primer(offset: float = 0.0, duck: float = -4.0, voice: float = -12.5):
    """One-shot: mix 11labs.mp3 over music_primer.mp3 with tunable params.
    Usage: /api/admin/mix-primer?offset=-0.5&duck=-1.0&voice=-11.5
      offset: added to base music_target_dbfs of -24.0  (negative = quieter music)
      duck:   max_duck_db, how much music dips under voice (-4.0 = heavy, 0.0 = none)
      voice:  voice_target_dbfs (-12.5 = default, -11.0 = louder)
    """
    import tempfile
    from app.services.mix import mix as do_mix

    voice_path = cfg.ASSETS_DIR / "11labs.mp3"
    music_path = cfg.ASSETS_DIR / "music_primer.mp3"
    if not voice_path.exists() or not music_path.exists():
        raise HTTPException(404, "primer source files not found in assets/")

    music_db = -24.0 + offset

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out_path = tmp.name

    try:
        do_mix(
            voice_path=str(voice_path),
            music_path=str(music_path),
            out_path=out_path,
            content_duration_sec=156,
            voice_target_dbfs=voice,
            music_target_dbfs=music_db,
            duck_db=5.0,
            duck_max_db=duck,
            duck_floor_db=0.0,
            ffmpeg_bin=cfg.FFMPEG_BIN,
        )
        data = Path(out_path).read_bytes()
        fname = f"primer_off{offset:+.1f}_duck{duck:.1f}_voice{voice:.1f}.mp3"
        return Response(
            content=data,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f"attachment; filename={fname}",
                "Content-Length": str(len(data)),
            },
        )
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
# >>> END TEMPORARY ENDPOINT <<<


@app.get("/")
def serve_frontend():
    if FRONTEND.exists():
        return FileResponse(str(FRONTEND), media_type="text/html")
    return {"error": "frontend not found"}
