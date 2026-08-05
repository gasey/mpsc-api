# MPSC API Backend

Django REST API for 73,405 MPSC exam questions + verification system.

## Quick Start

**Local development:**

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Create .env from .env.example and configure
cp .env.example .env

# Run migrations
python manage.py migrate

# Load questions from JSON
python manage.py load_questions /path/to/mpsc_bank_converted.json

# Start dev server
python manage.py runserver
```

**API is at:** http://localhost:8000/api/

## Database Schema

**Questions Table:**
- `question_id` - Unique identifier
- `stem` - Question text
- `options` - JSON array [A, B, C, D]
- `answer_index` - Correct answer (0-3)
- `answer_source` - 'official' or 'derived'
- `paper_id` - Which exam paper
- `has_diagram` - True if has diagram image
- `diagram_image` - Path to PNG file

**VerificationResult Table:**
- `question` - FK to Question
- `status` - pending / reviewed / ambiguous
- `marked_answer` - Original marked answer
- `model_answer` - What AI model said
- `your_decision` - Your verified answer
- `your_reasoning` - Why you chose it

## API Endpoints

```
GET    /api/questions/              # List all questions
GET    /api/questions/{id}/         # Get one question
GET    /api/questions/stats/        # Statistics
GET    /api/questions/with_diagrams/  # Only ones with images

GET    /api/verification/           # List verifications
GET    /api/verification/pending/   # Pending reviews
POST   /api/verification/{id}/mark_reviewed/  # Save your review
```

## Deploy to Render

See `DEPLOY_TO_RENDER.md` for step-by-step instructions.

## Bulk Load Questions

Load all 73,405 questions:

```bash
python manage.py load_questions /path/to/mpsc_bank_converted.json
```

Takes ~2 minutes on local SQLite, faster on PostgreSQL.

---

API ready to use with your React frontend!
