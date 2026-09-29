# Hypersion Gen

Synthetic data workspace for schema analysis, deterministic generation, validation, preview, documents and export.

## Run locally

Use Python 3.11+ and Node 20+.

```powershell
py -3 -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\backend\.venv\Scripts\python.exe -m uvicorn app:app --app-dir backend --reload
```

In a second terminal:

```powershell
npm install
npm run dev
```

Open http://localhost:5173. The backend runs at http://localhost:8000 and exposes OpenAPI at `/docs`.

The application works without an AI key using local prompt interpretation. To enable OpenRouter analysis, open `backend/.env` and set `OPENROUTER_API_KEY` to your key. You can choose a model with `OPENROUTER_MODEL`. Restart the backend after editing the file, then check `http://127.0.0.1:8000/api/integrations`; `openrouter.configured` should be `true`. That status endpoint never returns the key. Do not enter the key in the browser or commit `backend/.env` to source control. SQLite is used for local persistence; a PostgreSQL migration is still required for a multi-user deployment.

This is a local prototype. Upload only non-sensitive samples: up to 200 sample rows are retained in the local dataset version to reproduce its statistics. Do not expose the API beyond localhost or use it with production records until authentication, access control and a privacy review are in place.

## Implemented scope

- CSV sample and natural-language schema interpretation; editable columns and privacy treatment
- Seeded NumPy/SciPy tabular data, eight built-in relational domains (e-commerce, banking, hospital, university, inventory, HR, hotel and library), configurable nulls, outliers and duplicates
- Computed quality dimensions, business-rule checks, privacy gate and paginated preview
- Invoice and statement views derived from generated rows
- Server-side CSV, JSON, TXT, PDF report, SQL and ZIP export
- Version history with deterministic regeneration and previous-version inspection
- Dashboard, generation flow, DataLens, relationships, scenarios, documents, export and supporting workspace screens

Authentication, payments, database connectors, durable background jobs and custom relational schema editing remain outside this local MVP. The UI labels these areas accordingly. OpenRouter is optional; when it is unavailable, the local interpreter proposes a schema instead.

Run backend checks with `cd backend; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`. Run the frontend production build with `npm run build`.
