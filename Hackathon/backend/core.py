from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from scipy import stats
from template_library import retrieve_template


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "hypersion.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

FIRST = ["Aisha", "Omar", "Sofia", "Hamza", "Maria", "Zain", "Noor", "Daniel", "Sara", "Ali", "Maya", "Bilal", "Emma", "Hassan", "Fatima", "Lena"]
LAST = ["Khan", "Raza", "Chen", "Farooq", "Ahmed", "Malik", "Reed", "Siddiqui", "Iqbal", "Patel", "Wilson", "Shah"]
COUNTRIES = ["Pakistan", "United States", "United Kingdom", "UAE", "Germany", "Canada"]
PRODUCTS = [("Wireless mouse", 3500), ("USB-C hub", 8900), ("Laptop stand", 4250), ("Mechanical keyboard", 12700), ("Webcam", 9900), ("Desk lamp", 5600)]
PII_NAMES = {"name", "full_name", "first_name", "last_name", "email", "phone", "address", "ssn", "national_id", "iban"}
GENERIC_DOMAINS = {
    "hospital": [
        ("patients", "patient_id", {}, ["full_name", "email"]),
        ("doctors", "doctor_id", {}, ["full_name", "specialty"]),
        ("appointments", "appointment_id", {"patient_id":"patients", "doctor_id":"doctors"}, ["appointment_date", "status"]),
        ("records", "record_id", {"appointment_id":"appointments"}, ["diagnosis"]),
    ],
    "university": [
        ("students", "student_id", {}, ["full_name", "email"]),
        ("courses", "course_id", {}, ["title", "credits"]),
        ("enrollments", "enrollment_id", {"student_id":"students", "course_id":"courses"}, ["enrollment_date"]),
        ("results", "result_id", {"enrollment_id":"enrollments"}, ["score"]),
    ],
    "inventory": [
        ("suppliers", "supplier_id", {}, ["name", "email"]),
        ("products", "product_id", {"supplier_id":"suppliers"}, ["name", "price"]),
        ("warehouses", "warehouse_id", {}, ["name", "region"]),
        ("stock", "stock_id", {"product_id":"products", "warehouse_id":"warehouses"}, ["quantity"]),
    ],
    "hr": [
        ("departments", "department_id", {}, ["name"]),
        ("employees", "employee_id", {"department_id":"departments"}, ["full_name", "email"]),
        ("salaries", "salary_id", {"employee_id":"employees"}, ["amount", "pay_date"]),
        ("attendance", "attendance_id", {"employee_id":"employees"}, ["attendance_date", "status"]),
    ],
    "hotel": [
        ("guests", "guest_id", {}, ["full_name", "email"]),
        ("rooms", "room_id", {}, ["number", "rate"]),
        ("bookings", "booking_id", {"guest_id":"guests", "room_id":"rooms"}, ["start_date", "end_date", "total"]),
        ("payments", "payment_id", {"booking_id":"bookings"}, ["amount"]),
    ],
    "library": [
        ("members", "member_id", {}, ["full_name", "email"]),
        ("books", "book_id", {}, ["title", "author"]),
        ("loans", "loan_id", {"member_id":"members", "book_id":"books"}, ["loan_date", "due_date"]),
        ("returns", "return_id", {"loan_id":"loans"}, ["return_date"]),
    ],
}


class Column(BaseModel):
    name: str
    type: str = "text"
    key: str = ""
    pii: bool = False
    privacy: str = "synthetic"
    distribution: str = "auto"
    nullable: bool = False


class Config(BaseModel):
    name: str = "Untitled dataset"
    kind: str = "tabular"
    domain: str = "ecommerce"
    rows: int = Field(default=1000, ge=10, le=100000)
    seed: int = 42
    locale: str = "en_PK"
    currency: str = "PKR"
    null_rate: float = Field(default=0.03, ge=0, le=0.5)
    null_field: str | None = None
    outlier_rate: float = Field(default=0.01, ge=0, le=0.2)
    duplicate_rate: float = Field(default=0.01, ge=0, le=0.2)
    scenario: str = "Normal"
    columns: list[Column] = Field(default_factory=list)
    sample: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


@contextmanager
def connection():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS datasets (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          current_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS versions (
          dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, config_json TEXT NOT NULL, tables_json TEXT NOT NULL,
          quality_json TEXT NOT NULL, privacy_json TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(dataset_id, version)
        );
        CREATE INDEX IF NOT EXISTS versions_created ON versions(created_at DESC);
        """)
        fields={row["name"] for row in db.execute("PRAGMA table_info(versions)").fetchall()}
        if "flags_json" not in fields:db.execute("ALTER TABLE versions ADD COLUMN flags_json TEXT NOT NULL DEFAULT '{}'")


def _infer_type(name: str, values: list[str]) -> str:
    low = name.lower()
    if "email" in low: return "email"
    if "phone" in low: return "phone"
    if "date" in low or low.endswith("_at"): return "date"
    nonempty = [v for v in values if v not in ("", "null", "None")]
    if nonempty and all(re.fullmatch(r"-?\d+", v) for v in nonempty): return "integer"
    if nonempty and all(re.fullmatch(r"-?\d+(\.\d+)?", v) for v in nonempty): return "decimal"
    if len(set(nonempty)) <= max(12, len(nonempty) // 4) and nonempty: return "category"
    return "text"


def analyze_csv(content: bytes) -> dict:
    if len(content) > 5_000_000: raise ValueError("CSV is larger than the 5 MB sample limit")
    try: raw = content.decode("utf-8-sig")
    except UnicodeDecodeError: raise ValueError("Upload a UTF-8 encoded CSV")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames: raise ValueError("CSV needs a header row")
    if len(reader.fieldnames) > 100: raise ValueError("CSV has too many columns (maximum 100)")
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) == 200: break
    if not rows: raise ValueError("CSV needs at least one data row")
    columns = []
    for name in reader.fieldnames:
        values = [str(r.get(name) or "") for r in rows]
        low = name.lower()
        key = "PK" if low in ("id", "customer_id", "order_id") else "FK" if low.endswith("_id") else ""
        columns.append(Column(name=name, type=_infer_type(name, values), key=key, pii=low in PII_NAMES, nullable=any(not v for v in values)).model_dump())
    return {"columns": columns, "sample": rows, "confidence": 0.86, "source": "CSV sample", "explanation": f"Detected {len(columns)} columns from {len(rows)} sampled records. Review types and privacy rules before generating."}


def _fallback_prompt(prompt: str) -> dict:
    p = prompt.lower()
    count = re.search(r"\b([\d,]+)\s+(?:e-commerce\s+)?(?:rows|records|customers|employees|products|transactions|orders)", p)
    rows = min(100000, max(10, int(count.group(1).replace(",", "")))) if count else 1000
    domain = "customer" if "customer" in p else "employee" if "employee" in p else "product" if "product" in p else "transaction" if "transaction" in p or "bank" in p else "customer"
    templates = {
      "customer": [("customer_id","id"),("full_name","name"),("email","email"),("phone","phone"),("country","category"),("signup_date","date"),("balance","decimal"),("status","category")],
      "employee": [("employee_id","id"),("full_name","name"),("email","email"),("department","category"),("salary","decimal"),("joining_date","date"),("status","category")],
      "product": [("product_id","id"),("product_name","text"),("category","category"),("price","decimal"),("stock","integer"),("created_at","date")],
      "transaction": [("transaction_id","id"),("account_id","id"),("amount","decimal"),("type","category"),("transaction_date","date"),("merchant","text")],
    }
    missing = re.search(r"(\d+(?:\.\d+)?)%\s+missing\s+(\w+)", p)
    requested_field = missing.group(2) if missing else None
    available_fields={name for name,_ in templates[domain]}
    null_field = requested_field if requested_field in available_fields else requested_field[:-1] if requested_field and requested_field.endswith("s") and requested_field[:-1] in available_fields else requested_field
    columns = [Column(name=n, type=t, key="PK" if t == "id" and i == 0 else "", pii=n in PII_NAMES, nullable=n==null_field).model_dump() for i,(n,t) in enumerate(templates[domain])]
    banking="banking" in p or "bank account" in p
    relational=banking or "e-commerce" in p and ("orders" in p or "relational" in p)
    return {"name": "Banking operations" if banking else f"{domain.title()} dataset", "kind": "relational" if relational else "tabular", "domain":"banking" if banking else "ecommerce", "rows": rows, "columns": [] if relational else columns, "null_rate": float(missing.group(1))/100 if missing else 0.03, "null_field":null_field, "confidence": 0.72, "source": "local interpretation", "explanation": "A local parser prepared this schema. Configure an OpenRouter key to enable AI interpretation."}


def analyze_prompt(prompt: str) -> dict:
    if not prompt.strip(): raise ValueError("Describe the dataset you want to generate")
    key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    if key:
        payload = {"model": model, "response_format": {"type": "json_object"}, "messages": [
          {"role": "system", "content": "Infer a synthetic dataset configuration. Return JSON object with name, kind (tabular or relational), rows integer, columns array of objects with name,type,key,pii,privacy,distribution,nullable. Never include real personal data. Valid types: id,name,email,phone,text,category,date,integer,decimal. Do not generate rows."},
          {"role": "user", "content": prompt[:4000]}]}
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(payload).encode(), {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    body = json.load(response)
                parsed = json.loads(body["choices"][0]["message"]["content"])
                config = Config.model_validate({**_fallback_prompt(prompt), **parsed})
                return {**config.model_dump(), "confidence": 0.9, "source": f"OpenRouter · {model}", "explanation": "AI interpreted intent and proposed a schema. Deterministic engines will generate the values."}
            except urllib.error.HTTPError as exc:
                if exc.code in (401,403):raise ValueError("OpenRouter rejected the API key. Check backend/.env and restart the API.") from exc
                if exc.code==402:raise ValueError("OpenRouter requires credits for this model. Check your OpenRouter account or choose another model.") from exc
                if exc.code==429 and attempt==1:raise ValueError("OpenRouter rate limit reached. Wait briefly and retry.") from exc
                if exc.code not in (429,500,502,503,504) or attempt==1:
                    raise ValueError(f"OpenRouter request failed (HTTP {exc.code}). Check the model and try again.") from exc
            except urllib.error.URLError as exc:
                if attempt==1:raise ValueError("OpenRouter could not be reached. Check your connection and retry.") from exc
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("OpenRouter returned an invalid schema. Try a different model or revise the prompt.") from exc
    return _fallback_prompt(prompt)


def _sample_numeric(name: str, n: int, rng: np.random.Generator, sample: list[dict], typ: str) -> list[Any]:
    observed = []
    for row in sample:
        try: observed.append(float(row[name]))
        except (KeyError, ValueError, TypeError): pass
    if len(observed) >= 5:
        arr = np.array(observed)
        if np.all(arr > 0) and stats.skew(arr) > 0.7:
            shape, loc, scale = stats.lognorm.fit(arr, floc=0)
            result = stats.lognorm.rvs(shape, loc=loc, scale=scale, size=n, random_state=rng)
        else:
            result = rng.normal(float(arr.mean()), max(float(arr.std()), 0.01), n)
    elif any(k in name for k in ("balance", "salary", "price", "amount", "revenue")):
        result = rng.lognormal(7.1 if "salary" not in name else 10.5, 0.6, n)
    elif "age" in name: result = rng.normal(36, 11, n)
    elif "stock" in name: result = rng.poisson(85, n)
    else: result = rng.normal(50, 15, n)
    result = np.maximum(result, 0)
    return [int(round(x)) if typ == "integer" else round(float(x), 2) for x in result]


def generate_tabular(config: Config) -> tuple[dict[str,list[dict]],dict]:
    rng = np.random.default_rng(config.seed)
    n = config.rows
    columns = config.columns or [Column(name=n, type=t, key="PK" if i == 0 else "", pii=n in PII_NAMES) for i,(n,t) in enumerate([("customer_id","id"),("full_name","name"),("email","email"),("country","category"),("balance","decimal"),("signup_date","date")])]
    if any(not c.name.strip() for c in columns) or len({c.name for c in columns})!=len(columns):
        raise ValueError("Column names must be nonempty and unique")
    data: dict[str,list[Any]] = {}
    name_indexes = rng.integers(0, len(FIRST) * len(LAST), n)
    for col in columns:
        name, typ = col.name.lower(), col.type.lower()
        if col.key == "PK" or typ == "id": vals = [f"{col.name[:1].upper()}-{i+1:06d}" for i in range(n)]
        elif typ == "name" or "name" in name and "product" not in name:
            vals = [f"{FIRST[i % len(FIRST)]} {LAST[(i // len(FIRST)) % len(LAST)]}" for i in name_indexes]
        elif typ == "email": vals = [f"{FIRST[i % len(FIRST)].lower()}.{LAST[(i // len(FIRST)) % len(LAST)].lower()}{j+1}@example.test" for j,i in enumerate(name_indexes)]
        elif typ == "phone": vals = [f"+92-3{int(x):02d}-{int(y):07d}" for x,y in zip(rng.integers(0,100,n), rng.integers(0,10_000_000,n))]
        elif typ == "date": vals = [(date(2026,9,29)-timedelta(days=int(x))).isoformat() for x in rng.integers(0,730,n)]
        elif typ in ("integer", "decimal", "number"): vals = _sample_numeric(name,n,rng,config.sample,typ)
        elif typ == "category":
            observed = [str(r.get(col.name)) for r in config.sample if r.get(col.name)] if not col.pii else []
            options = list(Counter(observed)) if observed else COUNTRIES if "country" in name or "location" in name else ["Active","Pending","Dormant","Churned"] if "status" in name else ["Retail","Business","Enterprise","Education"]
            probs = np.array([observed.count(v) for v in options], dtype=float) if observed else None
            if probs is not None: probs /= probs.sum()
            vals = rng.choice(options,n,p=probs).tolist()
        elif "product" in name: vals = rng.choice([p[0] for p in PRODUCTS],n).tolist()
        else: vals = rng.choice(["Standard","Priority","Managed","Self-service"],n).tolist()
        data[col.name] = vals
    rows = [{name: vals[i] for name,vals in data.items()} for i in range(n)]
    flags: dict[str,list[int]] = {"missing":[],"outlier":[],"duplicate":[]}
    eligible = [c for c in columns if c.key != "PK" and c.type not in ("id",)]
    null_eligible = [c for c in eligible if c.name==config.null_field] if config.null_field else eligible
    if config.null_field and not null_eligible:
        raise ValueError(f"Missing-value target is not a non-key column: {config.null_field}")
    numeric = [c for c in eligible if c.type in ("integer","decimal","number")]
    null_count=round(n*config.null_rate) if null_eligible else 0
    outlier_count=round(n*config.outlier_rate) if numeric else 0
    duplicate_count=round(n*config.duplicate_rate) if eligible else 0
    order=rng.permutation(n)
    null_indices=order[:null_count]
    outlier_indices=order[null_count:null_count+outlier_count]
    duplicate_indices=order[null_count+outlier_count:null_count+outlier_count+duplicate_count]
    clean_indices=order[null_count+outlier_count+duplicate_count:]
    if null_eligible:
        for idx in null_indices:
            col = null_eligible[int(rng.integers(0,len(null_eligible)))]
            rows[int(idx)][col.name] = None; flags["missing"].append(int(idx))
    if numeric:
        for idx in outlier_indices:
            col = numeric[int(rng.integers(0,len(numeric)))]
            rows[int(idx)][col.name] = round(float(rows[int(idx)][col.name])*10+1000,2)
            flags["outlier"].append(int(idx))
    if eligible and len(clean_indices):
        for idx in duplicate_indices:
            src = int(rng.choice(clean_indices))
            for col in eligible: rows[int(idx)][col.name] = rows[src][col.name]
            flags["duplicate"].append(int(idx))
    for col in columns:
        if not col.pii or col.privacy=="synthetic":continue
        if col.privacy=="noise" and col.type not in ("integer","decimal","number"):
            raise ValueError(f"Noise treatment requires a numeric field: {col.name}")
        if col.privacy not in ("mask","hash","noise"):
            continue
        for row in rows:
            value=row[col.name]
            if value is None:continue
            if col.privacy=="noise":
                noisy=max(0,float(value)+float(rng.laplace(0,1)))
                row[col.name]=int(round(noisy)) if col.type=="integer" else round(noisy,2)
            else:
                digest=hashlib.sha256(f"{config.seed}:{col.name}:{value}".encode()).hexdigest()
                if col.privacy=="hash":row[col.name]=digest[:20]
                elif col.type=="email":row[col.name]=f"{str(value)[0]}***{digest[:5]}@example.test"
                elif col.type=="phone":row[col.name]="+92-***-"+str(value)[-4:]
                else:row[col.name]=str(value)[:1]+"***"+digest[:5]
    table_name=re.sub(r"[^a-z0-9_]+","_",config.name.lower()).strip("_") or "dataset"
    return {table_name:rows}, flags


def generate_relational(config: Config) -> tuple[dict[str,list[dict]],dict]:
    if config.domain=="banking":return generate_banking(config)
    if config.domain in GENERIC_DOMAINS:return generate_generic_relational(config)
    if config.domain!="ecommerce":raise ValueError("Unsupported relational domain")
    rng = np.random.default_rng(config.seed)
    customers = []
    for i in range(max(10, config.rows//4)):
        first, last = FIRST[int(rng.integers(len(FIRST)))], LAST[int(rng.integers(len(LAST)))]
        customers.append({"customer_id":f"C-{i+1:06d}","full_name":f"{first} {last}","email":f"{first.lower()}.{last.lower()}{i+1}@example.test","country":str(rng.choice(COUNTRIES))})
    products = [{"product_id":f"P-{i+1:03d}","name":name,"price":price} for i,(name,price) in enumerate(PRODUCTS)]
    orders, items, payments = [], [], []
    for i in range(config.rows):
        order_id = f"O-{i+1:06d}"; customer = customers[int(rng.integers(len(customers)))]
        amount = 0
        for product_idx in rng.choice(len(products),size=int(rng.integers(1,4)),replace=False):
            product = products[int(product_idx)]; qty = int(rng.integers(1,4)); line_total = qty*product["price"]
            items.append({"item_id":f"I-{len(items)+1:07d}","order_id":order_id,"product_id":product["product_id"],"item":product["name"],"qty":qty,"unit_price":product["price"],"line_total":line_total})
            amount += line_total
        orders.append({"order_id":order_id,"customer_id":customer["customer_id"],"order_date":(date(2026,9,29)-timedelta(days=int(rng.integers(0,365)))).isoformat(),"total":amount,"status":str(rng.choice(["Paid","Pending","Fulfilled"],p=[.65,.15,.20]))})
        payments.append({"payment_id":f"PM-{i+1:06d}","order_id":order_id,"amount":amount,"method":str(rng.choice(["Card","Bank transfer","Wallet"]))})
    return {"customers":customers,"products":products,"orders":orders,"order_items":items,"payments":payments}, {"missing":[],"outlier":[],"duplicate":[]}


def generate_banking(config: Config) -> tuple[dict[str,list[dict]],dict]:
    rng=np.random.default_rng(config.seed)
    customers=[];accounts=[];transactions=[]
    account_count=max(10,config.rows//8)
    for i in range(account_count):
        first=FIRST[int(rng.integers(len(FIRST)))];last=LAST[int(rng.integers(len(LAST)))]
        customer_id=f"C-{i+1:06d}";account_id=f"A-{i+1:06d}"
        customers.append({"customer_id":customer_id,"full_name":f"{first} {last}","email":f"{first.lower()}.{last.lower()}{i+1}@example.test"})
        accounts.append({"account_id":account_id,"customer_id":customer_id,"account_type":str(rng.choice(["Current","Savings"])),"opening_balance":100000,"balance":100000})
    account_transactions=defaultdict(list)
    for i in range(config.rows):account_transactions[int(rng.integers(account_count))].append(i)
    for account_index,indices in account_transactions.items():
        account=accounts[account_index];balance=account["opening_balance"]
        for i in indices:
            credit=bool(rng.random()<.38)
            amount=int(rng.integers(500,15000))
            if not credit:amount=min(amount,balance)
            balance+=amount if credit else -amount
            transactions.append({"transaction_id":f"T-{i+1:07d}","account_id":account["account_id"],"transaction_date":(date(2026,9,29)-timedelta(days=int(rng.integers(0,365)))).isoformat(),"description":str(rng.choice(["Payroll deposit","Greenleaf Market","Riverside Utilities","ATM withdrawal","Transfer"])),"debit":0 if credit else amount,"credit":amount if credit else 0,"balance":balance})
        account["balance"]=balance
    transactions.sort(key=lambda row:(row["account_id"],row["transaction_date"],row["transaction_id"]))
    account_map={account["account_id"]:account for account in accounts}
    balances={account["account_id"]:account["opening_balance"] for account in accounts}
    for transaction in transactions:
        account_id=transaction["account_id"]
        transaction["debit"]=min(transaction["debit"],balances[account_id])
        balances[account_id]+=transaction["credit"]-transaction["debit"]
        transaction["balance"]=balances[account_id]
    for account_id,balance in balances.items():account_map[account_id]["balance"]=balance
    return {"customers":customers,"accounts":accounts,"transactions":transactions}, {"missing":[],"outlier":[],"duplicate":[]}


def generate_generic_relational(config: Config) -> tuple[dict[str,list[dict]],dict]:
    rng=np.random.default_rng(config.seed)
    tables:dict[str,list[dict]]={}
    child_tables={"appointments","records","enrollments","results","stock","salaries","attendance","bookings","payments","loans","returns"}
    for table,pk,fks,fields in GENERIC_DOMAINS[config.domain]:
        count=config.rows if table in child_tables else max(10,config.rows//4)
        rows=[]
        for i in range(count):
            row={pk:f"{pk[:1].upper()}-{i+1:07d}"}
            for fk,parent in fks.items():
                parent_pk=next(iter(tables[parent][0]))
                row[fk]=tables[parent][int(rng.integers(len(tables[parent])))][parent_pk]
            first=FIRST[int(rng.integers(len(FIRST)))];last=LAST[int(rng.integers(len(LAST)))]
            for field in fields:
                if field=="full_name":row[field]=f"{first} {last}"
                elif field=="email":row[field]=f"{first.lower()}.{last.lower()}{i+1}@example.test"
                elif field.endswith("date"):
                    days=int(rng.integers(0,365))
                    if field=="end_date":row[field]=(date.fromisoformat(row.get("start_date",date(2026,9,29).isoformat()))+timedelta(days=int(rng.integers(1,8)))).isoformat()
                    elif field=="due_date":row[field]=(date.fromisoformat(row.get("loan_date",date(2026,9,29).isoformat()))+timedelta(days=14)).isoformat()
                    elif field=="return_date":row[field]=(date(2026,9,29)-timedelta(days=days)).isoformat()
                    else:row[field]=(date(2026,9,29)-timedelta(days=days)).isoformat()
                elif field in ("price","rate","amount","total"):row[field]=int(rng.integers(500,20000))
                elif field in ("quantity","credits","score","number"):row[field]=int(rng.integers(1,101))
                elif field=="status":row[field]=str(rng.choice(["Active","Pending","Completed"]))
                elif field=="specialty":row[field]=str(rng.choice(["General","Cardiology","Pediatrics","Radiology"]))
                elif field=="diagnosis":row[field]=str(rng.choice(["Routine checkup","Follow-up","Laboratory review"]))
                elif field=="region":row[field]=str(rng.choice(COUNTRIES))
                elif field=="title":row[field]=str(rng.choice(["Introduction to Data","Applied Systems","Research Methods","Statistics"]))
                elif field=="author":row[field]=f"{first} {last}"
                else:row[field]=str(rng.choice(["Standard","Premium","Operations","North","Central"]))
            rows.append(row)
        tables[table]=rows
    if config.domain=="hotel":
        room_rates={r["room_id"]:r["rate"] for r in tables["rooms"]}
        booking_totals={}
        for booking in tables["bookings"]:
            nights=(date.fromisoformat(booking["end_date"])-date.fromisoformat(booking["start_date"])).days
            booking["total"]=nights*room_rates[booking["room_id"]]
            booking_totals[booking["booking_id"]]=booking["total"]
        for payment in tables["payments"]:payment["amount"]=booking_totals[payment["booking_id"]]
    if config.domain=="library":
        loan_dates={row["loan_id"]:date.fromisoformat(row["loan_date"]) for row in tables["loans"]}
        for returned in tables["returns"]:
            returned["return_date"]=(loan_dates[returned["loan_id"]]+timedelta(days=int(rng.integers(1,22)))).isoformat()
    return tables,{"missing":[],"outlier":[],"duplicate":[]}


def validate(tables: dict[str,list[dict]], config: Config, flags: dict) -> dict:
    all_rows = [r for rows in tables.values() for r in rows]
    cells = [v for r in all_rows for v in r.values()]
    completeness = round(100*sum(v is not None and v != "" for v in cells)/max(1,len(cells)))
    duplicate_count = len(flags.get("duplicate",[]))
    uniqueness = round(100*(1-duplicate_count/max(1,config.rows)))
    checks = []
    referential = 100
    if "orders" in tables:
        customer_ids = {r["customer_id"] for r in tables["customers"]}
        order_ids = {r["order_id"] for r in tables["orders"]}
        product_ids = {r["product_id"] for r in tables["products"]}
        orphan = sum(r["customer_id"] not in customer_ids for r in tables["orders"])
        orphan += sum(r["order_id"] not in order_ids or r["product_id"] not in product_ids for r in tables["order_items"])
        totals = defaultdict(int)
        for item in tables["order_items"]: totals[item["order_id"]] += item["line_total"]
        mismatch = sum(r["total"] != totals[r["order_id"]] for r in tables["orders"])
        checks.extend([{"name":"Foreign keys resolve","passed":orphan==0,"detail":f"{orphan} orphan references"},{"name":"Order totals reconcile","passed":mismatch==0,"detail":f"{mismatch} mismatched orders"}])
        referential = round(100*(1-orphan/max(1,len(tables["order_items"])+len(tables["orders"]))))
    if "accounts" in tables:
        customer_ids={r["customer_id"] for r in tables["customers"]}
        account_ids={r["account_id"] for r in tables["accounts"]}
        orphan=sum(r["customer_id"] not in customer_ids for r in tables["accounts"])
        orphan+=sum(r["account_id"] not in account_ids for r in tables["transactions"])
        mismatch=0
        by_account=defaultdict(list)
        for transaction in tables["transactions"]:by_account[transaction["account_id"]].append(transaction)
        for account in tables["accounts"]:
            balance=account["opening_balance"]
            for transaction in by_account[account["account_id"]]:
                balance+=transaction["credit"]-transaction["debit"]
                mismatch+=balance!=transaction["balance"]
            mismatch+=balance!=account["balance"]
        checks.extend([{"name":"Account foreign keys resolve","passed":orphan==0,"detail":f"{orphan} orphan references"},{"name":"Running balances reconcile","passed":mismatch==0,"detail":f"{mismatch} mismatched balances"}])
        referential=round(100*(1-orphan/max(1,len(tables["transactions"])+len(tables["accounts"]))))
    if config.domain in GENERIC_DOMAINS and config.kind=="relational":
        orphan=0;links=0
        for table,_,fks,_ in GENERIC_DOMAINS[config.domain]:
            for fk,parent in fks.items():
                parent_pk=next(iter(tables[parent][0]));ids={r[parent_pk] for r in tables[parent]}
                orphan+=sum(row[fk] not in ids for row in tables[table]);links+=len(tables[table])
        referential=round(100*(1-orphan/max(1,links)))
        checks.append({"name":"Foreign keys resolve","passed":orphan==0,"detail":f"{orphan} orphan references across {links} links"})
        if config.domain=="hotel":
            booking_totals={r["booking_id"]:r["total"] for r in tables["bookings"]}
            mismatch=sum(p["amount"]!=booking_totals[p["booking_id"]] for p in tables["payments"])
            checks.append({"name":"Booking payments reconcile","passed":mismatch==0,"detail":f"{mismatch} mismatched payments"})
    for table_name, rows in tables.items():
        if rows:
            first_key = next((k for k in rows[0] if k.endswith("_id") or k=="id"),None)
            if first_key:
                unique = len({r[first_key] for r in rows}) == len(rows)
                checks.append({"name":f"{table_name}.{first_key} unique","passed":unique,"detail":f"{len(rows)} rows checked"})
    for rule in config.rules:
        match = re.fullmatch(r"([\w]+)\s*(>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?)",rule.strip())
        if match:
            name, op, bound = match.groups(); bound = float(bound)
            values = [float(r[name]) for r in all_rows if name in r and isinstance(r[name],(int,float))]
            compare = {">=":lambda x:x>=bound,"<=":lambda x:x<=bound,">":lambda x:x>bound,"<":lambda x:x<bound,"==":lambda x:x==bound}[op]
            bad = sum(not compare(x) for x in values)
            checks.append({"name":rule,"passed":bool(values) and bad==0,"detail":f"{bad} violations across {len(values)} values" if values else "Field missing or nonnumeric"})
        else:checks.append({"name":rule,"passed":False,"detail":"Unsupported rule syntax; use a numeric comparison such as age >= 18"})
    compliance = round(100*sum(c["passed"] for c in checks)/max(1,len(checks)))
    format_checked=0;format_valid=0
    if config.kind=="tabular":
        table=next(iter(tables.values()))
        for col in config.columns:
            if col.privacy=="hash":continue
            if col.type not in ("email","phone","date"):continue
            for row in table:
                value=row.get(col.name)
                if value is None:continue
                format_checked+=1
                if col.type=="email":valid=bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",str(value)))
                elif col.type=="phone":valid=bool(re.fullmatch(r"\+\d{1,3}-[\d*]+-[\d*]+",str(value)))
                else:
                    try:date.fromisoformat(str(value));valid=True
                    except ValueError:valid=False
                format_valid+=valid
    validity=round(100*format_valid/format_checked) if format_checked else 100
    similarity = None
    comparisons = []
    if config.sample:
        measures=[]
        table=next(iter(tables.values()))
        for col in config.columns:
            a=[]; b=[]
            for r in config.sample:
                try:a.append(float(r[col.name]))
                except (KeyError,ValueError,TypeError):pass
            for r in table:
                try:b.append(float(r[col.name]))
                except (KeyError,ValueError,TypeError):pass
            if len(a)>=5 and b:
                sample_mean=float(np.mean(a)); synthetic_mean=float(np.mean(b))
                score=max(0,100-100*abs(sample_mean-synthetic_mean)/max(abs(sample_mean),1))
                measures.append(score)
                comparisons.append({"field":col.name,"measure":"Mean","sample":round(sample_mean,2),"synthetic":round(synthetic_mean,2),"similarity":round(score)})
            elif col.type=="category":
                sample_counts=Counter(str(r[col.name]) for r in config.sample if r.get(col.name))
                synthetic_counts=Counter(str(r[col.name]) for r in table if r.get(col.name))
                if sample_counts and synthetic_counts:
                    keys=set(sample_counts)|set(synthetic_counts)
                    total_a=sum(sample_counts.values());total_b=sum(synthetic_counts.values())
                    score=max(0,100-50*sum(abs(sample_counts[k]/total_a-synthetic_counts[k]/total_b) for k in keys))
                    measures.append(score)
                    comparisons.append({"field":col.name,"measure":"Category distribution","sample":len(sample_counts),"synthetic":len(synthetic_counts),"similarity":round(score)})
        if measures: similarity=round(float(np.mean(measures)))
    dimensions={"Completeness":completeness,"Accuracy indicators":max(0,100-round(100*len(flags.get("outlier",[]))/max(1,config.rows))),"Consistency":100 if all(c["passed"] for c in checks if "total" in c["name"].lower()) else 0,"Uniqueness":uniqueness,"Validity":validity,"Referential integrity":referential,"Statistical similarity":similarity,"Business-rule compliance":compliance}
    measured=[x for x in dimensions.values() if x is not None]
    return {"score":round(sum(measured)/len(measured)),"dimensions":dimensions,"comparison":comparisons,"checks":checks,"counts":{"rows":sum(map(len,tables.values())),"missing":len(flags.get("missing",[])),"outliers":len(flags.get("outlier",[])),"duplicates":duplicate_count}}


def privacy_scan(config: Config) -> dict:
    fields=[]
    detected=config.columns
    if config.kind=="relational":
        if config.domain in GENERIC_DOMAINS:
            detected=[Column(name=f"{table}.{field}",type="email" if field=="email" else "name",pii=True) for table,_,_,columns in GENERIC_DOMAINS[config.domain] for field in columns if field in ("full_name","email")]
        else:detected=[Column(name="customers.full_name",type="name",pii=True),Column(name="customers.email",type="email",pii=True)]
    elif not detected:
        detected=[Column(name="full_name",type="name",pii=True),Column(name="email",type="email",pii=True)]
    for col in detected:
        if col.pii:
            fields.append({"field":col.name,"treatment":col.privacy,"covered":col.privacy in ("synthetic","mask","hash","noise")})
    return {"fields":fields,"coverage":round(100*sum(f["covered"] for f in fields)/max(1,len(fields))),"blocked":any(not f["covered"] for f in fields),"risk":"High" if any(not f["covered"] for f in fields) else "Low"}


def save_dataset(config: Config, tables: dict, quality: dict, privacy: dict, flags: dict | None = None) -> tuple[str,int]:
    ident=str(uuid.uuid4())
    with connection() as db:
        db.execute("INSERT INTO datasets(id,name,kind) VALUES(?,?,?)",(ident,config.name,config.kind))
        db.execute("INSERT INTO versions(dataset_id,version,config_json,tables_json,quality_json,privacy_json,flags_json) VALUES(?,?,?,?,?,?,?)",(ident,1,config.model_dump_json(),json.dumps(tables,separators=(",",":")),json.dumps(quality),json.dumps(privacy),json.dumps(flags or {})))
    return ident,1


def save_version(ident: str, config: Config, tables: dict, quality: dict, privacy: dict, flags: dict | None = None) -> int:
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT current_version,kind FROM datasets WHERE id=?",(ident,)).fetchone()
        if not current:raise ValueError("Dataset not found")
        if current["kind"]!=config.kind:raise ValueError("A dataset version must use the same generation engine")
        version=current["current_version"]+1
        db.execute("INSERT INTO versions(dataset_id,version,config_json,tables_json,quality_json,privacy_json,flags_json) VALUES(?,?,?,?,?,?,?)",(ident,version,config.model_dump_json(),json.dumps(tables,separators=(",",":")),json.dumps(quality),json.dumps(privacy),json.dumps(flags or {})))
        db.execute("UPDATE datasets SET current_version=?,name=? WHERE id=?",(version,config.name,ident))
    return version


def list_versions(ident: str) -> list[dict]:
    with connection() as db:
        rows=db.execute("SELECT version,config_json,quality_json,created_at FROM versions WHERE dataset_id=? ORDER BY version DESC",(ident,)).fetchall()
    return [{"version":r["version"],"config":json.loads(r["config_json"]),"score":json.loads(r["quality_json"])["score"],"created_at":r["created_at"]} for r in rows]


def get_dataset(ident: str, version: int | None = None) -> dict | None:
    with connection() as db:
        row=db.execute("SELECT d.id,d.name,d.kind,d.created_at,d.current_version,v.version,v.config_json,v.tables_json,v.quality_json,v.privacy_json,v.flags_json FROM datasets d JOIN versions v ON d.id=v.dataset_id WHERE d.id=? AND v.version=COALESCE(?,d.current_version)",(ident,version)).fetchone()
    if not row:return None
    return {"id":row["id"],"name":row["name"],"kind":row["kind"],"created_at":row["created_at"],"version":row["version"],"current_version":row["current_version"],"config":json.loads(row["config_json"]),"tables":json.loads(row["tables_json"]),"quality":json.loads(row["quality_json"]),"privacy":json.loads(row["privacy_json"]),"flags":json.loads(row["flags_json"])}


def list_datasets() -> list[dict]:
    with connection() as db:
        rows=db.execute("SELECT d.id,d.name,d.kind,d.created_at,d.current_version,v.quality_json,v.tables_json FROM datasets d JOIN versions v ON d.id=v.dataset_id AND d.current_version=v.version ORDER BY d.created_at DESC LIMIT 100").fetchall()
    return [{"id":r["id"],"name":r["name"],"kind":r["kind"],"created_at":r["created_at"],"version":r["current_version"],"score":json.loads(r["quality_json"])["score"],"rows":sum(map(len,json.loads(r["tables_json"]).values()))} for r in rows]


def make_document(dataset: dict, kind: str) -> dict:
    tables=dataset["tables"]
    if kind=="invoice":
        if "orders" not in tables: raise ValueError("Generate a relational e-commerce dataset for invoices")
        order=tables["orders"][0]; customer=next(x for x in tables["customers"] if x["customer_id"]==order["customer_id"])
        items=[x for x in tables["order_items"] if x["order_id"]==order["order_id"]]
        return {"type":"invoice","number":"INV-"+order["order_id"].split("-")[1],"date":order["order_date"],"customer":customer["full_name"],"currency":dataset["config"]["currency"],"template":retrieve_template(dataset["config"].get("locale","en_US"),"invoice"),"items":items,"total":order["total"]}
    if kind=="statement":
        if "accounts" not in tables: raise ValueError("Generate a relational banking dataset for statements")
        account=next((a for a in tables["accounts"] if any(t["account_id"]==a["account_id"] for t in tables["transactions"])),tables["accounts"][0])
        transactions=[{"date":t["transaction_date"],"description":t["description"],"debit":t["debit"],"credit":t["credit"],"balance":t["balance"]} for t in tables["transactions"] if t["account_id"]==account["account_id"]]
        return {"type":"statement","account":"••••"+account["account_id"][-4:],"currency":dataset["config"]["currency"],"template":retrieve_template(dataset["config"].get("locale","en_US"),"statement"),"opening_balance":account["opening_balance"],"transactions":transactions,"closing_balance":account["balance"]}
    raise ValueError("Unknown document type")


def document_export_bytes(document: dict, fmt: str) -> tuple[bytes,str,str]:
    kind=document["type"]
    if fmt=="json":return json.dumps(document,ensure_ascii=False,indent=2).encode(),"application/json",kind+".json"
    rows=document["items"] if kind=="invoice" else document["transactions"]
    if fmt=="csv":
        output=io.StringIO(newline="");writer=csv.DictWriter(output,fieldnames=list(rows[0]) if rows else [])
        if rows:writer.writeheader();writer.writerows(rows)
        return output.getvalue().encode(),"text/csv",kind+".csv"
    if fmt=="txt":
        if kind=="invoice":
            lines=[document["template"]["title"]+" "+document["number"],"Billed to: "+document["customer"],"Issued: "+document["date"],"", "ITEM | QTY | UNIT PRICE | AMOUNT"]
            lines += [f"{r['item']} | {r['qty']} | {r['unit_price']} | {r['line_total']}" for r in rows]
            lines += ["",f"TOTAL {document['currency']} {document['total']}"]
        else:
            lines=[document["template"]["title"]+" · Account "+document["account"],f"Opening balance: {document['opening_balance']}","","DATE | DESCRIPTION | DEBIT | CREDIT | BALANCE"]
            lines += [f"{r['date']} | {r['description']} | {r['debit']} | {r['credit']} | {r['balance']}" for r in rows]
            lines += ["",f"CLOSING BALANCE {document['currency']} {document['closing_balance']}"]
        return "\n".join(lines).encode("utf-8"),"text/plain",kind+".txt"
    if fmt=="pdf":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        output=io.BytesIO();c=canvas.Canvas(output,pagesize=A4);w,h=A4
        c.setTitle("Hypersion Gen "+kind.title())
        c.setFillColorRGB(.06,.43,.35);c.setFont("Helvetica-Bold",24);c.drawString(50,h-62,document["template"]["title"])
        c.setFillColorRGB(.15,.24,.25);c.setFont("Helvetica",10)
        subtitle=("#"+document["number"]+"  |  "+document["customer"]+"  |  "+document["date"]) if kind=="invoice" else ("Account "+document["account"]+"  |  Opening balance "+str(document["opening_balance"]))
        c.drawString(50,h-82,subtitle)
        y=h-120
        for row in rows:
            if y<70:c.showPage();y=h-60
            if kind=="invoice":line=f"{row['item'][:29]:29}   {row['qty']} x {row['unit_price']:,.2f}  =  {row['line_total']:,.2f}"
            else:line=f"{row['date']}  {row['description'][:24]:24}  debit {row['debit']:,.2f}  balance {row['balance']:,.2f}"
            c.drawString(50,y,line);y-=22
        c.setStrokeColorRGB(.75,.83,.81);c.line(50,y-7,w-50,y-7)
        c.setFont("Helvetica-Bold",14);c.drawRightString(w-50,y-31,f"{document['currency']} {document['total'] if kind=='invoice' else document['closing_balance']:,.2f}")
        c.save();return output.getvalue(),"application/pdf",kind+".pdf"
    raise ValueError("Unsupported document export format")


def export_bytes(dataset: dict, fmt: str) -> tuple[bytes,str,str]:
    if dataset["privacy"]["blocked"]: raise PermissionError("Sensitive fields without treatment block export. Set each field to synthetic, mask, hash, or noise.")
    tables=dataset["tables"]
    name=re.sub(r"[^a-z0-9_-]+","_",dataset["name"].lower()).strip("_") or "dataset"
    def csv_bytes(rows):
        output=io.StringIO(newline=""); writer=csv.DictWriter(output,fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            for row in rows:
                writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v) for k,v in row.items()})
        return output.getvalue().encode("utf-8")
    if fmt=="json": return json.dumps(tables,ensure_ascii=False,indent=2).encode(),"application/json",name+".json"
    if fmt=="csv":
        rows=next(iter(tables.values()))
        return csv_bytes(rows),"text/csv",name+".csv"
    if fmt=="txt":
        body="\n\n".join(table.upper()+"\n"+"\n".join(" | ".join(str(v) if v is not None else "missing" for v in row.values()) for row in rows) for table,rows in tables.items())
        return body.encode(),"text/plain",name+".txt"
    if fmt=="sql":
        def identifier(value:str)->str:return '"'+value.replace('"','""')+'"'
        lines=[]
        for table,rows in tables.items():
            if not rows:continue
            cols=list(rows[0]); lines.append(f"CREATE TABLE {identifier(table)} ("+", ".join(f"{identifier(c)} TEXT" for c in cols)+");")
            for row in rows:
                vals=["NULL" if row[c] is None else "'"+str(row[c]).replace("'","''")+"'" for c in cols]
                lines.append(f"INSERT INTO {identifier(table)} ("+", ".join(identifier(c) for c in cols)+") VALUES ("+", ".join(vals)+");")
        return "\n".join(lines).encode(),"application/sql",name+".sql"
    if fmt=="zip":
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
            for table,rows in tables.items():archive.writestr(table+".csv",csv_bytes(rows))
            archive.writestr("metadata.json",json.dumps({"config":dataset["config"],"quality":dataset["quality"],"privacy":dataset["privacy"]},indent=2))
            archive.writestr("README.txt","Hypersion Gen synthetic dataset. Generated records are for testing only.\n")
        return output.getvalue(),"application/zip",name+".zip"
    if fmt=="pdf":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        output=io.BytesIO(); c=canvas.Canvas(output,pagesize=A4); w,h=A4
        c.setFont("Helvetica-Bold",20);c.drawString(50,h-60,"Hypersion Gen | Dataset Report")
        c.setFont("Helvetica",11);c.drawString(50,h-88,dataset["name"])
        y=h-120
        for line in [f"Version: {dataset['version']}",f"Quality score: {dataset['quality']['score']}/100",f"Privacy coverage: {dataset['privacy']['coverage']}%"]:
            c.drawString(50,y,line);y-=22
        y-=15
        for table,rows in tables.items():
            if y<80:c.showPage();y=h-60
            c.setFont("Helvetica-Bold",12);c.drawString(50,y,f"{table} | {len(rows)} rows");y-=20
            c.setFont("Helvetica",8)
            for row in rows[:8]:
                if y<50:c.showPage();y=h-50
                c.drawString(50,y,"  |  ".join(f"{k}: {str(v)[:18]}" for k,v in list(row.items())[:4])[:115]);y-=15
            y-=18
        c.save();return output.getvalue(),"application/pdf",name+"_report.pdf"
    raise ValueError("Unsupported export format")
