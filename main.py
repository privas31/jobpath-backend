import os, json, smtplib, asyncio
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")
EMAIL_FROM = os.environ.get("EMAIL_FROM","")
EMAIL_PASS = os.environ.get("EMAIL_PASS","")
EMAIL_TO = os.environ.get("EMAIL_TO","")
MATCH_MIN = int(os.environ.get("MATCH_MIN","75"))
profiles_store={}
last_results=[]
last_run=""
scheduler=AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app):
    scheduler.add_job(auto_search,"interval",hours=24,id="daily_search")
    scheduler.start()
    yield
    scheduler.shutdown()

app=FastAPI(title="JobPath AI Backend",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

class ProfilePayload(BaseModel):
    slot:str
    profile:dict

class SearchPayload(BaseModel):
    query:str
    slot:str="warehouse"

async def call_claude(system,user,want_json=False):
    client=anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    tools=[{"type":"web_search_20250305","name":"web_search"}]
    msg=client.messages.create(model="claude-sonnet-4-6",max_tokens=2000,system=system,tools=tools,messages=[{"role":"user","content":user}])
    text="".join(b.text for b in msg.content if hasattr(b,"text"))
    if not want_json:return text
    clean=text.replace("```json","").replace("```","").strip()
    s,e=clean.find("["),clean.rfind("]")
    if s!=-1 and e!=-1:return clean[s:e+1]
    s,e=clean.find("{"),clean.rfind("}")
    if s!=-1 and e!=-1:return clean[s:e+1]
    return "[]"

async def search_jobs(query,profile):
    ctx=f"Candidato:{json.dumps(profile)}." if profile else ""
    raw=await call_claude(f"Busca empleos Orlando FL.{ctx} SOLO JSON.",f'Vacantes "{query}" Orlando/Kissimmee en Indeed/ZipRecruiter/employflorida.com. JSON array:[{{"titulo":"...","empresa":"...","ubicacion":"...","salario":"$XX/hr","tipo":"Full-time","descripcion":"...","fuente":"Indeed","match":85,"url":"https://..."}}] SOLO JSON.',want_json=True)
    try:
        jobs=json.loads(raw)
        return jobs if isinstance(jobs,list) else []
    except:return []

def send_email_alert(jobs,query,to_email):
    if not all([EMAIL_FROM,EMAIL_PASS,to_email]):return
    high=[j for j in jobs if j.get("match",0)>=MATCH_MIN]
    if not high:return
    rows="".join(f'<div style="border:1px solid #ddd;padding:12px;margin:8px 0;border-radius:8px"><b>{j.get("titulo","")}</b><br>{j.get("empresa","")} - {j.get("ubicacion","")}<br><span style="background:#1A7A4A;color:white;padding:2px 8px;border-radius:8px">{j["match"]}% match</span><p>{j.get("descripcion","")}</p><a href="{j.get("url","#")}">Aplicar</a></div>' for j in high)
    msg=MIMEMultipart("alternative")
    msg["Subject"]=f"JobPath: {len(high)} empleos - {query}"
    msg["From"]=EMAIL_FROM
    msg["To"]=to_email
    msg.attach(MIMEText(f'<h2>JobPath AI - {len(high)} vacantes</h2>{rows}',"html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_FROM,EMAIL_PASS)
            s.sendmail(EMAIL_FROM,to_email,msg.as_string())
    except Exception as ex:print(f"Email error:{ex}")

async def auto_search():
    global last_results,last_run
    if not profiles_store:return
    p=profiles_store.get("warehouse") or next(iter(profiles_store.values()))
    q=(p.get("buscar_puestos") or ["warehouse"])[0]
    jobs=await search_jobs(q,p)
    last_results=jobs
    last_run=datetime.now().isoformat()
    if EMAIL_TO:send_email_alert(jobs,q,EMAIL_TO)

@app.get("/")
def root():return{"status":"ok","last_run":last_run}

@app.post("/profile")
def save_profile(payload:ProfilePayload):
    profiles_store[payload.slot]=payload.profile
    return{"ok":True,"slots":list(profiles_store.keys())}

@app.get("/profile")
def get_profiles():return{"profiles":profiles_store}

@app.post("/search")
async def search(payload:SearchPayload):
    jobs=await search_jobs(payload.query,profiles_store.get(payload.slot,{}))
    return{"jobs":jobs,"count":len(jobs),"ts":datetime.now().isoformat()}

@app.post("/search/live")
async def search_live(payload:SearchPayload,background_tasks:BackgroundTasks):
    jobs=await search_jobs(payload.query,profiles_store.get(payload.slot,{}))
    if EMAIL_TO:background_tasks.add_task(send_email_alert,jobs,payload.query,EMAIL_TO)
    return{"jobs":jobs,"count":len(jobs),"alert_sent":bool(EMAIL_TO)}

@app.get("/results/last")
def last_results_route():return{"jobs":last_results,"last_run":last_run,"count":len(last_results)}

@app.post("/scheduler/run-now")
async def run_now():
    await auto_search()
    return{"ok":True,"jobs":len(last_results),"last_run":last_run}

@app.get("/health")
def health():return{"ok":True,"profiles":list(profiles_store.keys()),"last_run":last_run,"email_configured":bool(EMAIL_FROM and EMAIL_PASS),"alert_to":EMAIL_TO or "not set"}
