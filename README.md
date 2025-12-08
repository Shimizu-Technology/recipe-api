# Recipe Extractor API

FastAPI backend for extracting structured recipes from cooking videos using AI.

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

# Optional
ENVIRONMENT=development
```

## How It Works

```
User pastes URL → yt-dlp downloads audio → Whisper transcribes
    → Gemini extracts recipe → Thumbnail uploaded to S3 → Saved to PostgreSQL
```

**AI Stack:**
| Task | Model |
|------|-------|
| Transcription | OpenAI Whisper |
| Recipe Extraction | Gemini 2.0 Flash (primary), GPT-4o-mini (fallback) |
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
│   └── collections.py
└── services/         # Business logic
    ├── extractor.py  # Main extraction orchestrator
    ├── video.py      # yt-dlp audio download
    ├── llm_client.py # Gemini/GPT extraction
    ├── openai_client.py  # Whisper + chat
    └── storage.py    # S3 uploads
```

## API Endpoints

### Extraction
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/extract/async` | Start extraction job |
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

