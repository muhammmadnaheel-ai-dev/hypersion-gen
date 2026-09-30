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

Open http://localhost:5173 (or http://localhost:5174 if the port is already in use). The backend runs at http://localhost:8000 and exposes OpenAPI at `/docs`.

## Deploy to Vercel

Import `muhammmadnaheel-ai-dev/hypersion-gen` into Vercel using the repository root. `vercel.json` builds the Vite frontend, while `pyproject.toml` points Vercel to the FastAPI app. The app mounts the built `dist` frontend and routes API requests through the same deployment.

Before deploying, add these project environment variables for both Preview and Production: `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_APP_ID`, `FIREBASE_SERVICE_ACCOUNT_JSON`, and `SUPABASE_DB_URL`. `OPENROUTER_API_KEY` is optional. Frontend `VITE_*` values are embedded at build time, so redeploy after changing them. Keep the Firebase service-account JSON and Supabase Postgres connection string server-side; never use them as `VITE_*` variables. The backend refuses to use SQLite on Vercel, so configure Supabase Postgres for persistent datasets and credits. Keep Easypaisa checkout disabled until its verified merchant integration is implemented.

The application works without an AI key using local prompt interpretation. To enable OpenRouter analysis, open `backend/.env` and set `OPENROUTER_API_KEY` to your key. You can choose a model with `OPENROUTER_MODEL`. Restart the backend after editing the file, then check `http://127.0.0.1:8000/api/integrations`; `openrouter.configured` should be `true`. That status endpoint never returns the key. Do not enter the key in the browser or commit `backend/.env` to source control.

For Supabase persistence, install `backend/requirements.txt`, then copy the PostgreSQL connection string from your Supabase project's **Connect** panel into `SUPABASE_DB_URL` in `backend/.env`. Use the Session pooler when your machine cannot reach the direct IPv6 address. Restart the backend; it creates the datasets, versions, and credit ledger tables on startup. The connection uses TLS and works with Supabase's transaction pooler. Check `/api/integrations` or the Admin Panel to confirm the active database. When the variable is unset, local SQLite remains the default. Existing SQLite datasets are not migrated automatically. Keep the connection string on the backend and never commit `backend/.env` or add it to frontend variables.

Email/password accounts, password reset, email-link sign-in, and Google sign-in use Firebase Authentication. In Firebase Console, enable Email/Password and Google under Authentication > Sign-in method, and add the Vercel domain under Authentication > Settings > Authorized domains. Firebase email/password signup does not send a confirmation email unless verification is explicitly enabled. For local development, set `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, and `VITE_FIREBASE_APP_ID` in `.env.local`. For the backend, create a Firebase service account and set its JSON as `FIREBASE_SERVICE_ACCOUNT_JSON` in `backend/.env` and Vercel; never expose it in the browser or commit it. The backend verifies Firebase ID tokens on protected API requests; `/api/health` remains public. Supabase is used only for Postgres persistence: dataset reads and writes still require `SUPABASE_DB_URL`. Existing Supabase-owned datasets and credit balances are transferred to a Firebase UID only when Firebase asserts a verified email that uniquely matches a confirmed Supabase Auth account. New unverified email/password accounts start with their own data; Google accounts normally have verified email claims. On startup, the backend enables Row Level Security on its tables and revokes access from browser roles.

Firebase handles auth email delivery for password reset and email-link sign-in. These email flows require the Firebase Authentication email provider to be enabled.

This is a prototype. Upload only non-sensitive samples: up to 200 sample rows are retained in the dataset version to reproduce its statistics. Do not use it with production records until a privacy review is complete.

The Free plan grants 100 credits. Generation costs 1 credit per 1,000 rows, rounded up, including regenerated versions. Paid credit packages are Starter (PKR 500 / 500 credits), Pro (PKR 1,500 / 2,000 credits), and Premium (PKR 3,000 / 5,000 credits). Balances and charges are stored per signed-in user, and dataset writes and their credit debits are atomic. Easypaisa checkout is intentionally disabled: the merchant-specific integration guide was not found in this workspace. Add the official guide (without secrets) before enabling live purchases; payment confirmation must be verified server-side before granting purchased credits.

## Implemented scope

- CSV sample and natural-language schema interpretation; editable columns and privacy treatment
- Seeded NumPy/SciPy tabular data, eight built-in relational domains (e-commerce, banking, hospital, university, inventory, HR, hotel and library), configurable nulls, outliers and duplicates
- Computed quality dimensions, business-rule checks, privacy gate and paginated preview
- Invoice and statement views derived from generated rows
- Server-side CSV, JSON, TXT, PDF report, SQL and ZIP export
- Version history with deterministic regeneration and previous-version inspection
- Dashboard, generation flow, DataLens, relationships, scenarios, documents, export and supporting workspace screens

Easypaisa checkout, database connectors, durable background jobs, and custom relational schema editing remain outside this MVP. OpenRouter is optional; when it is unavailable, the local interpreter proposes a schema instead.

Run backend checks with `cd backend; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`. Run the frontend production build with `npm run build`.
