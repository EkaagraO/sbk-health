"""
SBK Health — FastAPI Backend (main.py)
Install: pip install fastapi uvicorn asyncpg python-jose[cryptography] passlib[bcrypt] anthropic python-multipart aiofiles boxsdk
Run:     uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import anthropic, asyncpg, json, os, io

app = FastAPI(title="SBK Health API", version="3.0.0", docs_url="/api/docs")

app.add_middleware(CORSMiddleware,
    allow_origins=["http://localhost:3000","http://localhost:5173","http://127.0.0.1:8080","*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="frontend")

SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE-THIS-IN-PRODUCTION-USE-32-CHAR-MIN")
ALGORITHM = "HS256"
TOKEN_EXPIRE_H = 24
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost/sbkhealth")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BOX_CLIENT_ID = os.getenv("BOX_CLIENT_ID", "")
BOX_CLIENT_SECRET = os.getenv("BOX_CLIENT_SECRET", "")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

def make_token(data: dict) -> str:
    return jwt.encode({**data, "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_H)}, SECRET_KEY, ALGORITHM)

async def current_user(token: str = Depends(oauth2)):
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError: raise HTTPException(401, "Invalid or expired token")

def require_admin(user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Admin access required")
    return user

async def db():
    return await asyncpg.connect(DATABASE_URL)

class PatientCreate(BaseModel):
    full_name: str; dob: str; gender: str; blood_group: Optional[str] = None
    primary_doctor: Optional[str] = None; status: Optional[str] = None

class ConditionCreate(BaseModel):
    name: str; severity: str; organ: str; organ_id: str
    findings: str; recommended_action: str; last_reviewed: Optional[str] = None

class LabCreate(BaseModel):
    test_date: str; marker: str; value: float; unit: str
    ref_low: Optional[float] = None; ref_high: Optional[float] = None; lab_name: Optional[str] = None

class UserCreate(BaseModel):
    username: str; email: str; password: str; role: str; full_name: str

@app.post("/api/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = await db()
    try:
        user = await conn.fetchrow("SELECT * FROM users WHERE username=$1 AND is_active=TRUE", form.username)
        if not user or not pwd_ctx.verify(form.password, user["password_hash"]):
            raise HTTPException(401, "Invalid credentials")
        await conn.execute("UPDATE users SET last_login=NOW() WHERE id=$1", user["id"])
        token = make_token({"sub": str(user["id"]), "role": user["role"], "name": user["full_name"]})
        return {"access_token": token, "token_type": "bearer", "role": user["role"], "name": user["full_name"]}
    finally: await conn.close()

@app.get("/api/patients")
async def list_patients(user=Depends(current_user)):
    conn = await db()
    try:
        if user["role"] == "admin":
            rows = await conn.fetch("SELECT id,full_name,dob,gender,blood_group,status,primary_doctor,ai_summary FROM patients ORDER BY full_name")
        else:
            rows = await conn.fetch("""SELECT p.id,p.full_name,p.dob,p.gender,p.blood_group,p.status,p.primary_doctor,p.ai_summary
                FROM patients p JOIN user_patient_access a ON p.id=a.patient_id WHERE a.user_id=$1""", user["sub"])
        return [dict(r) for r in rows]
    finally: await conn.close()

@app.get("/api/patients/{pid}")
async def get_patient(pid: str, user=Depends(current_user)):
    conn = await db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404)
        conditions = await conn.fetch("SELECT * FROM conditions WHERE patient_id=$1 AND is_active=TRUE ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END", pid)
        timeline = await conn.fetch("SELECT * FROM timeline_events WHERE patient_id=$1 ORDER BY event_date DESC", pid)
        documents = await conn.fetch("SELECT * FROM documents WHERE patient_id=$1 ORDER BY report_date DESC NULLS LAST", pid)
        labs = await conn.fetch("SELECT marker,test_date,value,unit,ref_low,ref_high FROM lab_results WHERE patient_id=$1 ORDER BY test_date", pid)
        lab_map = {}
        for row in labs:
            m = row["marker"]
            if m not in lab_map: lab_map[m] = {"label": m, "unit": row["unit"], "refLow": row["ref_low"], "refHigh": row["ref_high"], "data": []}
            lab_map[m]["data"].append({"d": str(row["test_date"])[:7], "v": float(row["value"])})
        return {"patient": dict(p), "conditions": [dict(c) for c in conditions], "timeline": [dict(t) for t in timeline], "documents": [dict(d) for d in documents], "labs": lab_map}
    finally: await conn.close()

@app.post("/api/patients", dependencies=[Depends(require_admin)])
async def create_patient(data: PatientCreate, background_tasks: BackgroundTasks):
    conn = await db()
    try:
        pid = await conn.fetchval("INSERT INTO patients (full_name,dob,gender,blood_group,primary_doctor,status) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            data.full_name, data.dob, data.gender, data.blood_group, data.primary_doctor, data.status)
        background_tasks.add_task(create_box_folders, str(pid), data.full_name)
        return {"id": str(pid)}
    finally: await conn.close()

@app.post("/api/patients/{pid}/upload", dependencies=[Depends(require_admin)])
async def upload_file(pid: str, file: UploadFile = File(...), doc_type: str = Form("Lab"),
                       doctor: str = Form(""), tags: str = Form(""), background_tasks: BackgroundTasks = BackgroundTasks()):
    ALLOWED = ["application/pdf","image/jpeg","image/png","image/jpg"]
    if file.content_type not in ALLOWED: raise HTTPException(400, "Only PDF, JPG, PNG accepted")
    conn = await db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        if not p: raise HTTPException(404)
        content = await file.read()
        folder_map = {"Lab":"box_lab_folder_id","Imaging":"box_imaging_folder_id","Prescription":"box_rx_folder_id","Hospital":"box_hospital_folder_id"}
        folder_id = p[folder_map.get(doc_type, "box_lab_folder_id")]
        box_file_id = await upload_to_box(folder_id, file.filename, content) if folder_id else None
        box_link = f"https://app.box.com/file/{box_file_id}" if box_file_id else None
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        doc_id = await conn.fetchval("""INSERT INTO documents (patient_id,box_file_id,file_name,doc_type,doctor_name,tags,box_link,file_size_bytes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            pid, box_file_id, file.filename, doc_type, doctor, tag_list, box_link, len(content))
        if box_file_id:
            background_tasks.add_task(extract_and_cache, pid, str(doc_id), box_file_id)
        return {"document_id": str(doc_id), "box_file_id": box_file_id, "box_link": box_link}
    finally: await conn.close()

@app.post("/api/patients/{pid}/ai-refresh", dependencies=[Depends(require_admin)])
async def ai_refresh(pid: str):
    if not claude_client: raise HTTPException(503, "Set ANTHROPIC_API_KEY in .env")
    conn = await db()
    try:
        p = await conn.fetchrow("SELECT * FROM patients WHERE id=$1", pid)
        conditions = await conn.fetch("SELECT name,severity,findings FROM conditions WHERE patient_id=$1 AND is_active=TRUE LIMIT 10", pid)
        labs = await conn.fetch("SELECT marker,value,unit,test_date FROM lab_results WHERE patient_id=$1 ORDER BY test_date DESC LIMIT 12", pid)
        from datetime import date
        cond_text = "\n".join([f"- {c['name']} ({c['severity']}): {c['findings'][:150]}" for c in conditions])
        lab_text = "\n".join([f"- {r['marker']}: {r['value']} {r['unit']} ({r['test_date']})" for r in labs])
        prompt = f"""Clinical AI. Patient: {p['full_name']}, {p['gender']}, DOB {p['dob']}
CONDITIONS:\n{cond_text}\nLABS:\n{lab_text}
Return ONLY JSON: {{"summary":"2-3 sentences","urgentConcerns":["max3"],"followUps":["max3"],"riskFlags":["max2"],"positives":["max2"],"overallRisk":"Low|Moderate|High|Critical"}}"""
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=700, messages=[{"role":"user","content":prompt}])
        summary = json.loads(response.content[0].text.replace("```json","").replace("```","").strip())
        await conn.execute("UPDATE patients SET ai_summary=$1::jsonb,ai_summary_updated_at=NOW() WHERE id=$2", json.dumps(summary), pid)
        return summary
    finally: await conn.close()

@app.post("/api/users", dependencies=[Depends(require_admin)])
async def create_user(data: UserCreate):
    conn = await db()
    try:
        uid = await conn.fetchval("INSERT INTO users (username,email,password_hash,role,full_name) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            data.username, data.email, pwd_ctx.hash(data.password), data.role, data.full_name)
        return {"id": str(uid)}
    finally: await conn.close()

async def create_box_folders(patient_id: str, patient_name: str):
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        auth = JWTAuth.from_settings_file("box_config.json")
        client = BoxClient(auth)
        root = client.folder("0")
        mr = next((f for f in root.get_items() if f.name == "Medical Records"), None)
        if not mr: mr = root.create_subfolder("Medical Records")
        pf = mr.create_subfolder(patient_name)
        lf = pf.create_subfolder("1 Lab Reports"); imgf = pf.create_subfolder("2 Imaging")
        rxf = pf.create_subfolder("3 Prescriptions"); hf = pf.create_subfolder("4 Hospital Visits")
        conn = await db()
        await conn.execute("UPDATE patients SET box_folder_id=$1,box_lab_folder_id=$2,box_imaging_folder_id=$3,box_rx_folder_id=$4,box_hospital_folder_id=$5 WHERE id=$6",
            pf.id, lf.id, imgf.id, rxf.id, hf.id, patient_id)
        await conn.close()
    except Exception as e: print(f"Box folder creation failed: {e}")

async def upload_to_box(folder_id: str, filename: str, content: bytes) -> str:
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        auth = JWTAuth.from_settings_file("box_config.json")
        client = BoxClient(auth)
        return client.folder(folder_id).upload_stream(io.BytesIO(content), filename).id
    except Exception as e: print(f"Box upload failed: {e}"); return None

async def extract_and_cache(patient_id: str, doc_id: str, box_file_id: str):
    try:
        from boxsdk import JWTAuth, Client as BoxClient
        auth = JWTAuth.from_settings_file("box_config.json")
        extracted = BoxClient(JWTAuth.from_settings_file("box_config.json")).file(box_file_id).content().decode("utf-8", errors="replace")[:5000]
        if extracted:
            conn = await db()
            await conn.execute("UPDATE documents SET extracted_text=$1 WHERE id=$2", extracted, doc_id)
            await conn.execute("UPDATE patients SET ai_summary=NULL WHERE id=$1", patient_id)
            await conn.close()
    except Exception as e: print(f"Extraction failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
