"""
SBK Health — FastAPI Backend v2
- Auto-creates all database tables on startup (no shell needed)
- Full Box integration for file uploads
- Claude AI for patient summaries

Deploy on Render.com:
  Build command: pip install -r requirements.txt
  Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import asyncpg, json, os, io, logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sbkhealth")

app = FastAPI(title="SBK Health API", version="2.0.0", docs_url="/api/docs")

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"])

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "change-this-to-40-random-chars-in-production")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost/sbkhealth")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BOX_CONFIG_FILE = "box_config.json"

pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2   = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# ── AUTO-CREATE TABLES ON STARTUP ──────────────────────────────────────────────
# This runs every time the server starts. Uses IF NOT EXISTS so it's safe to
# run repeatedly - it will never overwrite existing data.

CREATE_TABLES_SQL = """
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
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id          UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    name                VARCHAR(250) NOT NULL,
    severity            VARCHAR(20)  NOT NULL CHECK (severity IN ('urgent','high','moderate','monitor','normal','resolved')),
    organ               VARCHAR(100),
    organ_id            VARCHAR(50),
    findings            TEXT,
    recommended_action  TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    last_reviewed       VARCHAR(30),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS lab_results (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id  UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    test_date   VARCHAR(20) NOT NULL,
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
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id       UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    box_file_id      VARCHAR(60),
    file_name        VARCHAR(255) NOT NULL,
    report_date      VARCHAR(20),
    doc_type         VARCHAR(50)  NOT NULL,
    doctor_name      VARCHAR(120),
    condition_link   VARCHAR(250),
    tags             TEXT[],
    extracted_text   TEXT,
    file_size_bytes  BIGINT,
    box_link         VARCHAR(500),
    created_at       TIMESTAMPTZ DEFAULT NOW()
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

-- Seed default admin and viewer accounts (only if they don't exist yet)
-- Passwords: admin123 and viewer123
INSERT INTO users (username, email, password_hash, role, full_name)
VALUES
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
    """Auto-create all tables when server starts. Safe to run repeatedly."""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(CREATE_TABLES_SQL)
        await conn.close()
        log.info("✅ Database tables ready")
    except Exception as e:
        log.error(f"❌ Database setup failed: {e}")
        # Don't crash the server - it may just be a temp connection issue

# ── Serve static HTML frontend ─────────────────────────────────────────────────
if os.path.exists("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="frontend")

# ── Helpers ────────────────────────────────────────────────────────────────────
async def get_db():
    return await asyncpg.connect(DATABASE_URL)

def make_token(data: dict) -> str:
    return jwt.encode(
        {**data, "exp": datetime.utcnow() + timedelta(hours=24)},
        SECRET_KEY, algorithm="HS256")

async def current_user(token: str = Depends(oauth2)):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

def admin_only(user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return user

# ── Pydantic schemas ────────────────────────────────────────────────────────────
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
    status: Optional[str] = "ongoing"; notes: Optional[str] = None

# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}

# ── Auth ────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = await get_db()
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
async def list_patients(user=Depends(current_user)):
    conn = await get_db()
    try:
        if user["role"] == "admin":
            rows = await conn.fetch(
                "SELECT id,full_name,dob,gender,blood_group,status,primary_doctor,"
                "ai_summary FROM patients ORDER BY full_name")
        else:
            rows = await conn.fetch(
                "SELECT p.id,p.full_name,p.dob,p.gender,p.blood_group,p.status,"
                "p.primary_doctor,p.ai_summary FROM patients p "
                "JOIN user_patient_access a ON p.id=a.patient_id WHERE a.user_id=$1",
                user["sub"])
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@app.get("/api/patients/{pid}")
async def get_patient(pid: str, user=Depends(current_user)):
    conn = await get_db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404, "Patient not found")
        conditions = await conn.fetch(
            "SELECT * FROM conditions WHERE patient_id=$1 AND is_active=TRUE "
            "ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 ELSE 3 END", pid)
        timeline  = await conn.fetch(
            "SELECT * FROM timeline_events WHERE patient_id=$1 ORDER BY event_date DESC", pid)
        documents = await conn.fetch(
            "SELECT * FROM documents WHERE patient_id=$1 ORDER BY report_date DESC NULLS LAST", pid)
        symptoms  = await conn.fetch(
            "SELECT * FROM symptoms WHERE patient_id=$1 ORDER BY created_at DESC", pid)
        labs      = await conn.fetch(
            "SELECT marker,test_date,value,unit,ref_low,ref_high "
            "FROM lab_results WHERE patient_id=$1 ORDER BY test_date", pid)
        lab_map = {}
        for row in labs:
            m = row["marker"]
            if m not in lab_map:
                lab_map[m] = {"label": m, "unit": row["unit"],
                              "refLow": row["ref_low"], "refHigh": row["ref_high"], "data": []}
            lab_map[m]["data"].append({"d": str(row["test_date"])[:7], "v": float(row["value"])})
        return {
            "patient": dict(p),
            "conditions": [dict(c) for c in conditions],
            "timeline":   [dict(t) for t in timeline],
            "documents":  [dict(d) for d in documents],
            "symptoms":   [dict(s) for s in symptoms],
            "labs":       lab_map
        }
    finally:
        await conn.close()

@app.post("/api/patients", dependencies=[Depends(admin_only)])
async def create_patient(data: PatientIn, bg: BackgroundTasks):
    conn = await get_db()
    try:
        pid = await conn.fetchval(
            "INSERT INTO patients (full_name,dob,gender,blood_group,primary_doctor,status) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            data.full_name, data.dob, data.gender,
            data.blood_group, data.primary_doctor, data.status)
        bg.add_task(create_box_folders, str(pid), data.full_name)
        return {"id": str(pid), "message": "Patient created. Box folders initialising."}
    finally:
        await conn.close()

@app.put("/api/patients/{pid}", dependencies=[Depends(admin_only)])
async def update_patient(pid: str, data: PatientIn):
    conn = await get_db()
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
    conn = await get_db()
    try:
        cid = await conn.fetchval(
            "INSERT INTO conditions (patient_id,name,severity,organ,organ_id,"
            "findings,recommended_action,last_reviewed) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            pid, data.name, data.severity, data.organ, data.organ_id,
            data.findings, data.recommended_action, data.last_reviewed)
        return {"id": str(cid)}
    finally:
        await conn.close()

@app.put("/api/conditions/{cid}", dependencies=[Depends(admin_only)])
async def update_condition(cid: str, data: ConditionIn):
    conn = await get_db()
    try:
        await conn.execute(
            "UPDATE conditions SET name=$1,severity=$2,organ=$3,organ_id=$4,"
            "findings=$5,recommended_action=$6,last_reviewed=$7,updated_at=NOW() WHERE id=$8",
            data.name, data.severity, data.organ, data.organ_id,
            data.findings, data.recommended_action, data.last_reviewed, cid)
        return {"message": "Updated"}
    finally:
        await conn.close()

# ── Lab results ─────────────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/labs", dependencies=[Depends(admin_only)])
async def add_lab(pid: str, data: LabIn):
    conn = await get_db()
    try:
        abnormal = ((data.ref_high and data.value > data.ref_high) or
                    (data.ref_low  and data.value < data.ref_low))
        lid = await conn.fetchval(
            "INSERT INTO lab_results (patient_id,test_date,marker,value,unit,"
            "ref_low,ref_high,is_abnormal,lab_name) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
            pid, data.test_date, data.marker, data.value, data.unit,
            data.ref_low, data.ref_high, abnormal, data.lab_name)
        return {"id": str(lid), "is_abnormal": abnormal}
    finally:
        await conn.close()

# ── Symptoms ────────────────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/symptoms", dependencies=[Depends(admin_only)])
async def add_symptom(pid: str, data: SymptomIn):
    conn = await get_db()
    try:
        sid = await conn.fetchval(
            "INSERT INTO symptoms (patient_id,symptom,symptom_date,severity,status,notes) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            pid, data.symptom, data.symptom_date, data.severity, data.status, data.notes)
        return {"id": str(sid)}
    finally:
        await conn.close()

# ── File Upload → Box ───────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/upload", dependencies=[Depends(admin_only)])
async def upload_file(
        pid: str,
        file: UploadFile = File(...),
        doc_type: str = Form("Lab"),
        doctor:   str = Form(""),
        tags:     str = Form(""),
        bg: BackgroundTasks = BackgroundTasks()):

    ALLOWED = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in ALLOWED:
        raise HTTPException(400, "Only PDF, JPG, PNG accepted")

    conn = await get_db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404, "Patient not found")
        content = await file.read()

        folder_map = {"Lab": "box_lab_folder_id", "Imaging": "box_imaging_folder_id",
                      "Prescription": "box_rx_folder_id", "Hospital": "box_hospital_folder_id",
                      "Cardiac": "box_imaging_folder_id", "Consult": "box_rx_folder_id"}
        folder_id = p[folder_map.get(doc_type, "box_lab_folder_id")]
        box_file_id = await upload_to_box(folder_id, file.filename, content) if folder_id else None
        box_link    = f"https://app.box.com/file/{box_file_id}" if box_file_id else None
        tag_list    = [t.strip() for t in tags.split(",") if t.strip()]

        doc_id = await conn.fetchval(
            "INSERT INTO documents (patient_id,box_file_id,file_name,doc_type,"
            "doctor_name,tags,box_link,file_size_bytes) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            pid, box_file_id, file.filename, doc_type,
            doctor, tag_list, box_link, len(content))

        if box_file_id:
            bg.add_task(extract_and_cache, pid, str(doc_id), box_file_id)

        return {"document_id": str(doc_id), "box_file_id": box_file_id, "box_link": box_link}
    finally:
        await conn.close()

# ── AI Summary Refresh ──────────────────────────────────────────────────────────
@app.post("/api/patients/{pid}/ai-refresh", dependencies=[Depends(admin_only)])
async def ai_refresh(pid: str):
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "Set ANTHROPIC_API_KEY environment variable on Render")

    import anthropic
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    conn = await get_db()
    try:
        p          = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        conditions = await conn.fetch(
            "SELECT name,severity,findings FROM conditions "
            "WHERE patient_id=$1 AND is_active=TRUE LIMIT 10", pid)
        labs       = await conn.fetch(
            "SELECT marker,value,unit,test_date FROM lab_results "
            "WHERE patient_id=$1 ORDER BY test_date DESC LIMIT 12", pid)

        from datetime import date
        cond_text = "\n".join(f"- {c['name']} ({c['severity']}): {c['findings'][:150]}"
                              for c in conditions)
        lab_text  = "\n".join(f"- {r['marker']}: {r['value']} {r['unit']} ({r['test_date']})"
                              for r in labs)

        prompt = (
            f"Clinical AI. Patient: {p['full_name']}, {p['gender']}, DOB {p['dob']}\n"
            f"CONDITIONS:\n{cond_text}\nLABS:\n{lab_text}\n"
            f"Return ONLY JSON (no markdown):\n"
            f'{{"summary":"2-3 sentences","urgentConcerns":["max3"],'
            f'"followUps":["max3"],"riskFlags":["max2"],'
            f'"positives":["max2"],"overallRisk":"Low|Moderate|High|Critical"}}'
        )
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}])
        summary = json.loads(
            response.content[0].text.replace("```json", "").replace("```", "").strip())
        await conn.execute(
            "UPDATE patients SET ai_summary=$1::jsonb, ai_summary_updated_at=NOW() WHERE id=$2",
            json.dumps(summary), pid)
        return summary
    finally:
        await conn.close()

# ── User management ─────────────────────────────────────────────────────────────
@app.post("/api/users", dependencies=[Depends(admin_only)])
async def create_user(data: UserIn):
    conn = await get_db()
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (username,email,password_hash,role,full_name) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            data.username, data.email, pwd_ctx.hash(data.password), data.role, data.full_name)
        return {"id": str(uid)}
    finally:
        await conn.close()

@app.post("/api/patients/{pid}/access/{uid}", dependencies=[Depends(admin_only)])
async def grant_access(pid: str, uid: str, admin=Depends(admin_only)):
    conn = await get_db()
    try:
        await conn.execute(
            "INSERT INTO user_patient_access (user_id,patient_id,granted_by) "
            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", uid, pid, admin["sub"])
        return {"message": "Access granted"}
    finally:
        await conn.close()

# ── Box helpers ─────────────────────────────────────────────────────────────────
async def create_box_folders(patient_id: str, patient_name: str):
    """Called in background after patient creation."""
    if not os.path.exists(BOX_CONFIG_FILE):
        log.warning("box_config.json not found — skipping Box folder creation")
        return
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        client = BoxClient(JWTAuth.from_settings_file(BOX_CONFIG_FILE))
        root   = client.folder("0")
        mr     = next((f for f in root.get_items() if f.name == "Medical Records"), None)
        if not mr: mr = root.create_subfolder("Medical Records")
        pf  = mr.create_subfolder(patient_name)
        lf  = pf.create_subfolder("1 Lab Reports")
        imf = pf.create_subfolder("2 Imaging")
        rxf = pf.create_subfolder("3 Prescriptions")
        hf  = pf.create_subfolder("4 Hospital Visits")
        conn = await get_db()
        await conn.execute(
            "UPDATE patients SET box_folder_id=$1, box_lab_folder_id=$2, "
            "box_imaging_folder_id=$3, box_rx_folder_id=$4, box_hospital_folder_id=$5 WHERE id=$6",
            pf.id, lf.id, imf.id, rxf.id, hf.id, patient_id)
        await conn.close()
        log.info(f"Box folders created for {patient_name}")
    except Exception as e:
        log.error(f"Box folder creation failed: {e}")

async def upload_to_box(folder_id: str, filename: str, content: bytes) -> Optional[str]:
    if not os.path.exists(BOX_CONFIG_FILE): return None
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        return BoxClient(JWTAuth.from_settings_file(BOX_CONFIG_FILE)) \
               .folder(folder_id).upload_stream(io.BytesIO(content), filename).id
    except Exception as e:
        log.error(f"Box upload failed: {e}"); return None

async def extract_and_cache(patient_id: str, doc_id: str, box_file_id: str):
    if not os.path.exists(BOX_CONFIG_FILE): return
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        text = BoxClient(JWTAuth.from_settings_file(BOX_CONFIG_FILE)) \
               .file(box_file_id).content().decode("utf-8", errors="replace")[:5000]
        conn = await get_db()
        await conn.execute("UPDATE documents SET extracted_text=$1 WHERE id=$2", text, doc_id)
        await conn.execute("UPDATE patients SET ai_summary=NULL WHERE id=$1", patient_id)
        await conn.close()
    except Exception as e:
        log.error(f"Text extraction failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
