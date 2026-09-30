from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

if __package__:
    from .core import CREDITS_PER_THOUSAND_ROWS, FREE_PLAN_CREDITS, Config, InsufficientCreditsError, analyze_csv, analyze_prompt, credit_cost, document_export_bytes, explain_validation, export_bytes, generate_relational, generate_tabular, get_credit_balance, get_dataset, init_db, list_datasets, list_versions, make_document, migrate_legacy_firebase_owner, privacy_scan, save_dataset, save_version, validate
else:
    from core import CREDITS_PER_THOUSAND_ROWS, FREE_PLAN_CREDITS, Config, InsufficientCreditsError, analyze_csv, analyze_prompt, credit_cost, document_export_bytes, explain_validation, export_bytes, generate_relational, generate_tabular, get_credit_balance, get_dataset, init_db, list_datasets, list_versions, make_document, migrate_legacy_firebase_owner, privacy_scan, save_dataset, save_version, validate

app = FastAPI(title="Hypersion Gen API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
init_db()
credit_plans=[{"id":"starter","name":"Starter","price_pkr":500,"credits":500},{"id":"pro","name":"Pro","price_pkr":1500,"credits":2000},{"id":"premium","name":"Premium","price_pkr":3000,"credits":5000}]


def validate_firebase_access_token(access_token: str) -> str | None:
    project_id=os.getenv("FIREBASE_PROJECT_ID", "hypersion-50897").strip() or "hypersion-50897"
    try:
        claims=id_token.verify_firebase_token(access_token, GoogleAuthRequest(), audience=project_id)
    except ValueError:
        return None
    except google_auth_exceptions.GoogleAuthError as exc:
        raise HTTPException(503, "Firebase authentication could not validate the session") from exc
    user_id=claims.get("sub")
    email=claims.get("email")
    if not isinstance(user_id, str) or not user_id:
        return None
    if claims.get("email_verified") is True and isinstance(email, str) and email.strip():
        migrate_legacy_firebase_owner(user_id, email)
    return user_id


@app.middleware("http")
async def require_authenticated_user(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.url.path == "/api/health" or request.method == "OPTIONS":
        return await call_next(request)
    scheme, _, access_token=request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not access_token.strip():
        return JSONResponse(status_code=401, content={"detail":"Sign in to access this API"})
    try:
        user_id=await run_in_threadpool(validate_firebase_access_token, access_token.strip())
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail":exc.detail})
    if not user_id:
        return JSONResponse(status_code=401, content={"detail":"The Firebase session is invalid or expired"})
    request.state.user_id=user_id
    return await call_next(request)


class Prompt(BaseModel):
    prompt: str


@app.get("/api/health")
def health(): return {"status":"ok","service":"Hypersion Gen"}


@app.get("/api/billing")
def billing(request: Request):
    return {"balance":get_credit_balance(request.state.user_id),"free_plan_credits":FREE_PLAN_CREDITS,"credits_per_thousand_rows":CREDITS_PER_THOUSAND_ROWS,"checkout_ready":False,"plans":credit_plans}


@app.get("/api/integrations")
def integrations():
    supabase_configured=bool(os.getenv("SUPABASE_DB_URL","").strip())
    postgres_configured=bool(os.getenv("DATABASE_URL","").strip())
    provider="supabase-postgres" if supabase_configured else "postgres" if postgres_configured else "sqlite"
    return {"openrouter":{"configured":bool(os.getenv("OPENROUTER_API_KEY")),"model":os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini")},"database":{"provider":provider,"configured":supabase_configured or postgres_configured}}


@app.post("/api/analyze/prompt")
def prompt_analysis(body: Prompt):
    try: return analyze_prompt(body.prompt)
    except ValueError as exc: raise HTTPException(400,str(exc))


@app.post("/api/analyze/csv")
async def csv_analysis(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"): raise HTTPException(400,"Upload a .csv file")
    data=await file.read(5_000_001)
    try: return analyze_csv(data)
    except ValueError as exc: raise HTTPException(400,str(exc))


@app.post("/api/generate")
def generate(request: Request, config: Config):
    if config.kind not in ("tabular","relational"): raise HTTPException(400,"Choose tabular or relational generation")
    try:
        tables,flags=(generate_relational(config) if config.kind=="relational" else generate_tabular(config))
        quality=validate(tables,config,flags)
        privacy=privacy_scan(config)
        ident,version=save_dataset(config,tables,quality,privacy,flags,request.state.user_id)
        return {"id":ident,"version":version,"name":config.name,"kind":config.kind,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in tables.items()},"quality":quality,"privacy":privacy,"flags":flags,"credits_charged":credit_cost(config.rows),"credits_remaining":get_credit_balance(request.state.user_id)}
    except InsufficientCreditsError as exc: raise HTTPException(402,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))


@app.post("/api/datasets/{ident}/versions")
def regenerate_version(request: Request, ident: str, config: Config):
    try:
        tables,flags=(generate_relational(config) if config.kind=="relational" else generate_tabular(config))
        quality=validate(tables,config,flags);privacy=privacy_scan(config)
        version=save_version(ident,config,tables,quality,privacy,flags,request.state.user_id)
        return {"id":ident,"version":version,"name":config.name,"kind":config.kind,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in tables.items()},"quality":quality,"privacy":privacy,"flags":flags,"credits_charged":credit_cost(config.rows),"credits_remaining":get_credit_balance(request.state.user_id)}
    except InsufficientCreditsError as exc:raise HTTPException(402,str(exc))
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.get("/api/datasets/{ident}/versions")
def versions(request: Request, ident: str):
    if not get_dataset(ident,user_id=request.state.user_id):raise HTTPException(404,"Dataset not found")
    return list_versions(ident,user_id=request.state.user_id)


@app.post("/api/datasets/{ident}/explain-validation")
def validation_explanation(request: Request, ident: str, version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset version not found")
    try:return explain_validation(item["quality"])
    except ValueError as exc:raise HTTPException(503,str(exc))


@app.get("/api/datasets")
def datasets(request: Request): return list_datasets(user_id=request.state.user_id)


@app.get("/api/datasets/{ident}")
def dataset(request: Request, ident: str, version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset not found")
    return {**item,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in item["tables"].items()}}


@app.get("/api/datasets/{ident}/rows")
def rows(request: Request, ident: str, table: str, offset: int = Query(0,ge=0), limit: int = Query(50,ge=1,le=200), search: str = "", version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset not found")
    if table not in item["tables"]:raise HTTPException(404,"Table not found")
    indexed=list(enumerate(item["tables"][table]))
    if search:indexed=[pair for pair in indexed if search.lower() in json.dumps(pair[1],ensure_ascii=False).lower()]
    page=indexed[offset:offset+limit]
    flags=item["flags"] if table==next(iter(item["tables"])) else {}
    flag_sets={name:set(values) for name,values in flags.items()}
    return {"total":len(indexed),"rows":[row for _,row in page],"flags":[[name for name,indices in flag_sets.items() if idx in indices] for idx,_ in page],"offset":offset,"limit":limit}


@app.get("/api/datasets/{ident}/document/{kind}")
def document(request: Request, ident: str, kind: str, version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset not found")
    try:return make_document(item,kind)
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.get("/api/datasets/{ident}/document/{kind}/export/{fmt}")
def export_document(request: Request, ident: str,kind: str,fmt: str,version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset not found")
    if item["privacy"]["blocked"]:raise HTTPException(403,"Sensitive fields without treatment block export")
    try:body,mime,filename=document_export_bytes(make_document(item,kind),fmt)
    except ValueError as exc:raise HTTPException(400,str(exc))
    return Response(content=body,media_type=mime,headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/api/datasets/{ident}/export/{fmt}")
def export(request: Request, ident: str, fmt: str, version: int | None = None):
    item=get_dataset(ident,version,user_id=request.state.user_id)
    if not item:raise HTTPException(404,"Dataset not found")
    try:body,mime,filename=export_bytes(item,fmt)
    except PermissionError as exc:raise HTTPException(403,str(exc))
    except ValueError as exc:raise HTTPException(400,str(exc))
    return Response(content=body,media_type=mime,headers={"Content-Disposition":f'attachment; filename="{filename}"'})


if os.getenv("VERCEL"):
    app.frontend("/", directory=Path(__file__).resolve().parent.parent / "dist", fallback="index.html")
