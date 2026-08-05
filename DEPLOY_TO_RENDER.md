# Deploy MPSC Backend to Render

## Prerequisites
- Render.com account (free tier)
- GitHub account
- This repo pushed to GitHub

## Step 1: Create Render PostgreSQL Database

1. Go to https://render.com/dashboard
2. Click **New +** → **PostgreSQL**
3. Name: `mpsc-db`
4. Region: Choose closest to you
5. Click **Create Database**
6. Copy the **External Database URL** (you'll need this)

## Step 2: Create Render Web Service

1. Push this repo to GitHub
2. Go to https://render.com/dashboard
3. Click **New +** → **Web Service**
4. Connect your GitHub repo
5. Settings:
   - **Name:** `mpsc-api`
   - **Environment:** `Python 3.11`
   - **Build Command:** `pip install -r requirements.txt && python manage.py migrate`
   - **Start Command:** `gunicorn mpsc_api.wsgi`
6. Click **Create Web Service**

## Step 3: Configure Environment Variables

In Render dashboard, go to your service's **Environment** tab and add:

```
SECRET_KEY=<generate a random string>
DEBUG=False
ALLOWED_HOSTS=mpsc-api.onrender.com,yourdomain.com
DB_NAME=mpsc_db
DB_USER=postgres
DB_PASSWORD=<from postgres service>
DB_HOST=<from postgres service>
DB_PORT=5432
CORS_ALLOWED_ORIGINS=http://localhost:3000,https://yourdomain.com
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
```

## Step 4: Load Questions Data

Once deployed:

```bash
# SSH into Render service
# Run management command to load JSON:
python manage.py load_questions /path/to/mpsc_bank_converted.json
```

Or create a script to load from remote URL.

## Step 5: Test API

```bash
curl https://mpsc-api.onrender.com/api/questions/stats/
# Should return: {"total_questions": 73405, "with_diagrams": 737, ...}
```

## API Endpoints

- `GET /api/questions/` - List all questions (paginated, 100 per page)
- `GET /api/questions/?paper_id=xxx` - Filter by paper
- `GET /api/questions/?topic=xxx` - Filter by topic  
- `GET /api/questions/with_diagrams/` - Only questions with diagrams
- `GET /api/questions/stats/` - Get stats
- `GET /api/verification/pending/` - Pending verifications
- `POST /api/verification/{id}/mark_reviewed/` - Mark verification as reviewed

## Troubleshooting

**Database migration fails:**
- Check that DB_HOST is the Internal Postgres URL, not External
- Ensure ALLOWED_HOSTS includes your Render domain

**Questions not loading:**
- Run migrations: `python manage.py migrate`
- Load data: `python manage.py load_questions`

**CORS errors:**
- Add your frontend domain to CORS_ALLOWED_ORIGINS

## Next: Load Questions

Create `questions/management/commands/load_questions.py` to bulk-import from JSON.

Your API is now live! 🚀
