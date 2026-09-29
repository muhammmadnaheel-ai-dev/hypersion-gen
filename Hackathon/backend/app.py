from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from core import Config, analyze_csv, analyze_prompt, document_export_bytes, export_bytes, generate_relational, generate_tabular, get_dataset, init_db, list_datasets, list_versions, make_document, privacy_scan, save_dataset, save_version, validate

app = FastAPI(title="Hypersion Gen API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
init_db()


class Prompt(BaseModel):
    prompt: str


@app.get("/api/health")
def health(): return {"status":"ok","service":"Hypersion Gen"}


@app.get("/api/integrations")
def integrations():
    return {"openrouter":{"configured":bool(os.getenv("OPENROUTER_API_KEY")),"model":os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini")}}


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
def generate(config: Config):
    if config.kind not in ("tabular","relational"): raise HTTPException(400,"Choose tabular or relational generation")
    try:
        tables,flags=(generate_relational(config) if config.kind=="relational" else generate_tabular(config))
        quality=validate(tables,config,flags)
        privacy=privacy_scan(config)
        ident,version=save_dataset(config,tables,quality,privacy,flags)
        return {"id":ident,"version":version,"name":config.name,"kind":config.kind,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in tables.items()},"quality":quality,"privacy":privacy,"flags":flags}
    except ValueError as exc: raise HTTPException(400,str(exc))


@app.post("/api/datasets/{ident}/versions")
def regenerate_version(ident: str, config: Config):
    try:
        tables,flags=(generate_relational(config) if config.kind=="relational" else generate_tabular(config))
        quality=validate(tables,config,flags);privacy=privacy_scan(config)
        version=save_version(ident,config,tables,quality,privacy,flags)
        return {"id":ident,"version":version,"name":config.name,"kind":config.kind,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in tables.items()},"quality":quality,"privacy":privacy,"flags":flags}
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.get("/api/datasets/{ident}/versions")
def versions(ident: str):
    if not get_dataset(ident):raise HTTPException(404,"Dataset not found")
    return list_versions(ident)


@app.get("/api/datasets")
def datasets(): return list_datasets()


@app.get("/api/datasets/{ident}")
def dataset(ident: str, version: int | None = None):
    item=get_dataset(ident,version)
    if not item:raise HTTPException(404,"Dataset not found")
    return {**item,"tables":{k:{"count":len(v),"preview":v[:50],"columns":list(v[0]) if v else []} for k,v in item["tables"].items()}}


@app.get("/api/datasets/{ident}/rows")
def rows(ident: str, table: str, offset: int = Query(0,ge=0), limit: int = Query(50,ge=1,le=200), search: str = "", version: int | None = None):
    item=get_dataset(ident,version)
    if not item:raise HTTPException(404,"Dataset not found")
    if table not in item["tables"]:raise HTTPException(404,"Table not found")
    indexed=list(enumerate(item["tables"][table]))
    if search:indexed=[pair for pair in indexed if search.lower() in json.dumps(pair[1],ensure_ascii=False).lower()]
    page=indexed[offset:offset+limit]
    flags=item["flags"] if table==next(iter(item["tables"])) else {}
    flag_sets={name:set(values) for name,values in flags.items()}
    return {"total":len(indexed),"rows":[row for _,row in page],"flags":[[name for name,indices in flag_sets.items() if idx in indices] for idx,_ in page],"offset":offset,"limit":limit}


@app.get("/api/datasets/{ident}/document/{kind}")
def document(ident: str, kind: str, version: int | None = None):
    item=get_dataset(ident,version)
    if not item:raise HTTPException(404,"Dataset not found")
    try:return make_document(item,kind)
    except ValueError as exc:raise HTTPException(400,str(exc))


@app.get("/api/datasets/{ident}/document/{kind}/export/{fmt}")
def export_document(ident: str,kind: str,fmt: str,version: int | None = None):
    item=get_dataset(ident,version)
    if not item:raise HTTPException(404,"Dataset not found")
    if item["privacy"]["blocked"]:raise HTTPException(403,"Sensitive fields without treatment block export")
    try:body,mime,filename=document_export_bytes(make_document(item,kind),fmt)
    except ValueError as exc:raise HTTPException(400,str(exc))
    return Response(content=body,media_type=mime,headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/api/datasets/{ident}/export/{fmt}")
def export(ident: str, fmt: str, version: int | None = None):
    item=get_dataset(ident,version)
    if not item:raise HTTPException(404,"Dataset not found")
    try:body,mime,filename=export_bytes(item,fmt)
    except PermissionError as exc:raise HTTPException(403,str(exc))
    except ValueError as exc:raise HTTPException(400,str(exc))
    return Response(content=body,media_type=mime,headers={"Content-Disposition":f'attachment; filename="{filename}"'})
