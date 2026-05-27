from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import logging
import time

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.database import connect_db, close_db
from app.cache import init_redis, close_redis
from app.limiter import limiter
from app.services.notification_service import init_firebase
from app.services.media_service import init_media_provider
from app.routes import auth, users, posts, chat, notifications
from app.routes import media as media_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LimitUploadSize(BaseHTTPMiddleware):
    def __init__(self, app, max_upload_size: int):
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next):
        if request.method == 'POST':
            if 'content-length' in request.headers:
                try:
                    content_length = int(request.headers['content-length'])
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={'detail': 'Invalid Content-Length header'}
                    )
                if content_length > self.max_upload_size:
                    return JSONResponse(
                        status_code=413,
                        content={'detail': 'Request body too large'}
                    )
        return await call_next(request)

TEST_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trandia — Auth Console</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060808;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.wrap{width:100%;max-width:460px}
.brand{text-align:center;margin-bottom:28px}
.brand h1{font-size:32px;font-weight:800;color:#00c853;letter-spacing:3px}
.brand p{color:#444;font-size:13px;margin-top:5px;letter-spacing:1px}
.card{background:#0f0f0f;border:1px solid #1a1a1a;border-radius:18px;padding:28px}
.tabs{display:flex;gap:3px;background:#080808;border-radius:10px;padding:4px;margin-bottom:24px}
.tab{flex:1;padding:9px;border:none;background:transparent;color:#555;
  cursor:pointer;border-radius:7px;font-size:13px;font-weight:500;transition:.2s}
.tab.active{background:#00c853;color:#000;font-weight:700}
.pane{display:none}.pane.active{display:block}
.hint{color:#444;font-size:13px;text-align:center;margin-bottom:20px;line-height:1.6}
label{display:block;font-size:11px;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:.6px}
input{width:100%;background:#080808;border:1px solid #1e1e1e;border-radius:8px;
  padding:11px 13px;color:#e0e0e0;font-size:14px;margin-bottom:14px;transition:.2s}
input:focus{outline:none;border-color:#00c853}
.btn{width:100%;padding:13px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s;margin-top:2px}
.btn-green{background:#00c853;color:#000}.btn-green:hover{background:#00e676}
.btn-outline{background:transparent;color:#00c853;border:1px solid #00c853;margin-top:8px}
.btn-outline:hover{background:#00c85322}
.btn-dark{background:#141414;color:#ddd;border:1px solid #272727;display:flex;align-items:center;justify-content:center;gap:9px}
.btn-dark:hover{background:#1c1c1c;border-color:#333}
.sep{display:flex;align-items:center;gap:10px;margin:18px 0;color:#2a2a2a;font-size:12px}
.sep::before,.sep::after{content:'';flex:1;height:1px;background:#1e1e1e}
.box{display:none;margin-top:18px;padding:15px;border-radius:10px;border:1px solid #00c853;background:#061206}
.box.err{border-color:#f44336;background:#120606}
.box-title{font-size:12px;font-weight:700;color:#00c853;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px}
.box.err .box-title{color:#f44336}
pre{font-size:11px;color:#666;white-space:pre-wrap;word-break:break-all;line-height:1.6}
.step{display:none}.step.active{display:block}
.otp-info{background:#0a1a0a;border:1px solid #1a3a1a;border-radius:8px;padding:12px;margin-bottom:14px;font-size:12px;color:#6a6;line-height:1.6}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><h1>TRANDIA</h1><p>BACKEND AUTH CONSOLE</p></div>
  <div class="card">
    <div class="tabs">
      <button class="tab active" onclick="tab('google',this)">Google</button>
      <button class="tab" onclick="tab('login',this)">Sign In</button>
      <button class="tab" onclick="tab('signup',this)">Sign Up</button>
    </div>

    <div id="pane-google" class="pane active">
      <p class="hint">Sign in with your Google account.</p>
      <a href="/auth/google/web" style="text-decoration:none">
        <button class="btn btn-dark" style="width:100%">Continue with Google</button>
      </a>
    </div>

    <div id="pane-login" class="pane">
      <label>Email</label><input type="email" id="l-email" placeholder="you@example.com">
      <label>Password</label><input type="password" id="l-pass" placeholder="••••••••">
      <button class="btn btn-green" onclick="doLogin()">Sign In</button>
    </div>

    <div id="pane-signup" class="pane">
      <!-- Step 1: Fill form -->
      <div id="s-step1" class="step active">
        <label>Full Name</label><input type="text" id="s-name" placeholder="Yogesh Kumar">
        <label>Username</label><input type="text" id="s-user" placeholder="yogesh_k">
        <label>Email</label><input type="email" id="s-email" placeholder="you@example.com">
        <label>Password</label><input type="password" id="s-pass" placeholder="min 6 characters">
        <button class="btn btn-green" onclick="doSignupInitiate()">Send Verification OTP</button>
      </div>
      <!-- Step 2: Enter OTP -->
      <div id="s-step2" class="step">
        <div class="otp-info" id="s-otp-info">OTP sent! Check your inbox.</div>
        <label>Enter 6-digit OTP</label>
        <input type="text" id="s-otp" placeholder="••••••" maxlength="6" style="font-size:22px;letter-spacing:8px;text-align:center">
        <button class="btn btn-green" onclick="doSignupVerify()">Verify & Create Account</button>
        <button class="btn btn-outline" onclick="doResendOtp()">Resend OTP</button>
      </div>
    </div>

    <div id="result" class="box">
      <div class="box-title" id="rtitle">Result</div>
      <pre id="rcontent"></pre>
    </div>
  </div>
</div>
<script>
let _signupEmail = '';

function tab(n,el){
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('pane-'+n).classList.add('active');
  el.classList.add('active');
}

function show(ok,d){
  const b=document.getElementById('result');
  b.style.display='block';
  b.className='box'+(ok?'':' err');
  document.getElementById('rtitle').textContent=ok?'Success':'Failed';
  document.getElementById('rcontent').textContent=typeof d==='string'?d:JSON.stringify(d,null,2);
}

function showStep(n){
  document.querySelectorAll('.step').forEach(s=>s.classList.remove('active'));
  document.getElementById('s-step'+n).classList.add('active');
}

async function doLogin(){
  try{
    const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('l-email').value,password:document.getElementById('l-pass').value})});
    show(r.ok,await r.json());
  }catch(e){show(false,e.message);}
}

async function doSignupInitiate(){
  try{
    const r=await fetch('/auth/signup/initiate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        name:document.getElementById('s-name').value,
        username:document.getElementById('s-user').value,
        email:document.getElementById('s-email').value,
        password:document.getElementById('s-pass').value
      })});
    const data=await r.json();
    if(r.ok){
      _signupEmail=data.email;
      document.getElementById('s-otp-info').textContent='OTP sent to '+data.email+'. Check your inbox (also check spam).';
      showStep(2);
      show(true,data);
    }else{show(false,data);}
  }catch(e){show(false,e.message);}
}

async function doSignupVerify(){
  try{
    const r=await fetch('/auth/signup/verify',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:_signupEmail,otp:document.getElementById('s-otp').value})});
    const data=await r.json();
    show(r.ok,data);
    if(r.ok){showStep(1);}
  }catch(e){show(false,e.message);}
}

async function doResendOtp(){
  try{
    const r=await fetch('/auth/signup/resend',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:_signupEmail})});
    const data=await r.json();
    show(r.ok,data);
  }catch(e){show(false,e.message);}
}

const p=new URLSearchParams(location.search);
if(p.get('token')){try{show(true,{access_token:p.get('token'),user:JSON.parse(decodeURIComponent(p.get('user')||'{}'))});}catch{show(true,{access_token:p.get('token')});}history.replaceState({},'','/');}
if(p.get('error')){show(false,{error:decodeURIComponent(p.get('error'))});history.replaceState({},'','/');}
</script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await connect_db()
    except Exception as e:
        print(f"[STARTUP] DB init error: {e}")
    try:
        init_firebase(settings.firebase_credentials_path)
    except Exception as e:
        print(f"[STARTUP] Firebase init error: {e}")
    try:
        if settings.cloudinary_cloud_name:
            init_media_provider(settings)
        else:
            print("[STARTUP] Cloudinary not configured — set CLOUDINARY_* vars in .env")
    except Exception as e:
        print(f"[STARTUP] Media provider init error: {e}")
    try:
        if settings.redis_url:
            await init_redis(settings.redis_url)
        else:
            print("[STARTUP] REDIS_URL not set — caching disabled (set in Railway env vars)")
    except Exception as e:
        print(f"[STARTUP] Redis init error: {e}")
    yield
    await close_redis()
    await close_db()


app = FastAPI(
    title="Trandia API",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ALLOWED_ORIGINS - Allow all origins for mobile app access
ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # Cannot use credentials with wildcard origins
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=3600,
)

app.add_middleware(LimitUploadSize, max_upload_size=105 * 1024 * 1024)  # 105 MB (videos up to 100 MB)
app.add_middleware(GZipMiddleware, minimum_size=1000)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    
    if duration > 1.0:  # Log slow requests
        logger.warning(
            f"SLOW_REQUEST {request.method} {request.url.path} "
            f"took {duration:.2f}s status={response.status_code}"
        )
    
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[ERROR] {request.method} {request.url.path} → {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(posts.router, prefix="/posts", tags=["Posts"])
app.include_router(chat.router, prefix="/chat", tags=["Chat"])
app.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
app.include_router(media_router.router, prefix="/media", tags=["Media"])


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    return TEST_PAGE


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "Trandia API", "version": "1.0.0"}


@app.get("/health/ready", tags=["Health"])
async def health_ready():
    """Readiness check - includes dependencies."""
    from app.database import get_db
    try:
        db = get_db()
        if db is not None:
            await db.command("ping")
            return {"status": "ready", "database": "connected"}
        else:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "error": str(e)})
