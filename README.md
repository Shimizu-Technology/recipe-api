# Recipe Extractor API

FastAPI backend for extracting structured recipes from cooking videos and recipe websites using AI.

## Quick Start

```bash
# Install dependencies (creates .venv automatically)
uv sync

# Copy environment template
cp .env.example .env
# Edit .env with your credentials

# Run the server
uv run uvicorn app.main:app --reload --host 0.0.0.0

# Run the complete local/CI verification gate
./scripts/gate.sh

# Or activate venv and run directly
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0
```

Server runs at `http://localhost:8000`

## Environment Variables

Create a `.env` file:

```bash
# Database (required)
DATABASE_URL=postgresql://user:pass@host/dbname

# OpenAI - pinned recipe AI, transcription, and speech (required)
OPENAI_API_KEY=sk-...

# Routine work uses Luna. Terra is the deterministic extraction/OCR fallback.
RECIPE_EXTRACTION_MODEL=gpt-5.6-luna
RECIPE_EXTRACTION_FALLBACK_MODEL=gpt-5.6-terra
OCR_MODEL=gpt-5.6-luna
OCR_FALLBACK_MODEL=gpt-5.6-terra
RECIPE_CHAT_MODEL=gpt-5.6-luna
COOKING_CHAT_MODEL=gpt-5.6-luna
ENRICHMENT_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=none
# Emergency provider kill switch: recipe_chat,ocr or all
AI_DISABLED_CAPABILITIES=

# Clerk Auth (required)
CLERK_FRONTEND_API=your-clerk-domain.clerk.accounts.dev
CLERK_SECRET_KEY=sk_live_...              # required for server-side account deletion
# CLERK_JWT_ISSUER=https://your-clerk-domain.clerk.accounts.dev
# CLERK_JWT_AUDIENCE=hafa-recipes-api

# AWS S3 - thumbnail storage (recommended)
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
S3_BUCKET_NAME=your-bucket-name

# Instagram Authentication (for video extraction)
# Required for Instagram videos - export cookies from logged-in browser
# Can be raw cookie content or path to cookies.txt file
INSTAGRAM_COOKIES=# Netscape HTTP Cookie File...

# Sentry Error Monitoring (optional but recommended)
# Get DSN from: Sentry Dashboard → hafa-recipes-api → Settings → Client Keys
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx

# Optional
ENVIRONMENT=development
# CORS_ORIGINS=https://hafa-recipes.com,https://www.hafa-recipes.com
# ENABLE_SENTRY_DEBUG=false
```

## Error Monitoring (Sentry)

Sentry captures errors, performance data, and Instagram auth failures.

### Setup
1. Create a Sentry project for FastAPI (`hafa-recipes-api`)
2. Copy the DSN to your `.env` file
3. Add `SENTRY_DSN` to Render environment variables for production

### Testing
Visit `http://localhost:8000/sentry-debug` to trigger a test error in development. In non-development environments, set `ENABLE_SENTRY_DEBUG=true` temporarily before testing.

### What's Monitored
- All unhandled exceptions
- Instagram extraction failures (tagged with `platform:instagram`)
- API performance (20% sampled)

## Instagram Cookie Setup

Instagram requires authentication to extract videos. To enable:

1. **Install browser extension**: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/)
2. **Log into Instagram** in your browser
3. **Go to instagram.com** and export cookies with the extension
4. **Add to Render**: Paste entire content as `INSTAGRAM_COOKIES` environment variable

**Expiration**: Cookies last ~1 year. Refresh when you see "login required" errors in logs.

**Security**: Use a dedicated Instagram account if concerned about flagging.

## How It Works

### Video Extraction
```
User pastes video URL → yt-dlp downloads audio → Whisper transcribes
    → Luna extracts recipe (Terra fallback) → Thumbnail uploaded to S3 → Saved to PostgreSQL
```

### Website Extraction
```
User pastes website URL → Fetch HTML → Parse JSON-LD (or AI fallback)
    → Detect ingredient sections (WPRM/Tasty Recipes/Hearst Media)
    → Split combined steps → Thumbnail uploaded to S3 → Saved to PostgreSQL
```

### Durable async extraction

`POST /api/extract/async` and `POST /api/re-extract/{id}/async` persist the
complete request before returning. A database-backed worker moves each job
through `queued → claimed → processing → completed`, using row locks, renewable
leases, bounded retries, and stale-lease recovery so deploys do not lose work.
Terminal states are `completed`, `failed`, `cancelled`, and `expired`.

Clients should send a new UUID in the `Idempotency-Key` header for each user
action, retain the returned job ID, and poll `GET /api/jobs/{id}` until any
terminal state. Retrying the same request with the same key returns the original
job; reusing a key for a different payload returns `409`.

Supported sites: AllRecipes, Budget Bytes, Half Baked Harvest, Delish, Pinch of Yum, Sally's Baking, and hundreds more.

**AI Stack:**
| Task | Model |
|------|-------|
| Transcription | `whisper-1` |
| Recipe Extraction (Video) | GPT-5.6 Luna (routine), GPT-5.6 Terra (fallback) |
| Recipe Extraction (Website) | JSON-LD parsing (primary), Luna/Terra AI fallback |
| Recipe Extraction (OCR) | GPT-5.6 Luna (routine), GPT-5.6 Terra (fallback) |
| Recipe and Cooking Chat | GPT-5.6 Luna |
| Tag/Nutrition AI | GPT-5.6 Luna |
| Text-to-Speech | `tts-1` |

Model IDs are environment-pinned rather than provider aliases. Routine AI uses
`reasoning_effort=none`; Terra is not called unless extraction/OCR needs a
fallback. `AI_DISABLED_CAPABILITIES` can stop one paid capability (or `all`)
without a deploy. Chat inputs are bounded, image bytes are decoded and checked,
and per-user request/concurrency limits protect provider spend.

## Project Structure

```
app/
├── main.py           # FastAPI app entry point
├── auth.py           # Clerk JWT verification
├── config.py         # Settings from environment
├── db/               # Database connection
├── models/           # SQLAlchemy models
├── routers/          # API endpoints
│   ├── extract.py    # Extraction & job status
│   ├── recipes.py    # CRUD, search, share, chat
│   ├── grocery.py    # Grocery list management
│   ├── collections.py
│   └── meal_plans.py # Meal planning
└── services/         # Business logic
    ├── extractor.py  # Main extraction orchestrator
    ├── video.py      # yt-dlp audio download
    ├── website.py    # Website recipe extraction (JSON-LD, HTML parsing)
    ├── llm_client.py # Luna/Terra extraction and OCR
    ├── openai_client.py  # Whisper + direct extraction
    └── storage.py    # S3 uploads
```

## API Endpoints

### Extraction
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/extract/async` | Start extraction job (authenticated) |
| POST | `/api/extract/ocr` | Extract from single image (authenticated) |
| POST | `/api/extract/ocr/multi` | Extract from multiple images (authenticated) |
| POST | `/api/re-extract/{id}/async` | Re-extract with latest AI (owner/admin) |
| GET | `/api/jobs/{id}` | Get job status (owner-scoped) |
| GET | `/api/locations` | Available cost locations |

### Recipes
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/recipes` | List user's recipes |
| GET | `/api/recipes/discover` | Public recipes |
| GET | `/api/recipes/{id}` | Get single recipe |
| GET | `/api/recipes/search?q=` | Search recipes |
| POST | `/api/recipes/manual` | Create manual recipe |
| PATCH | `/api/recipes/{id}` | Edit recipe |
| DELETE | `/api/recipes/{id}` | Delete recipe |
| POST | `/api/recipes/{id}/share` | Toggle public sharing |
| POST | `/api/recipes/{id}/chat` | AI chat about recipe |
| POST | `/api/recipes/{id}/save` | Bookmark recipe |
| DELETE | `/api/recipes/{id}/save` | Remove bookmark |
| POST | `/api/recipes/{id}/restore` | Restore original version |

New recipes are private by default across extraction, OCR, and manual creation.
Publishing is an explicit user action through the share controls. Public recipe
responses expose a stable `contributor_id` (`chef_...`) and `is_owner` instead
of exposing another user's Clerk subject. The legacy `user_id` field remains
temporarily available for client compatibility, but contains the opaque public
contributor ID unless the authenticated viewer owns the recipe. Public detail
responses also omit extraction source text; owners still receive their own
source text for editing and diagnostics.

### Personal Notes
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/recipes/{id}/notes` | Get your note for a recipe |
| PUT | `/api/recipes/{id}/notes` | Create/update your note |
| DELETE | `/api/recipes/{id}/notes` | Delete your note |

### Version History
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/recipes/{id}/versions` | List all versions |
| GET | `/api/recipes/{id}/versions/{vid}` | Get specific version |
| POST | `/api/recipes/{id}/versions/{vid}/restore` | Restore to version |

### Grocery List
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/grocery` | Get grocery list |
| POST | `/api/grocery` | Add item |
| POST | `/api/grocery/from-recipe` | Add from recipe |
| PUT | `/api/grocery/{id}/toggle` | Toggle checked |
| DELETE | `/api/grocery/{id}` | Delete item |

### Collections
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/collections` | List collections |
| POST | `/api/collections` | Create collection |
| POST | `/api/collections/{id}/recipes` | Add recipe |
| DELETE | `/api/collections/{id}/recipes/{rid}` | Remove recipe |

### Meal Planning
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/meal-plans/week` | Get week's meal plan |
| GET | `/api/meal-plans/day` | Get day's meal plan |
| POST | `/api/meal-plans/` | Add meal to plan |
| PUT | `/api/meal-plans/{id}` | Update meal entry |
| DELETE | `/api/meal-plans/{id}` | Remove meal |
| DELETE | `/api/meal-plans/day/{date}` | Clear day |
| POST | `/api/meal-plans/to-grocery` | Add plan to grocery |
| POST | `/api/meal-plans/copy-week` | Copy week |

## Admin Setup

Admins can re-extract any recipe and read bounded operational diagnostics. Set
the role via Clerk:

1. **Clerk Dashboard** → Users → Select user
2. **Public metadata** → Add:
   ```json
   { "role": "admin" }
   ```

3. **JWT Template** → Create with claim:
   ```json
   { "public_metadata": "{{user.public_metadata}}" }
   ```

### Health and diagnostics

- `GET /up` is the public, dependency-free liveness endpoint. Configure Render's
  health check to use this path so a temporary database outage does not create a
  restart loop. `GET /health` remains a compatibility alias with the same shape.
- `GET /api/admin/diagnostics` requires an authenticated admin. It checks the
  database and reports only bounded configuration state (never credentials,
  provider responses, or database connection strings).

## Database Migrations

This repo uses simple numbered migration scripts in `migrations/` rather than Alembic.

```bash
# Local/dev with uv
PYTHONPATH=. uv run python migrations/015_add_extraction_job_user_id.py
PYTHONPATH=. uv run python migrations/016_add_clerk_user_migration_tables.py
PYTHONPATH=. uv run python migrations/017_add_durable_extraction_jobs.py

# Render shell/one-off job, using the service's installed environment
PYTHONPATH=. python migrations/015_add_extraction_job_user_id.py
PYTHONPATH=. python migrations/016_add_clerk_user_migration_tables.py
PYTHONPATH=. python migrations/017_add_durable_extraction_jobs.py
```

Run migrations intentionally for each environment; do not run production migrations from a local shell unless you have confirmed the target database.

Current production-required migration:

- `migrations/015_add_extraction_job_user_id.py` — required by `/api/extract/async` because extraction jobs are now user-owned.
- `migrations/016_add_clerk_user_migration_tables.py` — required before the Clerk production cutover migration bridge can import/link legacy users.
- `migrations/017_add_durable_extraction_jobs.py` — required before enabling the durable extraction worker. Apply this expand-only, idempotent migration before deploying the worker code, verify `/api/admin/diagnostics` reports queue counts, then deploy with `JOB_WORKER_ENABLED=true`. Roll back the worker by setting `JOB_WORKER_ENABLED=false` and redeploying; the migration and job history can remain in place for a later retry.

See `docs/PRODUCTION_EXTRACTION_MIGRATION_015_RUNBOOK.md` for the production extraction failure/runbook.
See `docs/CLERK_PROD_CUTOVER_RUNBOOK.md` for the Clerk production cutover runbook.

## Deployment (Render)

1. Connect GitHub repo to Render
2. Set environment variables in dashboard
3. Set the service health-check path to `/up`
4. Auto-deploys on push to `main`

**Build Command:** `pip install -r requirements.txt`  
**Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## License

Private - Shimizu Technology
