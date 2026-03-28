"""
SBK Health — FastAPI Backend v3
- Serves the frontend HTML directly from a route (no static folder needed)
- Auto-creates all database tables on startup (no shell/manual setup)
- AI summaries via Claude API (server-side, secure)
- Box integration for file uploads

Render.com deployment:
  Build command: pip install -r requirements.txt
  Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os, io, json, logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (FastAPI, Depends, HTTPException,
                     UploadFile, File, Form, BackgroundTasks, Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
import asyncpg

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sbkhealth")

# ── App setup ──────────────────────────────────────────────────────────────────
app = FastAPI(title="SBK Health API", version="3.0.0", docs_url="/api/docs")

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"])

# ── Config from environment variables ─────────────────────────────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "change-this-to-40-random-chars-minimum")
DATABASE_URL  = os.getenv("DATABASE_URL", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BOX_CONFIG    = "box_config.json"

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2  = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# ── Database auto-setup ────────────────────────────────────────────────────────
# Runs on every startup. IF NOT EXISTS means it never overwrites existing data.
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username      VARCHAR(50)  UNIQUE NOT NULL,
    email         VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(20)  NOT NULL CHECK (role IN ('admin','viewer')),
    full_name     VARCHAR(100),
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    last_login    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS patients (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name               VARCHAR(150) NOT NULL,
    dob                     VARCHAR(30),
    gender                  VARCHAR(20),
    blood_group             VARCHAR(5),
    status                  VARCHAR(80),
    primary_doctor          VARCHAR(100),
    box_folder_id           VARCHAR(50),
    box_lab_folder_id       VARCHAR(50),
    box_imaging_folder_id   VARCHAR(50),
    box_rx_folder_id        VARCHAR(50),
    box_hospital_folder_id  VARCHAR(50),
    ai_summary              JSONB,
    ai_summary_updated_at   TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_patient_access (
    user_id    UUID REFERENCES users(id)    ON DELETE CASCADE,
    patient_id UUID REFERENCES patients(id) ON DELETE CASCADE,
    granted_at TIMESTAMPTZ DEFAULT NOW(),
    granted_by UUID REFERENCES users(id),
    PRIMARY KEY (user_id, patient_id)
);

CREATE TABLE IF NOT EXISTS conditions (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id         UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    name               VARCHAR(250) NOT NULL,
    severity           VARCHAR(20) NOT NULL CHECK (severity IN
                       ('urgent','high','moderate','monitor','normal','resolved')),
    organ              VARCHAR(100),
    organ_id           VARCHAR(50),
    findings           TEXT,
    recommended_action TEXT,
    is_active          BOOLEAN DEFAULT TRUE,
    last_reviewed      VARCHAR(30),
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS lab_results (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id  UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    test_date   VARCHAR(20)  NOT NULL,
    marker      VARCHAR(100) NOT NULL,
    value       NUMERIC(12,4),
    unit        VARCHAR(30),
    ref_low     NUMERIC(12,4),
    ref_high    NUMERIC(12,4),
    is_abnormal BOOLEAN,
    lab_name    VARCHAR(100),
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    box_file_id     VARCHAR(60),
    file_name       VARCHAR(255) NOT NULL,
    report_date     VARCHAR(20),
    doc_type        VARCHAR(50) NOT NULL,
    doctor_name     VARCHAR(120),
    condition_link  VARCHAR(250),
    tags            TEXT[],
    extracted_text  TEXT,
    file_size_bytes BIGINT,
    box_link        VARCHAR(500),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS timeline_events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id  UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    event_date  VARCHAR(20) NOT NULL,
    event_type  VARCHAR(30) NOT NULL,
    title       VARCHAR(220) NOT NULL,
    detail      TEXT,
    specialty   VARCHAR(100),
    color_hex   VARCHAR(7) DEFAULT '#2563EB',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS symptoms (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id   UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    symptom      VARCHAR(300) NOT NULL,
    symptom_date VARCHAR(30),
    severity     VARCHAR(20) CHECK (severity IN ('mild','moderate','severe')),
    status       VARCHAR(20) DEFAULT 'ongoing',
    notes        TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id),
    action      VARCHAR(50),
    resource    VARCHAR(50),
    resource_id UUID,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Seed default accounts (only if they don't already exist)
-- Passwords: admin/admin123  viewer/viewer123
INSERT INTO users (username, email, password_hash, role, full_name) VALUES
  ('admin',  'admin@sbkhealth.local',
   '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW',
   'admin', 'Dr. Dinesh V.'),
  ('viewer', 'viewer@sbkhealth.local',
   '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW',
   'viewer', 'Family Member')
ON CONFLICT (username) DO NOTHING;
"""

@app.on_event("startup")
async def startup():
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — running without database")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(SCHEMA)
        await conn.close()
        log.info("✅ Database ready")
    except Exception as e:
        log.error(f"❌ DB startup error: {e}")

# ── Helpers ────────────────────────────────────────────────────────────────────
async def db():
    if not DATABASE_URL:
        raise HTTPException(503, "Database not configured")
    return await asyncpg.connect(DATABASE_URL)

def make_token(data: dict) -> str:
    return jwt.encode(
        {**data, "exp": datetime.utcnow() + timedelta(hours=24)},
        SECRET_KEY, algorithm="HS256")

async def get_user(token: str = Depends(oauth2)):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Invalid token")

def admin_only(user=Depends(get_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return user

# ── Schemas ────────────────────────────────────────────────────────────────────
class PatientIn(BaseModel):
    full_name: str; dob: str; gender: str
    blood_group: Optional[str] = None
    primary_doctor: Optional[str] = None
    status: Optional[str] = None

class ConditionIn(BaseModel):
    name: str; severity: str; organ: str; organ_id: str
    findings: str; recommended_action: str
    last_reviewed: Optional[str] = None

class LabIn(BaseModel):
    test_date: str; marker: str; value: float; unit: str
    ref_low: Optional[float] = None; ref_high: Optional[float] = None
    lab_name: Optional[str] = None

class UserIn(BaseModel):
    username: str; email: str; password: str; role: str; full_name: str

class SymptomIn(BaseModel):
    symptom: str; symptom_date: str; severity: str
    status: Optional[str] = "ongoing"
    notes: Optional[str] = None

# ── Frontend — serves index.html at the root URL ───────────────────────────────
# Reads from 'static/index.html' if it exists, otherwise returns a helpful page.
# This is what people see when they visit your Render URL.

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = os.path.join("static", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    # Fallback: guide page so you know the server is running even without the HTML
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><title>SBK Health</title>
<style>body{font-family:sans-serif;max-width:600px;margin:80px auto;padding:20px;background:#F8FAFC;color:#1E293B}
.card{background:#fff;border:1px solid #E2E8F0;border-radius:10px;padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{color:#1648A0}code{background:#F1F5F9;padding:2px 6px;border-radius:4px;font-size:13px}
.ok{color:#16A34A;font-weight:600}.warn{color:#D97706;font-weight:600}</style></head>
<body><div class="card">
<h1>⚕ SBK Health API</h1>
<p class="ok">✅ Server is running successfully!</p>
<p class="warn">⚠ Frontend not found</p>
<p>The API backend is working. To see the app, upload <code>index.html</code> to a folder called <code>static/</code> in your GitHub repo.</p>
<h3>Steps:</h3>
<ol>
<li>Go to your GitHub repo</li>
<li>Click <b>Add file → Create new file</b></li>
<li>Type <code>static/index.html</code> as the filename</li>
<li>Paste your <code>index.html</code> content and commit</li>
<li>Render will redeploy automatically</li>
</ol>
<p>API docs: <a href="/api/docs">/api/docs</a> | Health: <a href="/api/health">/api/health</a></p>
</div></body></html>""")

@app.get("/api/health")
async def health():
    db_ok = bool(DATABASE_URL)
    ai_ok = bool(ANTHROPIC_KEY)
    html_ok = os.path.exists(os.path.join("static", "index.html"))
    return {
        "status": "ok",
        "version": "3.0.0",
        "database": "configured" if db_ok else "⚠ DATABASE_URL not set",
        "ai": "configured" if ai_ok else "⚠ ANTHROPIC_API_KEY not set",
        "frontend": "found" if html_ok else "⚠ static/index.html not found"
    }

# ── Auth ────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = await db()
    try:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1 AND is_active=TRUE", form.username)
        if not user or not pwd_ctx.verify(form.password, user["password_hash"]):
            raise HTTPException(401, "Invalid credentials")
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])
        token = make_token({"sub": str(user["id"]), "role": user["role"],
                            "name": user["full_name"]})
        return {"access_token": token, "token_type": "bearer",
                "role": user["role"], "name": user["full_name"]}
    finally:
        await conn.close()

# ── Patients ────────────────────────────────────────────────────────────────────
@app.get("/api/patients")
async def list_patients(user=Depends(get_user)):
    conn = await db()
    try:
        if user["role"] == "admin":
            rows = await conn.fetch(
                "SELECT id,full_name,dob,gender,blood_group,status,"
                "primary_doctor,ai_summary FROM patients ORDER BY full_name")
        else:
            rows = await conn.fetch(
                "SELECT p.id,p.full_name,p.dob,p.gender,p.blood_group,"
                "p.status,p.primary_doctor,p.ai_summary FROM patients p "
                "JOIN user_patient_access a ON p.id=a.patient_id WHERE a.user_id=$1",
                user["sub"])
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@app.get("/api/patients/{pid}")
async def get_patient(pid: str, user=Depends(get_user)):
    conn = await db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404, "Not found")
        conds = await conn.fetch(
            "SELECT * FROM conditions WHERE patient_id=$1 AND is_active=TRUE "
            "ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 ELSE 3 END", pid)
        timeline = await conn.fetch(
            "SELECT * FROM timeline_events WHERE patient_id=$1 "
            "ORDER BY event_date DESC", pid)
        docs = await conn.fetch(
            "SELECT * FROM documents WHERE patient_id=$1 "
            "ORDER BY report_date DESC NULLS LAST", pid)
        syms = await conn.fetch(
            "SELECT * FROM symptoms WHERE patient_id=$1 "
            "ORDER BY created_at DESC", pid)
        labs = await conn.fetch(
            "SELECT marker,test_date,value,unit,ref_low,ref_high "
            "FROM lab_results WHERE patient_id=$1 ORDER BY test_date", pid)
        lab_map = {}
        for r in labs:
            m = r["marker"]
            if m not in lab_map:
                lab_map[m] = {"label": m, "unit": r["unit"],
                              "refLow": r["ref_low"], "refHigh": r["ref_high"], "data": []}
            lab_map[m]["data"].append({"d": str(r["test_date"])[:7], "v": float(r["value"])})
        return {"patient": dict(p), "conditions": [dict(c) for c in conds],
                "timeline": [dict(t) for t in timeline], "documents": [dict(d) for d in docs],
                "symptoms": [dict(s) for s in syms], "labs": lab_map}
    finally:
        await conn.close()

@app.post("/api/patients", dependencies=[Depends(admin_only)])
async def create_patient(data: PatientIn, bg: BackgroundTasks):
    conn = await db()
    try:
        pid = await conn.fetchval(
            "INSERT INTO patients (full_name,dob,gender,blood_group,primary_doctor,status) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            data.full_name, data.dob, data.gender,
            data.blood_group, data.primary_doctor, data.status)
        bg.add_task(create_box_folders, str(pid), data.full_name)
        return {"id": str(pid)}
    finally:
        await conn.close()

@app.put("/api/patients/{pid}", dependencies=[Depends(admin_only)])
async def update_patient(pid: str, data: PatientIn):
    conn = await db()
    try:
        await conn.execute(
            "UPDATE patients SET full_name=$1,dob=$2,gender=$3,blood_group=$4,"
            "primary_doctor=$5,status=$6,updated_at=NOW() WHERE id=$7",
            data.full_name, data.dob, data.gender,
            data.blood_group, data.primary_doctor, data.status, pid)
        return {"message": "Updated"}
    finally:
        await conn.close()

# ── Conditions ──────────────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/conditions", dependencies=[Depends(admin_only)])
async def add_condition(pid: str, data: ConditionIn):
    conn = await db()
    try:
        cid = await conn.fetchval(
            "INSERT INTO conditions "
            "(patient_id,name,severity,organ,organ_id,findings,recommended_action,last_reviewed) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            pid, data.name, data.severity, data.organ, data.organ_id,
            data.findings, data.recommended_action, data.last_reviewed)
        return {"id": str(cid)}
    finally:
        await conn.close()

@app.put("/api/conditions/{cid}", dependencies=[Depends(admin_only)])
async def update_condition(cid: str, data: ConditionIn):
    conn = await db()
    try:
        await conn.execute(
            "UPDATE conditions SET name=$1,severity=$2,organ=$3,organ_id=$4,"
            "findings=$5,recommended_action=$6,last_reviewed=$7,updated_at=NOW() WHERE id=$8",
            data.name, data.severity, data.organ, data.organ_id,
            data.findings, data.recommended_action, data.last_reviewed, cid)
        return {"message": "Updated"}
    finally:
        await conn.close()

# ── Labs ────────────────────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/labs", dependencies=[Depends(admin_only)])
async def add_lab(pid: str, data: LabIn):
    conn = await db()
    try:
        abn = ((data.ref_high and data.value > data.ref_high) or
               (data.ref_low  and data.value < data.ref_low))
        lid = await conn.fetchval(
            "INSERT INTO lab_results "
            "(patient_id,test_date,marker,value,unit,ref_low,ref_high,is_abnormal,lab_name) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
            pid, data.test_date, data.marker, data.value, data.unit,
            data.ref_low, data.ref_high, abn, data.lab_name)
        return {"id": str(lid), "is_abnormal": abn}
    finally:
        await conn.close()

# ── Symptoms ────────────────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/symptoms", dependencies=[Depends(admin_only)])
async def add_symptom(pid: str, data: SymptomIn):
    conn = await db()
    try:
        sid = await conn.fetchval(
            "INSERT INTO symptoms "
            "(patient_id,symptom,symptom_date,severity,status,notes) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            pid, data.symptom, data.symptom_date,
            data.severity, data.status, data.notes)
        return {"id": str(sid)}
    finally:
        await conn.close()

# ── File upload → Box ───────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/upload", dependencies=[Depends(admin_only)])
async def upload_file(pid: str,
                      file: UploadFile = File(...),
                      doc_type: str = Form("Lab"),
                      doctor: str = Form(""),
                      tags: str = Form(""),
                      bg: BackgroundTasks = BackgroundTasks()):
    if file.content_type not in ["application/pdf","image/jpeg","image/png","image/jpg"]:
        raise HTTPException(400, "Only PDF, JPG, PNG accepted")
    conn = await db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404)
        content = await file.read()
        folder_map = {"Lab":"box_lab_folder_id","Imaging":"box_imaging_folder_id",
                      "Prescription":"box_rx_folder_id","Hospital":"box_hospital_folder_id",
                      "Cardiac":"box_imaging_folder_id","Consult":"box_rx_folder_id"}
        folder_id   = p[folder_map.get(doc_type, "box_lab_folder_id")]
        box_file_id = await _box_upload(folder_id, file.filename, content) if folder_id else None
        box_link    = f"https://app.box.com/file/{box_file_id}" if box_file_id else None
        tag_list    = [t.strip() for t in tags.split(",") if t.strip()]
        doc_id = await conn.fetchval(
            "INSERT INTO documents "
            "(patient_id,box_file_id,file_name,doc_type,doctor_name,tags,box_link,file_size_bytes) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            pid, box_file_id, file.filename, doc_type,
            doctor, tag_list, box_link, len(content))
        if box_file_id:
            bg.add_task(_box_extract, pid, str(doc_id), box_file_id)
        return {"document_id": str(doc_id), "box_file_id": box_file_id, "box_link": box_link}
    finally:
        await conn.close()

# ── AI Summary (server-side — no CORS, key is secure) ─────────────────────────
@app.post("/api/patients/{pid}/ai-refresh", dependencies=[Depends(admin_only)])
async def ai_refresh(pid: str):
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set in Render environment variables")
    import anthropic
    conn = await db()
    try:
        p     = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        conds = await conn.fetch(
            "SELECT name,severity,findings FROM conditions "
            "WHERE patient_id=$1 AND is_active=TRUE LIMIT 10", pid)
        labs  = await conn.fetch(
            "SELECT marker,value,unit,test_date FROM lab_results "
            "WHERE patient_id=$1 ORDER BY test_date DESC LIMIT 12", pid)
        cond_text = "\n".join(f"- {c['name']} ({c['severity']}): {c['findings'][:150]}"
                              for c in conds)
        lab_text  = "\n".join(f"- {r['marker']}: {r['value']} {r['unit']} ({r['test_date']})"
                              for r in labs)
        prompt = (f"Clinical AI. Patient: {p['full_name']}, {p['gender']}, DOB {p['dob']}\n"
                  f"CONDITIONS:\n{cond_text}\nLABS:\n{lab_text}\n"
                  f'Return ONLY JSON: {{"summary":"2-3 sentences",'
                  f'"urgentConcerns":["max3"],"followUps":["max3"],'
                  f'"riskFlags":["max2"],"positives":["max2"],'
                  f'"overallRisk":"Low|Moderate|High|Critical"}}')
        resp    = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
            model="claude-sonnet-4-20250514", max_tokens=700,
            messages=[{"role": "user", "content": prompt}])
        summary = json.loads(
            resp.content[0].text.replace("```json","").replace("```","").strip())
        await conn.execute(
            "UPDATE patients SET ai_summary=$1::jsonb,ai_summary_updated_at=NOW() WHERE id=$2",
            json.dumps(summary), pid)
        return summary
    finally:
        await conn.close()

# ── Public AI summary endpoint (called by frontend, no auth needed) ────────────
# The frontend sends patient context; server calls Claude with its own API key.
# This avoids CORS issues and keeps the Anthropic key server-side and secure.

class AISummaryIn(BaseModel):
    name: str
    age: int
    gender: str
    bg: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    conditions: Optional[list] = []

@app.post("/api/ai/summary")
async def ai_summary_public(data: AISummaryIn):
    """Public endpoint — frontend calls this to get AI summary via server-side Claude key."""
    if not ANTHROPIC_KEY:
        raise HTTPException(503,
            "ANTHROPIC_API_KEY not set in Render environment variables. "
            "Go to Render dashboard → your web service → Environment → add ANTHROPIC_API_KEY")
    import anthropic
    cond_text = "\n".join(
        f"- {c.get('name','?')} ({c.get('sev','?')}): {str(c.get('findings',''))[:150]}"
        for c in (data.conditions or [])[:10]
    )
    prompt = (
        f"Clinical AI. Patient: {data.name}, Age {data.age}, {data.gender}, BG {data.bg}\n"
        f"Status: {data.status}. Latest: {data.summary}\n"
        f"Conditions:\n{cond_text}\n"
        f'Return ONLY JSON (no markdown): {{"summary":"2-3 sentences",'
        f'"urgentConcerns":["max3"],"followUps":["max3"],'
        f'"riskFlags":["max2"],"positives":["max2"],'
        f'"overallRisk":"Low|Moderate|High|Critical"}}'
    )
    resp = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-20250514", max_tokens=700,
        messages=[{"role": "user", "content": prompt}])
    return JSONResponse(
        content=json.loads(
            resp.content[0].text.replace("```json","").replace("```","").strip()))

# ── User management ─────────────────────────────────────────────────────────────
@app.post("/api/users", dependencies=[Depends(admin_only)])
async def create_user(data: UserIn):
    conn = await db()
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (username,email,password_hash,role,full_name) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            data.username, data.email, pwd_ctx.hash(data.password),
            data.role, data.full_name)
        return {"id": str(uid)}
    finally:
        await conn.close()

@app.post("/api/patients/{pid}/access/{uid}", dependencies=[Depends(admin_only)])
async def grant_access(pid: str, uid: str, admin=Depends(admin_only)):
    conn = await db()
    try:
        await conn.execute(
            "INSERT INTO user_patient_access (user_id,patient_id,granted_by) "
            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", uid, pid, admin["sub"])
        return {"message": "Access granted"}
    finally:
        await conn.close()

# ── Box helpers ─────────────────────────────────────────────────────────────────
async def create_box_folders(patient_id: str, patient_name: str):
    if not os.path.exists(BOX_CONFIG):
        log.warning("box_config.json missing — skipping Box folder creation")
        return
    try:
        from boxsdk import JWTAuth, Client
        cli = Client(JWTAuth.from_settings_file(BOX_CONFIG))
        root = cli.folder("0")
        mr   = next((f for f in root.get_items() if f.name == "Medical Records"), None)
        if not mr: mr = root.create_subfolder("Medical Records")
        pf  = mr.create_subfolder(patient_name)
        lf  = pf.create_subfolder("1 Lab Reports")
        imf = pf.create_subfolder("2 Imaging")
        rxf = pf.create_subfolder("3 Prescriptions")
        hf  = pf.create_subfolder("4 Hospital Visits")
        conn = await db()
        await conn.execute(
            "UPDATE patients SET box_folder_id=$1,box_lab_folder_id=$2,"
            "box_imaging_folder_id=$3,box_rx_folder_id=$4,box_hospital_folder_id=$5 WHERE id=$6",
            pf.id, lf.id, imf.id, rxf.id, hf.id, patient_id)
        await conn.close()
        log.info(f"Box folders created for {patient_name}")
    except Exception as e:
        log.error(f"Box folder creation failed: {e}")

async def _box_upload(folder_id: str, filename: str, content: bytes) -> Optional[str]:
    if not os.path.exists(BOX_CONFIG): return None
    try:
        from boxsdk import JWTAuth, Client
        return Client(JWTAuth.from_settings_file(BOX_CONFIG)) \
               .folder(folder_id).upload_stream(io.BytesIO(content), filename).id
    except Exception as e:
        log.error(f"Box upload failed: {e}"); return None

async def _box_extract(patient_id: str, doc_id: str, box_file_id: str):
    if not os.path.exists(BOX_CONFIG): return
    try:
        from boxsdk import JWTAuth, Client
        text = Client(JWTAuth.from_settings_file(BOX_CONFIG)) \
               .file(box_file_id).content().decode("utf-8", errors="replace")[:5000]
        conn = await db()
        await conn.execute("UPDATE documents SET extracted_text=$1 WHERE id=$2", text, doc_id)
        await conn.execute("UPDATE patients SET ai_summary=NULL WHERE id=$1", patient_id)
        await conn.close()
    except Exception as e:
        log.error(f"Box text extraction failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
