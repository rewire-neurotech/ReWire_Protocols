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
    from app.models import Base, PromoCode, PushSubscription, User
except Exception:
    engine = None
    Base = None
    SessionLocal = None
    PromoCode = None
    PushSubscription = None
    User = None
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


def _promote_admins():
    """ADMIN CONSOLE: grant is_admin to every account listed in ADMIN_EMAILS.

    This is how the first admin comes to exist at all: there is no shell on
    Render and no UI for setting the flag, so it is driven by an env var.
    routes/auth.py runs the same check at sign-in, which covers an account
    created after this process booted.

    Only ever GRANTS. Removing an email from ADMIN_EMAILS does not revoke the
    flag, so a decision made deliberately in the database is never undone by an
    env var somebody forgot to update. Revoke by setting is_admin = false
    directly.

    MUST run after the ALTER TABLE migrations below, because querying User
    selects every column on the model -- including last_active_at, which does
    not exist until that migration has run.
    """
    if SessionLocal is None or User is None:
        return
    emails = cfg.admin_emails
    if not emails:
        return
    db = SessionLocal()
    try:
        promoted = 0
        for e in emails:
            u = db.query(User).filter(User.email == e).first()
            if u is None:
                print(f"[startup] ADMIN_EMAILS: no account yet for {e} "
                      f"(will be promoted when it signs in)")
                continue
            if not u.is_admin:
                u.is_admin = True
                promoted += 1
        if promoted:
            db.commit()
            print(f"[startup] promoted {promoted} admin account(s) from ADMIN_EMAILS")
    except Exception as e:
        print(f"[startup] admin promotion error: {e}")
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

        # ------------------------------------------------------------------ #
        # ADMIN CONSOLE MIGRATION
        # ------------------------------------------------------------------ #
        # migrate: add last_active_at to users
        #
        # THIS ONE IS NOT OPTIONAL. Every other migration above adds a column
        # that only one feature reads, so a database missing it merely loses
        # that feature. last_active_at is different: it is declared on the User
        # model, so SQLAlchemy puts it in EVERY select and insert against
        # `users`. On a database where this ALTER has not run, login,
        # registration and every authenticated request fail outright.
        #
        # create_all() cannot do this -- it only creates missing TABLES, never
        # adds columns to existing ones. TIMESTAMP is accepted by both Postgres
        # and SQLite. Wrapped like its neighbours so a redeploy against an
        # already-migrated database is a silent no-op.
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN last_active_at TIMESTAMP"
                ))
                conn.commit()
                print("[migrate] added last_active_at column to users")
        except Exception:
            pass  # column already exists

        # Runs LAST, after the migration above: querying User selects every
        # column on the model, so promotion would fail on a database that has
        # not been migrated yet.
        _promote_admins()
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

# --- admin console router (Phase 2) ---
# Guarded on purpose, matching the defensive import style at the top of this
# file. Phase 1 (data capture) can deploy before app/routes/admin.py exists:
# the app boots normally and simply has no /api/admin/* endpoints. Once that
# file lands, this picks it up with no further change here.
try:
    from app.routes.admin import r as admin_r
    app.include_router(admin_r)
    print("[startup] admin console router mounted")
except ImportError:
    print("[startup] admin console router not present yet (Phase 2 pending)")
except Exception as e:
    print(f"[startup] admin router failed to mount: {e}")

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
var CACHE_NAME = 'rewire-v5-v2';
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
  /* Never cache the internal console. It is a separate document from the PWA
     shell, it is only ever opened on a desktop browser, and a cached copy would
     keep serving stale HTML after a deploy. */
  if (url.pathname === '/admin' || url.pathname.startsWith('/admin/')) return;
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
ADMIN_FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "admin.html"

@app.get("/")
def serve_frontend():
    if FRONTEND.exists():
        return FileResponse(str(FRONTEND), media_type="text/html")
    return {"error": "frontend not found"}


@app.get("/admin")
def serve_admin():
    """The internal ops console.

    Static HTML only -- it holds no secrets and gates nothing by itself. Access
    control lives entirely on the /api/admin/* endpoints, which require a valid
    JWT belonging to an account with is_admin set. Serving the shell to an
    anonymous browser reveals nothing: every panel is empty until an admin
    signs in, exactly like the app's own index.html.

    no-store because this is a desktop page behind a login, never a PWA shell,
    and a cached copy after a deploy is worse than a round trip.
    """
    if ADMIN_FRONTEND.exists():
        return FileResponse(
            str(ADMIN_FRONTEND),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )
    return {"error": "admin console not found"}
# --------------------------------------------------------------------------- #
# TEMP: v4.5 test jolt generation
# --------------------------------------------------------------------------- #
# Usage:
#   /api/admin/generate-v45?case=1             -> full pipeline, returns MP3
#   /api/admin/generate-v45?case=1&text_only=1 -> speech text only (no TTS/mix)
#
# After downloading all 5 test jolts, remove everything below this comment
# and redeploy.

_V45_CASES = {
    1: {
        "category": "critic",
        "when_it_shows_up": "After a mistake",
        "pattern": "i talk to myself in a way i would never talk to another person. if i get something wrong at work, even something small that nobody else notices, i replay it for days, and the voice doing the replaying is genuinely cruel about it, like the mistake is evidence of something bigger and older about me that i've been hiding successfully until now.",
        "last_noticed": "friday. i sent a deck to a client with the wrong quarter in the title. someone replied \"i think this is q2?\" and that was it, completely fine to them. i sat in the car in the parking garage for twenty minutes before i could drive home.",
        "who_would_notice": "my husband. he's the one who has to hear it out loud, or watch me go quiet instead, which i think is worse for him.",
    },
    2: {
        "category": "leave",
        "when_it_shows_up": "When someone goes quiet",
        "pattern": "i always assume people are about to leave. things go well for a bit and then i start looking for the sign that they're pulling away, and usually i find it somewhere.",
        "last_noticed": "last night. she took longer than usual to reply and i had a whole story built by the time she did.",
        "who_would_notice": "nobody. that's sort of the point.",
    },
    3: {
        "category": "perfect",
        "when_it_shows_up": "When it's almost done",
        "pattern": "Nothing counts unless it's excellent, and nothing I make is ever excellent, so nothing I make ever really counts. I've been revising the same manuscript for three years. It's not that I think it's bad, it's that I can always see the version of it that would be better, and shipping the current one feels like agreeing to be seen as someone who couldn't get there. It's the same with the house, with cooking, with anything I'd have to show someone.",
        "last_noticed": "Sunday night. I had the manuscript open for four hours and the only thing I did was rewrite the opening paragraph six times, then closed the laptop and told myself I'd been working.",
        "who_would_notice": "My co-author. He's been waiting on me since February and he's stopped asking about it, which is worse than when he was asking.",
    },
    4: {
        "category": "numb",
        "when_it_shows_up": "I mostly notice afterwards",
        "pattern": "when things get heavy i go flat. my mum has been ill since january and everyone keeps telling me how well i'm handling it, and i'm not handling it, i'm just not there for any of it. i can feel myself switch off in the room and then i'm watching from somewhere behind my own eyes.",
        "last_noticed": "wednesday at the hospital. the consultant was going through the scan results and i caught myself working out whether i'd left the car in the right bay. i wasn't in the room at all.",
        "who_would_notice": "my sister. she does the feeling for both of us and i think she's exhausted.",
    },
    5: {
        "category": "accept",
        "when_it_shows_up": "",
        "pattern": "i want to get to a place where i'm okay with how i am without having to fix something first. there's always a condition attached, once i've got a proper job, once i'm further along than i am now, and then as soon as i get there the condition just moves somewhere else.",
        "last_noticed": "this morning getting dressed. i changed four times and made myself late, and none of it was actually about the clothes.",
        "who_would_notice": "my best friend. she's been telling me this for years and i keep proving her wrong on purpose.",
    },
}


@app.get("/api/admin/generate-v45")
def generate_v45(case: int = 1, text_only: bool = False):
    """Generate a v4.5 test jolt from onboarding test cases."""
    import re
    import time
    import anthropic
    from fastapi.responses import FileResponse
    from app.services.prompt_v45 import SYSTEM_PROMPT, build_user_prompt
    from app.core.config import cfg as _cfg

    if case not in _V45_CASES:
        raise HTTPException(400, f"case must be 1-5, got {case}")

    tc = _V45_CASES[case]
    t0 = time.time()

    # 1. Build prompt — 111s track @ 1.8 wps = 200 spoken words
    user_prompt = build_user_prompt(
        pattern=tc["pattern"],
        when_it_shows_up=tc.get("when_it_shows_up", ""),
        last_noticed=tc.get("last_noticed", ""),
        who_would_notice=tc.get("who_would_notice", ""),
        category=tc["category"],
        target_words=200,
    )

    # 2. Call Claude
    client = anthropic.Anthropic(api_key=_cfg.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=_cfg.CLAUDE_MODEL,
        max_tokens=2000,
        temperature=1.0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    speech = msg.content[0].text.strip()

    # Count spoken words (exclude tags and section breaks)
    spoken = re.sub(r"\[[^\]]*\]", "", speech).replace("---", " ")
    wc = len(spoken.split())
    print(f"[v45] case {case} ({tc['category']}): {wc} spoken words, {time.time() - t0:.1f}s")
    print(f"[v45] speech:\n{speech}")

    if "REWIRE_UNSAFE" in speech:
        return {"status": "unsafe", "case": case, "speech": speech}

    if text_only:
        return {"status": "ok", "case": case, "category": tc["category"],
                "spoken_words": wc, "speech": speech}

    # 3. TTS
    from app.services.tts import synth

    voice_id = _cfg.PROTOCOL_VOICE["activate"]["voice_id"]
    voice_settings = _cfg.PROTOCOL_VOICE["activate"]["voice_settings"]
    wav_path = synth(speech, voice_id=voice_id, voice_settings=voice_settings)
    print(f"[v45] case {case}: TTS done {time.time() - t0:.1f}s")

    # 4. Mix
    from app.services.mix_v45 import mix as mix_v45

    music_path = Path(__file__).resolve().parent / "assets" / "sanctus.mpeg"
    if not music_path.exists():
        raise HTTPException(500, "sanctus.mpeg not found in app/assets/")

    out_path = _cfg.out_dir_path / f"v45_case_{case}.mp3"
    mix_v45(
        voice_path=wav_path,
        music_path=str(music_path),
        out_path=str(out_path),
        mix_profile="jolt1",
    )
    print(f"[v45] case {case}: mix done {time.time() - t0:.1f}s")

    # 5. Cleanup voice temp file
    try:
        import os
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)
    except Exception:
        pass

    return FileResponse(str(out_path), media_type="audio/mpeg",
                        filename=f"v45_case_{case}_{tc['category']}.mp3")
