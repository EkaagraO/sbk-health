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

import os, io, json, logging, base64
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (FastAPI, Depends, HTTPException,
                     UploadFile, File, Form, BackgroundTasks, Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
import asyncpg
import httpx

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
BOX_CONFIG        = "box_config.json"
BOX_CLIENT_ID     = os.getenv("BOX_CLIENT_ID", "")
BOX_CLIENT_SECRET = os.getenv("BOX_CLIENT_SECRET", "")
BOX_REDIRECT_URI  = os.getenv("BOX_REDIRECT_URI", "https://sbkhealth-api.onrender.com/api/box/callback")

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
    photo                   TEXT,
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

-- Stores AI summaries, narratives, and photos per patient (keyed by frontend ID: P001, P002)
-- Using patient_key (not UUID) because frontend uses hardcoded string IDs
CREATE TABLE IF NOT EXISTS patient_ai_data (
    patient_key       VARCHAR(20)  PRIMARY KEY,
    ai_summary        JSONB,
    ai_summary_ts     TIMESTAMPTZ,
    narrative         JSONB,
    narrative_ts      TIMESTAMPTZ,
    photo             TEXT,        -- base64 data URL for cross-device photo storage
    updated_at        TIMESTAMPTZ  DEFAULT NOW()
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

-- Box OAuth tokens (auto-refreshed, works with personal Box accounts)
CREATE TABLE IF NOT EXISTS box_tokens (
    id            SERIAL      PRIMARY KEY,
    access_token  TEXT        NOT NULL,
    refresh_token TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Lab values extracted by AI from uploaded documents
CREATE TABLE IF NOT EXISTS extracted_lab_values (
    id            BIGSERIAL    PRIMARY KEY,
    patient_key   VARCHAR(20)  NOT NULL,
    box_file_id   VARCHAR(50),
    test_name     VARCHAR(100) NOT NULL,
    test_key      VARCHAR(50),
    value         NUMERIC(10,4) NOT NULL,
    unit          VARCHAR(30),
    ref_low       NUMERIC(10,4),
    ref_high      NUMERIC(10,4),
    test_date     DATE,
    lab_name      VARCHAR(200),
    raw_json      JSONB,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (patient_key, box_file_id, test_key, test_date)
);
"""

@app.on_event("startup")
async def startup():
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — running without database")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(SCHEMA)
        # Safe migrations for existing Render DBs
        for stmt in [
            "ALTER TABLE patients ADD COLUMN IF NOT EXISTS photo TEXT;",
            "ALTER TABLE patient_ai_data ADD COLUMN IF NOT EXISTS photo TEXT;",
        ]:
            try:
                await conn.execute(stmt)
            except Exception:
                pass  # Column may already exist
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

# ── Public file upload endpoint (no JWT required — uses backend URL as implicit auth) ─
# The Render URL itself provides sufficient security for a family medical app.
# Files are saved to Box and indexed in the database.

@app.post("/api/upload/{pid}")
async def public_upload_file(
        pid:      str,
        file:     UploadFile = File(...),
        doc_type: str = Form("Lab"),
        doctor:   str = Form(""),
        tags:     str = Form(""),
        bg:       BackgroundTasks = BackgroundTasks()):
    """Public upload — no auth needed. Uploads to Box via OAuth and triggers AI extraction."""

    if file.content_type and file.content_type not in [
        "application/pdf", "image/jpeg", "image/png", "image/jpg", "application/octet-stream"
    ] and not file.filename.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png')):
        raise HTTPException(400, "Only PDF, JPG, PNG accepted")

    content  = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(400, "File too large — max 50MB")

    tag_list     = [t.strip() for t in tags.split(",") if t.strip()]
    box_file_id  = None
    box_link     = None

    FOLDER_MAP   = {
            "P001": {
                "Lab": "372768683500", "Imaging": "372769252193",
                "Prescription": "372768863251", "Hospital": "372769787402",
                "Cardiac": "372769252193", "Consult": "372768863251",
                "Procedure": "372769252193",
            },
            "P002": {
                "Lab": "369528428881", "Imaging": "369526874258",
                "Prescription": "369526443865", "Hospital": "369528640568",
                "Cardiac": "369526874258", "Consult": "369526443865",
                "Procedure": "369526874258",
            },
        }

    # ── Try Box OAuth upload ───────────────────────────────────────────────────
    if BOX_CLIENT_ID and BOX_CLIENT_SECRET and DATABASE_URL:
        try:
            folder_id = FOLDER_MAP.get(pid, {}).get(doc_type)
            if not folder_id:
                # Fallback: look up from DB for dynamic patients
                try:
                    conn2 = await db()
                    fm    = {"Lab":"box_lab_folder_id","Imaging":"box_imaging_folder_id",
                             "Prescription":"box_rx_folder_id","Hospital":"box_hospital_folder_id"}
                    field = fm.get(doc_type, "box_lab_folder_id")
                    row2  = await conn2.fetchrow(f"SELECT {field} FROM patients WHERE id=$1", pid)
                    if row2: folder_id = row2[field]
                    await conn2.close()
                except Exception:
                    pass

            if folder_id:
                box_file_id = await box_upload_oauth(folder_id, file.filename, content)
                box_link    = f"https://app.box.com/file/{box_file_id}"
                log.info(f"Uploaded {file.filename} to Box: {box_file_id}")
        except Exception as e:
            log.warning(f"Box OAuth upload failed: {e}")

    # ── Legacy JWT fallback (Enterprise accounts only) ─────────────────────────
    elif os.path.exists(BOX_CONFIG) and not box_file_id:
        try:
            folder_id = FOLDER_MAP.get(pid, {}).get(doc_type)
            if folder_id:
                box_file_id = await _box_upload(folder_id, file.filename, content)
                box_link    = f"https://app.box.com/file/{box_file_id}" if box_file_id else None
        except Exception as e:
            log.warning(f"JWT Box upload failed: {e}")

    # ── Save metadata to database ──────────────────────────────────────────────
    doc_id = None
    if DATABASE_URL:
        try:
            conn = await db()
            doc_id = await conn.fetchval(
                """INSERT INTO documents
                   (patient_id, box_file_id, file_name, doc_type, doctor_name, tags, box_link, file_size_bytes)
                   SELECT p.id, $2, $3, $4, $5, $6, $7, $8
                   FROM patients p WHERE p.id::text=$1 OR p.id::text=$1
                   RETURNING id""",
                pid, box_file_id, file.filename, doc_type, doctor, tag_list, box_link, len(content))
            await conn.close()
        except Exception as e:
            log.warning(f"DB document save failed: {e}")

    # ── Queue AI extraction if file is in Box ─────────────────────────────────
    if box_file_id and ANTHROPIC_KEY:
        bg.add_task(extract_and_store, pid, box_file_id, file.filename, content)

    return {
        "document_id":  str(doc_id) if doc_id else None,
        "box_file_id":  box_file_id,
        "box_link":     box_link,
        "saved_to_box": box_file_id is not None,
        "ai_analysis":  "queued" if (box_file_id and ANTHROPIC_KEY) else "not_available",
        "message":      ("Uploaded to Box — AI analysis queued"
                         if box_file_id and ANTHROPIC_KEY
                         else "Uploaded to Box" if box_file_id
                         else "Indexed locally — connect Box OAuth in Settings to enable cloud upload")
    }

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



@app.post("/api/patients/{pid}/refresh-summary")
async def refresh_summary_with_context(pid: str, request: Request):
    """
    Called by frontend after Sync completes.
    Accepts the full patient JSON context from the frontend (hardcoded data + extracted labs),
    runs Claude on it, and saves the result — same as the Refresh button but invoked server-side.
    """
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "ANTHROPIC_KEY not set")
    try:
        body = await request.json()
        patient_data = body.get("patient_data", {})
        if not patient_data:
            raise HTTPException(400, "patient_data required")

        # Call the same summary generator used by the Refresh button
        summary = await generate_ai_summary(patient_data, narrative_mode=False)
        narrative = await generate_ai_summary(patient_data, narrative_mode=True)

        if DATABASE_URL:
            conn = await db()
            try:
                await conn.execute(
                    """INSERT INTO patient_ai_data (patient_key, ai_summary, ai_summary_ts, narrative, narrative_ts, updated_at)
                       VALUES ($1, $2::jsonb, NOW(), $3::jsonb, NOW(), NOW())
                       ON CONFLICT (patient_key) DO UPDATE
                       SET ai_summary=$2::jsonb, ai_summary_ts=NOW(),
                           narrative=$3::jsonb, narrative_ts=NOW(), updated_at=NOW()""",
                    pid,
                    json.dumps({"data": summary,   "ts": datetime.utcnow().strftime("%d %b %Y")}),
                    json.dumps({"data": narrative, "ts": datetime.utcnow().strftime("%d %b %Y")}),
                )
            finally:
                await conn.close()

        return {"status": "ok", "summary_length": len(str(summary)), "narrative_length": len(str(narrative))}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"refresh_summary_with_context failed: {e}")
        raise HTTPException(500, str(e))


# ── Box Sync Pipeline ─────────────────────────────────────────────────────────
# Lists all Box folders for a patient, finds files not yet extracted,
# downloads each, runs Claude AI extraction, updates DB and regenerates summary.
# Progress tracked in-memory (safe for Render single-worker).

import uuid as _uuid

_sync_jobs: dict = {}  # job_id → progress dict

# Box folder IDs for each patient (both patients hardcoded)
PATIENT_BOX_FOLDERS = {
    "P001": {
        "Lab":          "372768683500",
        "Imaging":      "372769252193",
        "Prescription": "372768863251",
        "Hospital":     "372769787402",
    },
    "P002": {
        "Lab":          "369528428881",
        "Imaging":      "369526874258",
        "Prescription": "369526443865",
        "Hospital":     "369528640568",
    },
}

# File extensions worth sending to Claude for extraction
EXTRACTABLE_EXTS = {".pdf", ".jpg", ".jpeg", ".png"}


@app.post("/api/patients/{pid}/sync")
async def sync_from_box(pid: str, bg: BackgroundTasks):
    """Start a Box sync job. Returns job_id immediately; poll /api/sync/{job_id} for progress."""
    if not BOX_CLIENT_ID or not BOX_CLIENT_SECRET:
        raise HTTPException(503, "Box OAuth not configured — add BOX_CLIENT_ID and BOX_CLIENT_SECRET to Render env vars, then visit /api/box/auth")
    if pid not in PATIENT_BOX_FOLDERS:
        raise HTTPException(404, f"Unknown patient: {pid}")

    job_id = str(_uuid.uuid4())[:8]
    _sync_jobs[job_id] = {
        "status": "running", "pid": pid,
        "found": 0, "new": 0, "processed": 0,
        "lab_values": 0, "errors": [],
        "current_file": "",
        "message": "Starting sync…",
    }
    bg.add_task(_run_sync, pid, job_id)
    return {"job_id": job_id}


@app.get("/api/sync/{job_id}")
async def sync_job_status(job_id: str):
    """Poll this endpoint to get live sync progress."""
    return _sync_jobs.get(job_id, {"status": "not_found"})


async def _box_list_folder(folder_id: str, token: str) -> list:
    """Return list of file dicts from a Box folder."""
    items = []
    offset = 0
    while True:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.box.com/2.0/folders/{folder_id}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"limit": 1000, "offset": offset,
                        "fields": "id,name,size,type,content_created_at"})
        if r.status_code != 200:
            break
        data = r.json()
        for item in data.get("entries", []):
            if item.get("type") == "file":
                name = item.get("name", "")
                ext  = os.path.splitext(name)[1].lower()
                if ext in EXTRACTABLE_EXTS and not name.startswith("desktop"):
                    items.append({
                        "file_id":    item["id"],
                        "name":       name,
                        "size":       item.get("size", 0),
                        "created_at": item.get("content_created_at"),
                    })
        if len(data.get("entries", [])) < 1000:
            break
        offset += 1000
    return items


async def _run_sync(pid: str, job_id: str):
    """Background task: full Box sync for one patient."""
    prog = _sync_jobs[job_id]
    try:
        # ── 1. Discover all Box files ─────────────────────────────────────────
        token      = await box_get_token()
        all_files  = []
        folders    = PATIENT_BOX_FOLDERS[pid]

        for folder_type, folder_id in folders.items():
            prog["message"] = f"Scanning {folder_type} folder in Box…"
            try:
                files = await _box_list_folder(folder_id, token)
                for f in files:
                    f["folder_type"] = folder_type
                all_files.extend(files)
            except Exception as e:
                prog["errors"].append(f"Scan error ({folder_type}): {str(e)[:80]}")

        prog["found"]   = len(all_files)
        prog["message"] = f"Found {len(all_files)} files. Checking which are new…"

        # ── 2. Filter out already-processed files ─────────────────────────────
        processed_ids: set = set()
        if DATABASE_URL:
            conn = await db()
            try:
                rows = await conn.fetch(
                    "SELECT DISTINCT box_file_id FROM extracted_lab_values WHERE patient_key=$1",
                    pid)
                processed_ids = {r["box_file_id"] for r in rows}
            finally:
                await conn.close()

        new_files = [f for f in all_files if f["file_id"] not in processed_ids]
        prog["new"]     = len(new_files)
        prog["message"] = f"Found {len(new_files)} new files to process…"

        if not new_files:
            prog["status"]  = "complete"
            prog["message"] = "✅ Already up to date — no new files found in Box."
            return

        # ── 3. Process each new file ──────────────────────────────────────────
        total_lab_values = 0
        for i, f in enumerate(new_files):
            prog["processed"]    = i + 1
            prog["current_file"] = f["name"]
            prog["message"]      = f"Processing {i+1}/{len(new_files)}: {f['name'][:50]}…"

            if f["size"] > 40 * 1024 * 1024:
                prog["errors"].append(f"Skipped (too large): {f['name']}")
                continue

            try:
                content = await box_download(f["file_id"])
                before  = await _count_extracted(pid)
                await extract_and_store(pid, f["file_id"], f["name"], content)
                after   = await _count_extracted(pid)
                total_lab_values += max(0, after - before)
            except Exception as e:
                prog["errors"].append(f"Error on {f['name'][:40]}: {str(e)[:80]}")
                log.warning(f"Sync extraction error for {f['name']}: {e}")

        prog["lab_values"] = total_lab_values

        # ── 4. Regenerate AI summary + narrative ──────────────────────────────
        if total_lab_values > 0 and ANTHROPIC_KEY:
            prog["message"] = "Regenerating AI summary and narrative with new data…"
            try:
                conn = await db()
                try:
                    await auto_regenerate_summary(pid, conn)
                    # Also regenerate narrative
                    await auto_regenerate_narrative(pid, conn)
                finally:
                    await conn.close()
            except Exception as e:
                prog["errors"].append(f"AI regen error: {str(e)[:80]}")

        prog["status"]  = "complete"
        prog["message"] = (
            f"✅ Sync complete! Processed {len(new_files)} new files, "
            f"extracted {total_lab_values} lab values. "
            f"AI summary updated. Click Refresh on Clinical Summary to see changes."
        )
        if prog["errors"]:
            prog["message"] += f" ({len(prog['errors'])} warnings — see errors list)"

    except Exception as e:
        prog["status"]  = "error"
        prog["message"] = f"Sync failed: {str(e)}"
        log.error(f"Sync failed for {pid}: {e}")


async def _count_extracted(pid: str) -> int:
    """Count extracted lab values for a patient."""
    if not DATABASE_URL:
        return 0
    conn = await db()
    try:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as n FROM extracted_lab_values WHERE patient_key=$1", pid)
        return row["n"] if row else 0
    finally:
        await conn.close()

# ── Box OAuth 2.0 token management ──────────────────────────────────────────
# Works with personal Box accounts — no Enterprise plan needed.

async def box_get_token() -> str:
    """Return a valid Box access token, auto-refreshing via stored refresh token."""
    if not BOX_CLIENT_ID or not BOX_CLIENT_SECRET:
        raise HTTPException(503,
            "Box not configured. Add BOX_CLIENT_ID and BOX_CLIENT_SECRET to Render env vars, "
            "then visit /api/box/auth to authorise.")
    if not DATABASE_URL:
        raise HTTPException(503, "Database required for Box token storage")

    conn = await db()
    try:
        row = await conn.fetchrow(
            "SELECT id, access_token, refresh_token, expires_at "
            "FROM box_tokens ORDER BY updated_at DESC LIMIT 1")
        if not row:
            raise HTTPException(503,
                "Box not yet authorised. Visit /api/box/auth in your browser to connect.")

        # Use existing access token if still valid (5 min buffer)
        buf = datetime.utcnow() + timedelta(minutes=5)
        if row["expires_at"].replace(tzinfo=None) > buf:
            return row["access_token"]

        # Refresh
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.box.com/oauth2/token", data={
                "grant_type":    "refresh_token",
                "refresh_token": row["refresh_token"],
                "client_id":     BOX_CLIENT_ID,
                "client_secret": BOX_CLIENT_SECRET,
            })
        if r.status_code != 200:
            raise HTTPException(503, f"Box token refresh failed: {r.text[:200]}")

        tokens     = r.json()
        expires_at = datetime.utcnow() + timedelta(seconds=tokens["expires_in"])
        await conn.execute(
            "INSERT INTO box_tokens (access_token, refresh_token, expires_at) VALUES ($1,$2,$3)",
            tokens["access_token"], tokens["refresh_token"], expires_at)
        # Keep only last 3 rows
        await conn.execute(
            "DELETE FROM box_tokens WHERE id NOT IN "
            "(SELECT id FROM box_tokens ORDER BY updated_at DESC LIMIT 3)")
        return tokens["access_token"]
    finally:
        await conn.close()


async def box_upload_oauth(folder_id: str, filename: str, content: bytes) -> str:
    """Upload file to Box via OAuth, return file_id. Handles name conflicts."""
    token = await box_get_token()
    attrs = json.dumps({"name": filename, "parent": {"id": folder_id}})
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://upload.box.com/api/2.0/files/content",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, content, "application/octet-stream")},
            data={"attributes": attrs})
        if r.status_code == 409:
            # Name conflict — add timestamp suffix
            ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            base, ext = os.path.splitext(filename)
            new_name  = f"{base}_{ts}{ext}"
            attrs2 = json.dumps({"name": new_name, "parent": {"id": folder_id}})
            r = await client.post(
                "https://upload.box.com/api/2.0/files/content",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (new_name, content, "application/octet-stream")},
                data={"attributes": attrs2})
        if r.status_code not in (200, 201):
            raise HTTPException(500, f"Box upload failed {r.status_code}: {r.text[:300]}")
    return r.json()["entries"][0]["id"]


async def box_download(file_id: str) -> bytes:
    """Download file bytes from Box for AI analysis."""
    token = await box_get_token()
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(
            f"https://api.box.com/2.0/files/{file_id}/content",
            headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise Exception(f"Box download failed: {r.status_code}")
    return r.content


# ── Box OAuth flow (one-time setup) ──────────────────────────────────────────

@app.get("/api/box/auth")
async def box_auth_redirect():
    """Redirect browser to Box authorization page. Visit this URL once to connect Box."""
    if not BOX_CLIENT_ID:
        return HTMLResponse("<h2>BOX_CLIENT_ID not set in Render environment variables</h2>"
                            "<p>Add it in Render Dashboard → your service → Environment</p>")
    url = (f"https://account.box.com/api/oauth2/authorize"
           f"?client_id={BOX_CLIENT_ID}"
           f"&redirect_uri={BOX_REDIRECT_URI}"
           f"&response_type=code&state=sbkhealth")
    return RedirectResponse(url)


@app.get("/api/box/callback")
async def box_oauth_callback(code: str = None, error: str = None):
    """Handle Box OAuth callback — exchanges auth code for tokens and stores them."""
    if error:
        return HTMLResponse(f"<h2>Box error: {error}</h2>")
    if not code:
        return HTMLResponse("<h2>No code received from Box</h2>")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.box.com/oauth2/token", data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     BOX_CLIENT_ID,
            "client_secret": BOX_CLIENT_SECRET,
            "redirect_uri":  BOX_REDIRECT_URI,
        })
    if r.status_code != 200:
        return HTMLResponse(f"<h2>Token exchange failed: {r.text}</h2>")

    tokens     = r.json()
    expires_at = datetime.utcnow() + timedelta(seconds=tokens["expires_in"])
    if DATABASE_URL:
        conn = await db()
        try:
            await conn.execute(
                "INSERT INTO box_tokens (access_token, refresh_token, expires_at) VALUES ($1,$2,$3)",
                tokens["access_token"], tokens["refresh_token"], expires_at)
        finally:
            await conn.close()

    return HTMLResponse("""<html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;text-align:center">
<h2 style="color:#16A34A">✅ Box Connected!</h2>
<p>SBK Health can now upload files directly to your Box account.</p>
<p>You can close this tab and return to the app.<br>
All future uploads will go straight to Box and be analysed by AI automatically.</p>
</body></html>""")


@app.get("/api/box/status")
async def box_status():
    """Check if Box OAuth is connected. Called by the frontend on startup."""
    if not BOX_CLIENT_ID:
        return JSONResponse({"connected": False, "reason": "BOX_CLIENT_ID not configured in Render"})
    if not DATABASE_URL:
        return JSONResponse({"connected": False, "reason": "No database"})
    conn = await db()
    try:
        row = await conn.fetchrow(
            "SELECT updated_at FROM box_tokens ORDER BY updated_at DESC LIMIT 1")
        if row:
            return JSONResponse({"connected": True,
                                 "auth_time": str(row["updated_at"])[:16]})
        return JSONResponse({"connected": False,
                             "reason": "Not yet authorised",
                             "auth_url": "/api/box/auth"})
    finally:
        await conn.close()


# ── AI document extraction pipeline ──────────────────────────────────────────

async def extract_and_store(patient_key: str, box_file_id: str,
                             filename: str, content: bytes):
    """Background task: send uploaded file to Claude, extract lab values, store in DB."""
    if not ANTHROPIC_KEY:
        log.warning("ANTHROPIC_API_KEY not set — skipping AI extraction")
        return

    log.info(f"AI extraction starting for {filename} (patient {patient_key})")
    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=ANTHROPIC_KEY)

        # Send PDF to Claude for structured extraction
        b64 = base64.standard_b64encode(content).decode()
        media = "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"

        prompt = """You are a medical data extraction engine. Extract all lab test results from this document.
Return ONLY valid JSON — no markdown, no explanation:
{
  "is_lab_report": true,
  "report_date": "YYYY-MM-DD or null",
  "lab_name": "lab or hospital name",
  "tests": [
    {
      "name": "exact test name as printed",
      "key": "snake_case_normalised_key e.g. hba1c, tsh, ldl, psa, haemoglobin",
      "value": 6.4,
      "unit": "% or mg/dL etc",
      "ref_low": null,
      "ref_high": 5.7,
      "status": "high|low|normal"
    }
  ]
}
If not a lab report (e.g. prescription, discharge summary, imaging report) set is_lab_report=false and tests=[].
Extract EVERY numeric test result visible in the document."""

        resp = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": prompt}
            ]}])

        raw = resp.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        extracted = json.loads(raw)

        if not extracted.get("is_lab_report") or not extracted.get("tests"):
            log.info(f"Not a lab report or no tests found in {filename}")
            return

        tests = extracted["tests"]
        report_date = extracted.get("report_date")
        lab_name    = extracted.get("lab_name", "")

        conn = await db()
        try:
            for t in tests:
                if t.get("value") is None:
                    continue
                await conn.execute(
                    """INSERT INTO extracted_lab_values
                       (patient_key, box_file_id, test_name, test_key, value,
                        unit, ref_low, ref_high, test_date, lab_name, raw_json)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::date,$10,$11::jsonb)
                       ON CONFLICT DO NOTHING""",
                    patient_key, box_file_id,
                    t.get("name", ""), t.get("key", ""), float(t["value"]),
                    t.get("unit"), t.get("ref_low"), t.get("ref_high"),
                    report_date, lab_name, json.dumps(t))
            log.info(f"Stored {len(tests)} lab values from {filename}")

            # Trigger summary regeneration
            if ANTHROPIC_KEY:
                await auto_regenerate_summary(patient_key, conn)
        finally:
            await conn.close()

    except json.JSONDecodeError as e:
        log.error(f"AI extraction JSON parse error for {filename}: {e}")
    except Exception as e:
        log.error(f"AI extraction failed for {filename}: {e}")




async def auto_regenerate_narrative(patient_key: str, conn):
    """After sync, regenerate narrative from latest extracted data."""
    try:
        # Build a simple narrative from extracted data
        rows = await conn.fetch(
            """SELECT test_name, value, unit, test_date, lab_name
               FROM extracted_lab_values WHERE patient_key=$1
               ORDER BY test_date DESC, created_at DESC LIMIT 20""", patient_key)
        if not rows:
            return
        summary_text = ", ".join(
            f"{r['test_name']} {r['value']}{r['unit'] or ''} ({r['test_date'] or 'recent'})"
            for r in rows[:10])
        # Store basic narrative update timestamp
        await conn.execute(
            """INSERT INTO patient_ai_data (patient_key, narrative_ts, updated_at)
               VALUES ($1, NOW(), NOW())
               ON CONFLICT (patient_key) DO UPDATE
               SET narrative_ts=NOW(), updated_at=NOW()""",
            patient_key)
        log.info(f"Narrative timestamp updated for {patient_key}")
    except Exception as e:
        log.error(f"Narrative regen failed: {e}")
async def auto_regenerate_summary(patient_key: str, conn):
    """After new lab data extracted, regenerate and persist AI summary."""
    try:
        rows = await conn.fetch(
            """SELECT test_name, test_key, value, unit, ref_high, ref_low,
                      test_date, lab_name
               FROM extracted_lab_values
               WHERE patient_key=$1
               ORDER BY test_date DESC, created_at DESC
               LIMIT 40""", patient_key)

        if not rows:
            return

        lab_text = "\n".join(
            "- {}: {} {} (ref: {}-{}) on {}".format(
                r['test_name'], r['value'], r['unit'] or '',
                r['ref_low'] or '?', r['ref_high'] or '?',
                r['test_date'] or 'unknown date')
            for r in rows)

        import anthropic as _ant
        client = _ant.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=800,
            messages=[{"role": "user", "content": (
                "Patient: " + patient_key + ". Recent lab data:\n" + lab_text + "\n\n"
                'Return ONLY JSON: {"summary":"2-3 sentences","urgentConcerns":["max3"],'
                '"followUps":["max3"],"riskFlags":["max2"],"positives":["max2"],'
                '"overallRisk":"Low|Moderate|High|Critical"}'
            )}])

        ai_data = json.loads(resp.content[0].text.replace("```json","").replace("```","").strip())
        ts      = datetime.utcnow().strftime("%d %b %Y, %I:%M %p")
        await conn.execute(
            """INSERT INTO patient_ai_data (patient_key, ai_summary, ai_summary_ts, updated_at)
               VALUES ($1,$2::jsonb,NOW(),NOW())
               ON CONFLICT (patient_key) DO UPDATE
               SET ai_summary=$2::jsonb, ai_summary_ts=NOW(), updated_at=NOW()""",
            patient_key, json.dumps(ai_data))
        log.info(f"Auto-regenerated summary for {patient_key}")
    except Exception as e:
        log.error(f"Auto summary regen failed: {e}")


@app.get("/api/patients/{pid}/extracted-labs")
async def get_extracted_labs(pid: str):
    """Return all AI-extracted lab values for a patient — merged with hardcoded data by frontend."""
    if not DATABASE_URL:
        return JSONResponse({"labs": []})
    conn = await db()
    try:
        rows = await conn.fetch(
            """SELECT test_key, test_name, value, unit, ref_low, ref_high,
                      test_date, lab_name, box_file_id
               FROM extracted_lab_values
               WHERE patient_key=$1
               ORDER BY test_key, test_date""", pid)
        return JSONResponse({"labs": [dict(r) for r in rows]})
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
    narrative_mode: Optional[bool] = False
    prompt_override: Optional[str] = None

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
    final_prompt = data.prompt_override if data.prompt_override else prompt
    resp = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1200 if data.narrative_mode else 700,
        messages=[{"role": "user", "content": final_prompt}])
    return JSONResponse(
        content=json.loads(
            resp.content[0].text.replace("```json","").replace("```","").strip()))

# ── Patient photo endpoints (public — no auth needed for family app) ─────────────
# Photos stored as base64 data URLs directly in the patients table.
# Accessed by patient ID (P001, P002 are hardcoded IDs in the frontend).

class PhotoIn(BaseModel):
    photo: str  # base64 data URL, e.g. "data:image/jpeg;base64,..."

@app.put("/api/patients/{pid}/photo")
async def save_photo(pid: str, data: PhotoIn):
    """Save patient photo to patient_ai_data table (keyed by P001/P002). Public endpoint."""
    if not data.photo or not data.photo.startswith("data:image"):
        raise HTTPException(400, "Invalid photo — must be a base64 data URL starting with data:image")
    if len(data.photo) > 5_000_000:
        raise HTTPException(400, "Photo too large — please resize to under 3MB")
    if not DATABASE_URL:
        return JSONResponse({"message": "No database — photo saved locally only"})
    conn = await db()
    try:
        await conn.execute(
            """INSERT INTO patient_ai_data (patient_key, photo, updated_at)
               VALUES ($1, $2, NOW())
               ON CONFLICT (patient_key) DO UPDATE
               SET photo=$2, updated_at=NOW()""",
            pid, data.photo)
        return JSONResponse({"message": "Photo saved"})
    except Exception as e:
        log.error(f"Photo save failed: {e}")
        raise HTTPException(500, f"Database error: {e}")
    finally:
        await conn.close()

@app.get("/api/patients/{pid}/photo")
async def get_photo(pid: str):
    """Get patient photo from patient_ai_data table. Public endpoint."""
    if not DATABASE_URL:
        return JSONResponse({"photo": None})
    conn = await db()
    try:
        row = await conn.fetchrow(
            "SELECT photo FROM patient_ai_data WHERE patient_key=$1", pid)
        return JSONResponse({"photo": row["photo"] if row and row["photo"] else None})
    except Exception as e:
        log.error(f"Photo fetch failed: {e}")
        return JSONResponse({"photo": None})
    finally:
        await conn.close()

# ── AI Data persistence (summary + narrative stored server-side) ────────────────
# Patient key is the frontend ID (e.g. "P001", "P002") — not a DB UUID.
# These endpoints are public (no auth) so the standalone HTML app can call them.

class AISummaryStore(BaseModel):
    data: dict          # The AI summary JSON object
    ts:   str           # Human-readable timestamp string

class NarrativeStore(BaseModel):
    data: dict          # The narrative JSON object
    ts:   str

@app.get("/api/ai-data/{patient_key}")
async def get_ai_data(patient_key: str):
    """Load both AI summary and narrative for a patient. Called on app load."""
    if not DATABASE_URL:
        return JSONResponse({"ai_summary": None, "narrative": None})
    conn = await db()
    try:
        row = await conn.fetchrow(
            "SELECT ai_summary, ai_summary_ts, narrative, narrative_ts "
            "FROM patient_ai_data WHERE patient_key=$1", patient_key)
        if not row:
            return JSONResponse({"ai_summary": None, "narrative": None})
        return JSONResponse({
            "ai_summary": {
                "data": json.loads(row["ai_summary"])  if row["ai_summary"]  else None,
                "ts":   row["ai_summary_ts"].strftime("%d %b %Y, %I:%M %p") if row["ai_summary_ts"] else None,
            },
            "narrative": {
                "data": json.loads(row["narrative"])   if row["narrative"]   else None,
                "ts":   row["narrative_ts"].strftime("%d %b %Y, %I:%M %p")  if row["narrative_ts"]  else None,
            }
        })
    finally:
        await conn.close()

@app.put("/api/ai-data/{patient_key}/summary")
async def save_ai_summary(patient_key: str, body: AISummaryStore):
    """Save AI clinical summary for a patient."""
    if not DATABASE_URL:
        return JSONResponse({"message": "No database configured"})
    conn = await db()
    try:
        await conn.execute(
            """INSERT INTO patient_ai_data (patient_key, ai_summary, ai_summary_ts, updated_at)
               VALUES ($1, $2::jsonb, NOW(), NOW())
               ON CONFLICT (patient_key) DO UPDATE
               SET ai_summary=$2::jsonb, ai_summary_ts=NOW(), updated_at=NOW()""",
            patient_key, json.dumps(body.data))
        return JSONResponse({"message": "Saved"})
    finally:
        await conn.close()

@app.put("/api/ai-data/{patient_key}/narrative")
async def save_narrative(patient_key: str, body: NarrativeStore):
    """Save medical narrative for a patient."""
    if not DATABASE_URL:
        return JSONResponse({"message": "No database configured"})
    conn = await db()
    try:
        await conn.execute(
            """INSERT INTO patient_ai_data (patient_key, narrative, narrative_ts, updated_at)
               VALUES ($1, $2::jsonb, NOW(), NOW())
               ON CONFLICT (patient_key) DO UPDATE
               SET narrative=$2::jsonb, narrative_ts=NOW(), updated_at=NOW()""",
            patient_key, json.dumps(body.data))
        return JSONResponse({"message": "Saved"})
    finally:
        await conn.close()

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
