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

# OpenAI - Whisper transcription + GPT chat (required)
OPENAI_API_KEY=sk-...

# OpenRouter - Gemini extraction (required)
OPENROUTER_API_KEY=sk-or-...

# Clerk Auth (required)
CLERK_FRONTEND_API=your-clerk-domain.clerk.accounts.dev

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
```

## Error Monitoring (Sentry)

Sentry captures errors, performance data, and Instagram auth failures.

### Setup
1. Create a Sentry project for FastAPI (`hafa-recipes-api`)
2. Copy the DSN to your `.env` file
3. Add `SENTRY_DSN` to Render environment variables for production

### Testing
Visit `http://localhost:8000/sentry-debug` to trigger a test error.

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
    → Gemini extracts recipe → Thumbnail uploaded to S3 → Saved to PostgreSQL
```

### Website Extraction
```
User pastes website URL → Fetch HTML → Parse JSON-LD (or AI fallback)
    → Detect ingredient sections (WPRM/Tasty Recipes/Hearst Media)
    → Split combined steps → Thumbnail uploaded to S3 → Saved to PostgreSQL
```

Supported sites: AllRecipes, Budget Bytes, Half Baked Harvest, Delish, Pinch of Yum, Sally's Baking, and hundreds more.

**AI Stack:**
| Task | Model |
|------|-------|
| Transcription | OpenAI Whisper |
| Recipe Extraction (Video) | Gemini 2.0 Flash (primary), GPT-4o-mini (fallback) |
| Recipe Extraction (Website) | JSON-LD parsing (primary), GPT-4o-mini (fallback) |
| Recipe Extraction (OCR) | Gemini 2.0 Flash Vision (primary), GPT-4o Vision (fallback) |
| Recipe Chat | GPT-4o |
| Tag/Nutrition AI | GPT-4o-mini |

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
    ├── llm_client.py # Gemini/GPT extraction
    ├── openai_client.py  # Whisper + chat
    └── storage.py    # S3 uploads
```

## API Endpoints

### Extraction
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/extract/async` | Start extraction job (video URL) |
| POST | `/api/extract/ocr` | Extract from single image (OCR) |
| POST | `/api/extract/ocr/multi` | Extract from multiple images (OCR) |
| POST | `/api/re-extract/{id}/async` | Re-extract with latest AI (owner/admin) |
| GET | `/api/jobs/{id}` | Get job status |
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

Admins can re-extract any recipe. Set via Clerk:

1. **Clerk Dashboard** → Users → Select user
2. **Public metadata** → Add:
   ```json
   { "role": "admin" }
   ```

3. **JWT Template** → Create with claim:
   ```json
   { "public_metadata": "{{user.public_metadata}}" }
   ```

## Database Migrations

```bash
# Create migration
alembic revision --autogenerate -m "description"

# Run migrations
alembic upgrade head
```

## Deployment (Render)

1. Connect GitHub repo to Render
2. Set environment variables in dashboard
3. Auto-deploys on push to `main`

**Build Command:** `pip install -r requirements.txt`  
**Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## License

Private - Shimizu Technology

