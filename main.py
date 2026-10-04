import hashlib as _hashlib
import json as _json
import re as _re
import secrets as _secrets
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from db import get_conn, dict_cursor
from learn_routes import router as learn_router
from auth import (
    hash_password,
    verify_password,
    create_token,
    get_current_user,
    require_cap,
    has_cap,
    capabilities_for,
)

app = FastAPI(title="MPSC Study API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(learn_router)


# ============================================================
# Legacy: MPSC Old Questions bank (unchanged — do not break the
# existing frontend module that reads this).
# ============================================================

def row_to_paper(r):
    return {
        "id": r["id"],
        "examType": r["exam_type"],
        "examName": r["exam_name"],
        "post": r["post"],
        "paperNumber": r["paper_number"],
        "paperSubject": r["paper_subject"],
        "year": r["year"],
        "sourceFile": r["source_file"],
    }


def row_to_question(r):
    return {
        "id": r["id"],
        "subject": r["subject"],
        "topic": r["topic"],
        "topicLabel": r["topic_label"],
        "difficulty": r["difficulty"],
        "type": r.get("type", "mcq"),
        "question": r["question"],
        "options": r["options"],
        "answerIndex": r["answer_index"],
        "explanation": r["explanation"],
        "source": r["source"],
        # 88.5% of questions have a NULL year of their own and rely entirely
        # on their paper's year — callers that joined papers and selected
        # COALESCE(p.year, q.year) AS effective_year get that value here;
        # callers that didn't (the legacy /api/mpsc/bank dump) fall back to
        # the raw column, unchanged.
        "year": r.get("effective_year", r["year"]),
        "tags": r["tags"],
        "paperId": r["paper_id"],
        "subparts": r.get("subparts"),
        "figureBased": r.get("figure_based", False),
    }


class LegacyReportIn(BaseModel):
    questionId: str
    issueType: str
    notes: str = ""
    questionText: str = ""
    examName: Optional[str] = None
    examType: Optional[str] = None
    post: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/mpsc/bank")
def get_bank():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM papers ORDER BY year DESC")
        papers = [row_to_paper(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM questions ORDER BY id")
        questions = [row_to_question(r) for r in cur.fetchall()]
        return {"papers": papers, "questions": questions}
    finally:
        conn.close()


@app.get("/api/mpsc/papers")
def list_bank_papers():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT p.*, COALESCE(qc.n, 0) AS question_count FROM papers p "
            "LEFT JOIN (SELECT paper_id, count(*) AS n FROM questions WHERE paper_id IS NOT NULL GROUP BY paper_id) qc "
            "ON qc.paper_id = p.id ORDER BY p.year DESC"
        )
        return {"papers": [{**row_to_paper(r), "questionCount": r["question_count"]} for r in cur.fetchall()]}
    finally:
        conn.close()


# ---- Papers tree (Phase 5). Groups papers into "sittings" using the same
# source_file heuristic the frontend already validated client-side in
# useMpscData.ts's sittingKey() — ported 1:1 rather than redesigned, since
# `post` alone is too noisy (heavy near-duplicate variants) to group on.

def _sitting_key(exam_type, exam_name, post, year, source_file, paper_id=None):
    # source_file is NULL for most of the batch (Departmental/LDE/Competitive
    # entirely, ~80% of Direct) -- id is the paper's own PDF title/filename
    # and carries the same signal, so fall back to it rather than collapsing
    # every paper in an exam_type+examName+post+year bucket into one sitting.
    # Verified against production data 2026-08-10: this took 211 sittings
    # (largest = 138 papers, clearly not a real sitting) to 1506 (largest = 7).
    basis = source_file if (source_file and source_file != "seed") else paper_id
    if not basis:
        return "|".join([exam_type, exam_name, post or "", str(year)])
    base = basis.rsplit("/", 1)[-1]
    title = _re.sub(r"\.pdf$", "", base, flags=_re.I)
    title = _re.sub(r"\.+$", "", title)
    title = _re.sub(r"[\s-]*paper[\s-]*[ivxlcdm\d]+\s*(\d{4})?\s*$", "", title, flags=_re.I)
    title = _re.sub(r"\.+$", "", title)
    title = title.strip().lower()
    return "|".join([exam_type, title, str(year)])


def _natural_key(s):
    s = s or ""
    return [int(part) if part.isdigit() else part.lower() for part in _re.split(r"(\d+)", s)]


@app.get("/api/papers/tree/")
def papers_tree():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT p.*, COALESCE(qc.n, 0) AS question_count FROM papers p "
            "LEFT JOIN (SELECT paper_id, count(*) AS n FROM questions WHERE paper_id IS NOT NULL GROUP BY paper_id) qc "
            "ON qc.paper_id = p.id"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    sittings_by_key = {}
    for r in rows:
        paper = {**row_to_paper(r), "questionCount": r["question_count"]}
        key = _sitting_key(r["exam_type"], r["exam_name"], r["post"], r["year"], r["source_file"], r["id"])
        s = sittings_by_key.get(key)
        if s is None:
            label_parts = [r["exam_name"], r["post"], str(r["year"]) if r["year"] is not None else None]
            s = {
                "key": key,
                "examType": r["exam_type"],
                "examName": r["exam_name"],
                "post": r["post"],
                "year": r["year"],
                "label": " · ".join(p for p in label_parts if p),
                "papers": [],
            }
            sittings_by_key[key] = s
        s["papers"].append(paper)

    for s in sittings_by_key.values():
        s["papers"].sort(key=lambda p: _natural_key(p["paperNumber"]))
        s["totalQuestions"] = sum(p["questionCount"] for p in s["papers"])

    exam_types = {}
    for s in sittings_by_key.values():
        years_map = exam_types.setdefault(s["examType"], {})
        years_map.setdefault(s["year"], []).append(s)

    result = []
    for exam_type in sorted(exam_types.keys()):
        years_map = exam_types[exam_type]
        years_list = []
        for year in sorted(years_map.keys(), key=lambda y: (y is None, -(y or 0))):
            sittings = sorted(years_map[year], key=lambda s: s["label"])
            years_list.append({"year": year, "sittings": sittings})
        result.append({"examType": exam_type, "years": years_list})

    return {"examTypes": result}


# ---- Admin: Papers metadata editing (Phase 6, AdminTable target). Papers
# are extracted from PDFs and sometimes carry a blank/wrong exam_type, post,
# or year — this is the only way to fix that without re-running extraction.
# No create/delete: papers are 1:1 with source PDFs, only their metadata is
# ever wrong, never their existence.

class PaperPatchIn(BaseModel):
    examType: Optional[str] = None
    examName: Optional[str] = None
    post: Optional[str] = None
    paperNumber: Optional[str] = None
    paperSubject: Optional[str] = None
    year: Optional[int] = None


_PAPER_PATCH_COLUMNS = {
    "examType": "exam_type", "examName": "exam_name", "post": "post",
    "paperNumber": "paper_number", "paperSubject": "paper_subject", "year": "year",
}


@app.get("/api/admin/papers/")
def admin_list_papers(
    q: Optional[str] = Query(None),
    missingYear: Optional[bool] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    user: dict = Depends(require_cap("paper.edit")),
):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses, params = [], []
        if q:
            clauses.append("(p.exam_name ILIKE %s OR p.post ILIKE %s OR p.source_file ILIKE %s)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if missingYear:
            clauses.append("p.year IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur.execute(f"SELECT count(*) AS n FROM papers p {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"SELECT p.*, COALESCE(qc.n, 0) AS question_count FROM papers p "
            f"LEFT JOIN (SELECT paper_id, count(*) AS n FROM questions WHERE paper_id IS NOT NULL GROUP BY paper_id) qc "
            f"ON qc.paper_id = p.id {where} ORDER BY p.year DESC NULLS LAST, p.exam_name LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        papers = [{**row_to_paper(r), "questionCount": r["question_count"]} for r in cur.fetchall()]
        return {"total": total, "papers": papers}
    finally:
        conn.close()


@app.patch("/api/admin/papers/{paper_id}")
def admin_update_paper(paper_id: str, body: PaperPatchIn, user: dict = Depends(require_cap("paper.edit"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM papers WHERE id = %s", (paper_id,))
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="Paper not found")

        updates = body.model_dump(exclude_unset=True)
        if updates:
            set_clauses, params = [], []
            for field, value in updates.items():
                set_clauses.append(f"{_PAPER_PATCH_COLUMNS[field]} = %s")
                params.append(value)
            params.append(paper_id)
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE papers SET {', '.join(set_clauses)} WHERE id = %s", params)
            cur2.execute(
                "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, after, note) "
                "VALUES (%s, %s, 'paper_edited', %s, %s, %s, %s)",
                ("_admin", f"paper:{paper_id}", user["id"],
                 _json.dumps(row_to_paper(before)), _json.dumps({**row_to_paper(before), **updates}),
                 f"{user['username']} edited paper {paper_id}"),
            )
            conn.commit()

        cur.execute("SELECT p.*, COALESCE(qc.n, 0) AS question_count FROM papers p "
                     "LEFT JOIN (SELECT paper_id, count(*) AS n FROM questions WHERE paper_id IS NOT NULL GROUP BY paper_id) qc "
                     "ON qc.paper_id = p.id WHERE p.id = %s", (paper_id,))
        r = cur.fetchone()
        return {**row_to_paper(r), "questionCount": r["question_count"]}
    finally:
        conn.close()


# ---- Server-side filtering/pagination for the Question Bank (Phase 4).
# The full-dump /api/mpsc/bank above is kept working unchanged for whatever
# still depends on it; everything below is additive.

_QUESTIONS_FROM = "FROM questions q LEFT JOIN papers p ON p.id = q.paper_id"


def _question_filters(examType, post, year, paperId, subject, difficulty, qtype, search):
    """LEFT JOIN (not inner) + COALESCE(p.year, q.year) matter here: ~2,324
    questions have no paper_id, and must stay visible under a year filter
    (via their own q.year) while correctly dropping out of any examType/post
    filter (paper-less rows have no p.exam_type/p.post to match)."""
    clauses, params = [], []
    if examType:
        clauses.append("p.exam_type = ANY(%s)")
        params.append(examType)
    if post:
        clauses.append("p.post = ANY(%s)")
        params.append(post)
    if year:
        clauses.append("COALESCE(p.year, q.year) = ANY(%s)")
        params.append(year)
    if paperId:
        clauses.append("q.paper_id = ANY(%s)")
        params.append(paperId)
    if subject:
        clauses.append("q.subject = ANY(%s)")
        params.append(subject)
    if difficulty:
        clauses.append("q.difficulty = ANY(%s)")
        params.append(difficulty)
    if qtype:
        clauses.append("q.type = ANY(%s)")
        params.append(qtype)
    if search:
        clauses.append("q.search @@ plainto_tsquery('english', %s)")
        params.append(search)
    return clauses, params


_SORT_MAP = {
    "year": "COALESCE(p.year, q.year)",
    "difficulty": "CASE q.difficulty WHEN 'easy' THEN 0 WHEN 'medium' THEN 1 WHEN 'hard' THEN 2 ELSE 3 END",
    "question": "q.question",
    "id": "q.id",
}


@app.get("/api/mpsc/questions")
def list_bank_questions(
    examType: Optional[List[str]] = Query(None),
    post: Optional[List[str]] = Query(None),
    year: Optional[List[int]] = Query(None),
    paperId: Optional[List[str]] = Query(None),
    subject: Optional[List[str]] = Query(None),
    difficulty: Optional[List[str]] = Query(None),
    type: Optional[List[str]] = Query(None),
    search: Optional[str] = None,
    sortBy: str = "year",
    sortDir: str = "desc",
    limit: int = 25,
    offset: int = 0,
):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses, params = _question_filters(examType, post, year, paperId, subject, difficulty, type, search)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_col = _SORT_MAP.get(sortBy, _SORT_MAP["year"])
        dir_sql = "ASC" if sortDir == "asc" else "DESC"
        limit_c = max(1, min(limit, 100))
        offset_c = max(0, offset)
        cur.execute(f"SELECT count(*) AS n {_QUESTIONS_FROM} {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            # NULLS LAST regardless of direction — Postgres defaults to NULLS
            # FIRST on DESC, which would otherwise surface the ~2,324
            # paper-less/year-less orphan questions ahead of every real,
            # dated question on the default (year desc) sort.
            f"SELECT q.*, COALESCE(p.year, q.year) AS effective_year {_QUESTIONS_FROM} {where} "
            f"ORDER BY {order_col} {dir_sql} NULLS LAST, q.id LIMIT %s OFFSET %s",
            params + [limit_c, offset_c],
        )
        return {"total": total, "questions": [row_to_question(r) for r in cur.fetchall()]}
    finally:
        conn.close()


_FACET_DIMENSIONS = {
    "examType": "p.exam_type",
    "post": "p.post",
    "year": "COALESCE(p.year, q.year)",
    "paperId": "q.paper_id",
    "subject": "q.subject",
    "difficulty": "q.difficulty",
    "type": "q.type",
}


@app.get("/api/mpsc/questions/facets")
def bank_question_facets(
    examType: Optional[List[str]] = Query(None),
    post: Optional[List[str]] = Query(None),
    year: Optional[List[int]] = Query(None),
    paperId: Optional[List[str]] = Query(None),
    subject: Optional[List[str]] = Query(None),
    difficulty: Optional[List[str]] = Query(None),
    type: Optional[List[str]] = Query(None),
    search: Optional[str] = None,
):
    active = {
        "examType": examType, "post": post, "year": year, "paperId": paperId,
        "subject": subject, "difficulty": difficulty, "type": type,
    }
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        out = {}
        for dim, expr in _FACET_DIMENSIONS.items():
            others = {k: v for k, v in active.items() if k != dim}
            clauses, params = _question_filters(
                others.get("examType"), others.get("post"), others.get("year"), others.get("paperId"),
                others.get("subject"), others.get("difficulty"), others.get("type"), search,
            )
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT {expr} AS v, count(*) AS n {_QUESTIONS_FROM} {where} GROUP BY {expr}", params)
            out[dim] = {str(r["v"]): r["n"] for r in cur.fetchall() if r["v"] is not None}
        return out
    finally:
        conn.close()


@app.get("/api/mpsc/questions/sample")
def sample_bank_questions(
    examType: Optional[List[str]] = Query(None),
    post: Optional[List[str]] = Query(None),
    year: Optional[List[int]] = Query(None),
    paperId: Optional[List[str]] = Query(None),
    subject: Optional[List[str]] = Query(None),
    difficulty: Optional[List[str]] = Query(None),
    type: Optional[List[str]] = Query(None),
    search: Optional[str] = None,
    count: int = 25,
):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses, params = _question_filters(examType, post, year, paperId, subject, difficulty, type or ["mcq"], search)
        # Scored/practice tests never sample a figure-based question -- the
        # image was never captured at extraction, so the printed options are
        # unanswerable OCR debris ("option a"/"Figure (a)"/etc). Browse mode
        # still shows them (with the figureBased badge) since they're still
        # real, readable content -- just not something to grade a candidate
        # on. See DEVLOG 2026-08-10.
        clauses = clauses + ["q.figure_based = false"]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count_c = max(1, min(count, 100))
        cur.execute(
            f"SELECT q.*, COALESCE(p.year, q.year) AS effective_year {_QUESTIONS_FROM} {where} ORDER BY random() LIMIT %s",
            params + [count_c],
        )
        return {"questions": [row_to_question(r) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get("/api/mpsc/questions/{questionId}")
def get_bank_question(questionId: str):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT q.*, COALESCE(p.year, q.year) AS effective_year "
            "FROM questions q LEFT JOIN papers p ON p.id = q.paper_id WHERE q.id = %s",
            (questionId,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="question not found")
        return row_to_question(row)
    finally:
        conn.close()


@app.post("/api/mpsc/report")
def submit_legacy_report(report: LegacyReportIn):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reports (question_id, issue_type, notes, question_text, exam_name, exam_type, post) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (report.questionId, report.issueType, report.notes, report.questionText,
             report.examName, report.examType, report.post),
        )
        report_id = cur.fetchone()[0]
        conn.commit()
        return {"status": "ok", "reportId": report_id}
    finally:
        conn.close()


@app.get("/api/mpsc/reports")
def list_legacy_reports():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 200")
        return {"reports": [dict(r) for r in cur.fetchall()]}
    finally:
        conn.close()


# ============================================================
# Auth
# ============================================================

class LoginIn(BaseModel):
    username: str
    password: str


def user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"], "displayName": u.get("display_name")}


@app.post("/api/auth/login")
def login(body: LoginIn):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM users WHERE username = %s", (body.username,))
        user = cur.fetchone()
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = create_token(user)
        return {"token": token, "user": user_public(user)}
    finally:
        conn.close()


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {**user_public(user), "capabilities": capabilities_for(user["role"])}


# ---- Signup (Phase 7 auth flows). Every new account is rank 'learner' —
# roles are granted by an owner/admin afterward, never self-selected. No
# email field exists on `users` (this stayed username+password since it's a
# closed, invite-seeded system) — that also means self-service "forgot
# password via email" isn't buildable without adding email + a mail
# provider, a real infra decision, not something to invent unasked. The
# admin-triggered reset below is the in-architecture substitute.

class SignupIn(BaseModel):
    username: str
    password: str
    displayName: str = ""

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if not _re.match(r"^[a-zA-Z0-9_]{3,32}$", v):
            raise ValueError("Username must be 3-32 characters: letters, numbers, underscore only")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@app.post("/api/auth/signup")
def signup(body: SignupIn):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id FROM users WHERE username = %s", (body.username,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="That username is taken")
        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s, %s, 'learner', %s) RETURNING id",
            (body.username, hash_password(body.password), body.displayName.strip() or None),
        )
        user_id = cur2.fetchone()[0]
        cur2.execute(
            "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, after, note) "
            "VALUES (%s, %s, 'signup', %s, %s, %s, %s)",
            ("_admin", f"user:{body.username}", user_id, None, _json.dumps({"role": "learner"}), f"{body.username} signed up"),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        token = create_token(user)
        return {"token": token, "user": user_public(user)}
    finally:
        conn.close()


# ============================================================
# Question reports (complaints) — bank-agnostic: bank_id + question_id
# identify a question across ANY bank, not just the ones stored in
# Postgres. Any logged-in user can file one; only admins resolve them.
# ============================================================

class ReportIn(BaseModel):
    bankId: str
    questionId: str
    issueType: str
    suggestedAnswerIndex: Optional[int] = None
    # Free-text suggested fix — for descriptive questions/sub-parts, which
    # have no answer index to suggest.
    suggestedText: Optional[str] = None
    # Which lettered sub-part (a..z) this flag targets, if any.
    subpartLabel: Optional[str] = None
    message: str = ""


def report_public(r: dict) -> dict:
    return {
        "id": r["id"],
        "bankId": r["bank_id"],
        "questionId": r["question_id"],
        "issueType": r["issue_type"],
        "suggestedAnswerIndex": r["suggested_answer_index"],
        "suggestedText": r.get("suggested_text"),
        "subpartLabel": r.get("subpart_label"),
        "message": r["message"],
        "status": r["status"],
        "adminNote": r["admin_note"],
        "reviewedBy": r.get("reviewed_by_username"),
        "reviewedAt": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        "username": r.get("username"),
    }


@app.post("/api/questions/report")
def submit_report(body: ReportIn, user: dict = Depends(require_cap("report.create"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "INSERT INTO question_reports "
            "(bank_id, question_id, user_id, issue_type, suggested_answer_index, suggested_text, subpart_label, message) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (body.bankId, body.questionId, user["id"], body.issueType, body.suggestedAnswerIndex,
             body.suggestedText, body.subpartLabel, body.message),
        )
        row = cur.fetchone()
        conn.commit()
        return report_public(row)
    finally:
        conn.close()


@app.get("/api/questions/my-reports")
def my_reports(bankId: Optional[str] = None, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        base = (
            "SELECT r.*, ru.username AS reviewed_by_username "
            "FROM question_reports r LEFT JOIN users ru ON ru.id = r.reviewed_by "
            "WHERE r.user_id = %s"
        )
        if bankId:
            cur.execute(base + " AND r.bank_id = %s ORDER BY r.created_at DESC", (user["id"], bankId))
        else:
            cur.execute(base + " ORDER BY r.created_at DESC", (user["id"],))
        return {"reports": [report_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


# ============================================================
# Corrections — admin-authored overrides, publicly readable so the
# frontend can overlay them on top of the static bundled answer.
# ============================================================

@app.get("/api/questions/corrections")
def get_corrections(bankId: str):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT question_id, corrected_answer_index, corrected_explanation, corrected_note, "
            "corrected_stem, corrected_options, corrected_subparts, updated_at, updated_by "
            "FROM question_corrections WHERE bank_id = %s",
            (bankId,),
        )
        out = {}
        for r in cur.fetchall():
            out[r["question_id"]] = {
                "answerIndex": r["corrected_answer_index"],
                "explanation": r["corrected_explanation"],
                "note": r["corrected_note"],
                "stem": r["corrected_stem"],
                "options": r["corrected_options"],
                "subparts": r["corrected_subparts"],
                "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        return out
    finally:
        conn.close()


# ============================================================
# Public flag summary; no reporter identity, messages, or private notes.
@app.get("/api/questions/flag-status")
def public_flag_status(bankId: str, questionId: str):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT issue_type, count(*) AS n FROM question_reports "
            "WHERE bank_id = %s AND question_id = %s AND status IN ('pending', 'accepted') "
            "GROUP BY issue_type ORDER BY issue_type",
            (bankId, questionId),
        )
        rows = cur.fetchall()
        return {
            "flagged": bool(rows),
            "count": sum(r["n"] for r in rows),
            "issueTypes": [r["issue_type"] for r in rows],
        }
    finally:
        conn.close()

# Comments — public thread per question.
# ============================================================

class CommentIn(BaseModel):
    bankId: str
    questionId: str
    body: str
    # One level of reply — the comment being replied to, if any.
    parentId: Optional[int] = None


class CommentPatchIn(BaseModel):
    body: str


def comment_public(r: dict) -> dict:
    return {
        "id": r["id"],
        "body": r["body"],
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
        "username": r["username"],
        "displayName": r.get("display_name"),
        "parentId": r.get("parent_id"),
        "isPinned": r.get("is_pinned", False),
    }


@app.get("/api/questions/comments")
def list_comments(bankId: str, questionId: str):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT c.id, c.body, c.created_at, c.updated_at, c.parent_id, c.is_pinned, u.username, u.display_name "
            "FROM question_comments c JOIN users u ON u.id = c.user_id "
            "WHERE c.bank_id = %s AND c.question_id = %s AND c.deleted_at IS NULL "
            "ORDER BY c.is_pinned DESC, c.created_at ASC",
            (bankId, questionId),
        )
        return {"comments": [comment_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.post("/api/questions/comments")
def add_comment(body: CommentIn, user: dict = Depends(require_cap("comment.create"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "INSERT INTO question_comments (bank_id, question_id, user_id, body, parent_id) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, body, created_at, updated_at, parent_id, is_pinned",
            (body.bankId, body.questionId, user["id"], body.body, body.parentId),
        )
        row = dict(cur.fetchone())
        conn.commit()
        row["username"] = user["username"]
        row["display_name"] = user.get("display_name")
        return comment_public(row)
    finally:
        conn.close()


@app.patch("/api/questions/comments/{comment_id}")
def edit_comment(comment_id: int, body: CommentPatchIn, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM question_comments WHERE id = %s AND deleted_at IS NULL", (comment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found")
        if row["user_id"] != user["id"] and not has_cap(user["role"], "comment.moderate"):
            raise HTTPException(status_code=403, detail="Not your comment")
        cur2 = conn.cursor()
        cur2.execute("UPDATE question_comments SET body = %s, updated_at = now() WHERE id = %s", (body.body, comment_id))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/questions/comments/{comment_id}")
def delete_comment(comment_id: int, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM question_comments WHERE id = %s AND deleted_at IS NULL", (comment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found")
        is_owner = row["user_id"] == user["id"]
        is_moderator = has_cap(user["role"], "comment.moderate")
        if not is_owner and not is_moderator:
            raise HTTPException(status_code=403, detail="Not your comment")
        cur2 = conn.cursor()
        cur2.execute("UPDATE question_comments SET deleted_at = now() WHERE id = %s", (comment_id,))
        if is_moderator and not is_owner:
            cur2.execute(
                "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, note) "
                "VALUES (%s, %s, 'comment_deleted', %s, %s, %s)",
                (row["bank_id"], row["question_id"], user["id"], _json.dumps({"body": row["body"]}), "Deleted via moderation"),
            )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/api/questions/comments/{comment_id}/pin")
def pin_comment(comment_id: int, actor: dict = Depends(require_cap("comment.moderate"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM question_comments WHERE id = %s AND deleted_at IS NULL", (comment_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found")
        new_pinned = not row["is_pinned"]
        cur2 = conn.cursor()
        cur2.execute("UPDATE question_comments SET is_pinned = %s WHERE id = %s", (new_pinned, comment_id))
        cur2.execute(
            "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, after) "
            "VALUES (%s, %s, 'comment_pinned', %s, %s, %s)",
            (row["bank_id"], row["question_id"], actor["id"],
             _json.dumps({"isPinned": row["is_pinned"]}), _json.dumps({"isPinned": new_pinned})),
        )
        conn.commit()
        return {"status": "ok", "isPinned": new_pinned}
    finally:
        conn.close()


# ============================================================
# Personal notes — private, one per (user, bank, question).
# ============================================================

class NoteIn(BaseModel):
    bankId: str
    questionId: str
    note: str


@app.get("/api/questions/notes")
def get_note(bankId: str, questionId: str, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT note, updated_at FROM question_notes WHERE user_id = %s AND bank_id = %s AND question_id = %s",
            (user["id"], bankId, questionId),
        )
        row = cur.fetchone()
        if not row:
            return {"note": "", "updatedAt": None}
        return {"note": row["note"], "updatedAt": row["updated_at"].isoformat()}
    finally:
        conn.close()


@app.put("/api/questions/notes")
def upsert_note(body: NoteIn, user: dict = Depends(require_cap("note.write"))):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO question_notes (user_id, bank_id, question_id, note, updated_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (user_id, bank_id, question_id) DO UPDATE SET note = EXCLUDED.note, updated_at = now()",
            (user["id"], body.bankId, body.questionId, body.note),
        )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ============================================================
# Progress — per-user mock test attempt history.
# ============================================================

class MockAttemptIn(BaseModel):
    bankId: str
    testLabel: str = ""
    total: int
    correct: int
    wrong: int
    unattempted: int
    score: float


@app.post("/api/progress/mock-attempt")
def record_attempt(body: MockAttemptIn, user: dict = Depends(require_cap("attempt.write"))):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mock_attempts (user_id, bank_id, test_label, total, correct, wrong, unattempted, score) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user["id"], body.bankId, body.testLabel, body.total, body.correct, body.wrong, body.unattempted, body.score),
        )
        attempt_id = cur.fetchone()[0]
        conn.commit()
        return {"status": "ok", "attemptId": attempt_id}
    finally:
        conn.close()


@app.get("/api/progress/history")
def get_history(bankId: Optional[str] = None, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        if bankId:
            cur.execute(
                "SELECT * FROM mock_attempts WHERE user_id = %s AND bank_id = %s ORDER BY taken_at DESC LIMIT 200",
                (user["id"], bankId),
            )
        else:
            cur.execute(
                "SELECT * FROM mock_attempts WHERE user_id = %s ORDER BY taken_at DESC LIMIT 200",
                (user["id"],),
            )
        rows = cur.fetchall()
        return {
            "attempts": [
                {
                    "id": r["id"],
                    "bankId": r["bank_id"],
                    "testLabel": r["test_label"],
                    "total": r["total"],
                    "correct": r["correct"],
                    "wrong": r["wrong"],
                    "unattempted": r["unattempted"],
                    "score": float(r["score"]),
                    "takenAt": r["taken_at"].isoformat(),
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


# ============================================================
# Real per-topic attempt log. mock_attempts (above) only stores whole-test
# aggregates -- nothing before this could answer "how am I doing on
# Mizoram GK specifically". Every write here is one real answered question;
# GET /api/me/topic-accuracy aggregates them per (subject, topic) for the
# calling user only. Same 'attempt.write' cap the mock-attempt endpoint
# already uses -- this is the same class of write, just finer-grained.
# ============================================================

class AttemptLogIn(BaseModel):
    subject: str
    topic: str
    topicLabel: str
    source: str
    correct: bool


@app.post("/api/attempts/log")
def log_attempt(body: AttemptLogIn, user: dict = Depends(require_cap("attempt.write"))):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO attempt_log (user_id, subject, topic, topic_label, source, correct) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user["id"], body.subject, body.topic, body.topicLabel, body.source, body.correct),
        )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.get("/api/me/topic-accuracy")
def my_topic_accuracy(user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT subject, topic, topic_label, "
            "count(*) FILTER (WHERE correct) AS correct, count(*) AS attempts "
            "FROM attempt_log WHERE user_id = %s GROUP BY subject, topic, topic_label "
            "ORDER BY attempts DESC",
            (user["id"],),
        )
        return {
            "topics": [
                {
                    "subject": r["subject"], "topic": r["topic"], "topicLabel": r["topic_label"],
                    "correct": r["correct"], "attempts": r["attempts"],
                    "accuracy": round(r["correct"] / r["attempts"] * 100) if r["attempts"] else 0,
                }
                for r in cur.fetchall()
            ]
        }
    finally:
        conn.close()


# ============================================================
# Admin — bulk triage of reports, and per-question corrections.
# ============================================================

@app.get("/api/admin/reports")
def admin_list_reports(
    status: Optional[str] = None,
    bankId: Optional[str] = None,
    issueType: Optional[List[str]] = Query(None),
    search: Optional[str] = None,
    fromDate: Optional[str] = None,
    toDate: Optional[str] = None,
    hasSuggestion: Optional[bool] = None,
    subpartLabel: Optional[str] = None,
    sort: str = "newest",
    limit: int = 100,
    offset: int = 0,
    actor: dict = Depends(require_cap("report.reject")),
):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses = []
        params: List = []
        if status:
            clauses.append("r.status = %s")
            params.append(status)
        if bankId:
            clauses.append("r.bank_id = %s")
            params.append(bankId)
        if issueType:
            clauses.append("r.issue_type = ANY(%s)")
            params.append(issueType)
        if search:
            clauses.append("(r.message ILIKE %s OR u.username ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like])
        if fromDate:
            clauses.append("r.created_at >= %s")
            params.append(fromDate)
        if toDate:
            clauses.append("r.created_at <= %s")
            params.append(toDate)
        if hasSuggestion is not None:
            if hasSuggestion:
                clauses.append("(r.suggested_answer_index IS NOT NULL OR r.suggested_text IS NOT NULL)")
            else:
                clauses.append("(r.suggested_answer_index IS NULL AND r.suggested_text IS NULL)")
        if subpartLabel:
            clauses.append("r.subpart_label = %s")
            params.append(subpartLabel)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "r.created_at ASC" if sort == "oldest" else "r.created_at DESC"
        limit_c = max(1, min(limit, 500))
        offset_c = max(0, offset)
        cur.execute(
            f"SELECT r.*, u.username, ru.username AS reviewed_by_username "
            f"FROM question_reports r "
            f"JOIN users u ON u.id = r.user_id "
            f"LEFT JOIN users ru ON ru.id = r.reviewed_by "
            f"{where} ORDER BY {order} LIMIT %s OFFSET %s",
            params + [limit_c, offset_c],
        )
        return {"reports": [report_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


class BulkStatusIn(BaseModel):
    ids: List[int]
    status: str  # 'accepted' | 'rejected'
    adminNote: str = ""


@app.post("/api/admin/reports/bulk-status")
def admin_bulk_status(body: BulkStatusIn, actor: dict = Depends(get_current_user)):
    if body.status not in ("accepted", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'accepted' or 'rejected'")
    needed_cap = "report.accept" if body.status == "accepted" else "report.reject"
    if not has_cap(actor["role"], needed_cap):
        raise HTTPException(status_code=403, detail=f"missing capability: {needed_cap}")
    if not body.ids:
        return {"updated": 0}
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT id, bank_id, question_id, subpart_label, status FROM question_reports WHERE id = ANY(%s)",
            (body.ids,),
        )
        before_rows = {r["id"]: dict(r) for r in cur.fetchall()}

        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE question_reports SET status = %s, admin_note = %s, reviewed_by = %s, reviewed_at = now() "
            "WHERE id = ANY(%s)",
            (body.status, body.adminNote, actor["id"], body.ids),
        )
        updated = cur2.rowcount
        for rid, before in before_rows.items():
            cur2.execute(
                "INSERT INTO question_audit_log (bank_id, question_id, subpart_label, action, actor_id, before, after, note) "
                "VALUES (%s, %s, %s, 'report_status', %s, %s, %s, %s)",
                (before["bank_id"], before["question_id"], before["subpart_label"], actor["id"],
                 _json.dumps({"status": before["status"]}),
                 _json.dumps({"status": body.status, "reportId": rid}), body.adminNote or None),
            )
        conn.commit()
        return {"updated": updated}
    finally:
        conn.close()


class CorrectionIn(BaseModel):
    bankId: str
    questionId: str
    correctedAnswerIndex: Optional[int] = None
    correctedExplanation: Optional[str] = None
    correctedNote: Optional[str] = None
    correctedStem: Optional[str] = None
    correctedOptions: Optional[List[str]] = None
    # Per-sub-part overrides for descriptive questions: [{label, text, modelAnswer}].
    correctedSubparts: Optional[List[dict]] = None
    # Which lettered sub-part this correction was made in response to, if any
    # (informational — the correction itself always covers the whole question).
    subpartLabel: Optional[str] = None
    reportIds: List[int] = []
    adminNote: str = ""


@app.post("/api/admin/corrections")
def admin_upsert_correction(body: CorrectionIn, actor: dict = Depends(require_cap("correction.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT corrected_answer_index, corrected_explanation, corrected_note, "
            "corrected_stem, corrected_options, corrected_subparts "
            "FROM question_corrections WHERE bank_id = %s AND question_id = %s",
            (body.bankId, body.questionId),
        )
        before_row = cur.fetchone()
        before = dict(before_row) if before_row else None

        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO question_corrections "
            "(bank_id, question_id, corrected_answer_index, corrected_explanation, corrected_note, "
            "corrected_stem, corrected_options, corrected_subparts, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (bank_id, question_id) DO UPDATE SET "
            "corrected_answer_index = EXCLUDED.corrected_answer_index, "
            "corrected_explanation = EXCLUDED.corrected_explanation, "
            "corrected_note = EXCLUDED.corrected_note, "
            "corrected_stem = EXCLUDED.corrected_stem, "
            "corrected_options = EXCLUDED.corrected_options, "
            "corrected_subparts = EXCLUDED.corrected_subparts, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()",
            (body.bankId, body.questionId, body.correctedAnswerIndex, body.correctedExplanation,
             body.correctedNote, body.correctedStem,
             _json.dumps(body.correctedOptions) if body.correctedOptions is not None else None,
             _json.dumps(body.correctedSubparts) if body.correctedSubparts is not None else None,
             actor["id"]),
        )

        after = {
            "correctedAnswerIndex": body.correctedAnswerIndex,
            "correctedExplanation": body.correctedExplanation,
            "correctedNote": body.correctedNote,
            "correctedStem": body.correctedStem,
            "correctedOptions": body.correctedOptions,
            "correctedSubparts": body.correctedSubparts,
        }
        cur2.execute(
            "INSERT INTO question_audit_log (bank_id, question_id, subpart_label, action, actor_id, before, after, note) "
            "VALUES (%s, %s, %s, 'correction', %s, %s, %s, %s)",
            (body.bankId, body.questionId, body.subpartLabel, actor["id"],
             _json.dumps(before) if before is not None else None, _json.dumps(after), body.adminNote or None),
        )

        if body.reportIds:
            cur.execute(
                "SELECT id, bank_id, question_id, subpart_label, status FROM question_reports WHERE id = ANY(%s)",
                (body.reportIds,),
            )
            report_before = {r["id"]: dict(r) for r in cur.fetchall()}
            cur2.execute(
                "UPDATE question_reports SET status = 'accepted', admin_note = %s, reviewed_by = %s, reviewed_at = now() "
                "WHERE id = ANY(%s)",
                (body.adminNote, actor["id"], body.reportIds),
            )
            for rid, rbefore in report_before.items():
                cur2.execute(
                    "INSERT INTO question_audit_log (bank_id, question_id, subpart_label, action, actor_id, before, after, note) "
                    "VALUES (%s, %s, %s, 'report_status', %s, %s, %s, %s)",
                    (rbefore["bank_id"], rbefore["question_id"], rbefore["subpart_label"], actor["id"],
                     _json.dumps({"status": rbefore["status"]}),
                     _json.dumps({"status": "accepted", "reportId": rid}), body.adminNote or None),
                )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.get("/api/admin/users")
def admin_list_users(actor: dict = Depends(require_cap("user.read"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id, username, role, display_name, created_at FROM users ORDER BY created_at ASC")
        return {
            "users": [
                {
                    "id": r["id"], "username": r["username"], "role": r["role"],
                    "displayName": r["display_name"], "createdAt": r["created_at"].isoformat(),
                }
                for r in cur.fetchall()
            ]
        }
    finally:
        conn.close()


# ============================================================
# Audit log — every correction, report-status change, and comment
# moderation action lands here. Single source of "who changed what,
# when" for the admin History tab.
# ============================================================

@app.get("/api/admin/audit-log")
def admin_audit_log(
    bankId: Optional[str] = None,
    questionId: Optional[str] = None,
    actorId: Optional[int] = None,
    action: Optional[str] = None,
    fromDate: Optional[str] = None,
    toDate: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    actor: dict = Depends(get_current_user),
):
    # Scoped read: an editor may read the history of one question, because they
    # can already edit that question (correction.write, same rank 4) and seeing
    # who changed it before them is strictly less privilege. The unscoped
    # site-wide feed still requires audit.read (rank 5).
    needed = "audit.read_question" if questionId else "audit.read"
    if not has_cap(actor["role"], needed):
        raise HTTPException(status_code=403, detail=f"missing capability: {needed}")

    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses = []
        params: List = []
        if bankId:
            clauses.append("a.bank_id = %s")
            params.append(bankId)
        if questionId:
            clauses.append("a.question_id = %s")
            params.append(questionId)
        if actorId:
            clauses.append("a.actor_id = %s")
            params.append(actorId)
        if action:
            clauses.append("a.action = %s")
            params.append(action)
        if fromDate:
            clauses.append("a.created_at >= %s")
            params.append(fromDate)
        if toDate:
            clauses.append("a.created_at <= %s")
            params.append(toDate)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_c = max(1, min(limit, 500))
        offset_c = max(0, offset)
        cur.execute(
            f"SELECT a.*, u.username AS actor_username FROM question_audit_log a "
            f"JOIN users u ON u.id = a.actor_id "
            f"{where} ORDER BY a.created_at DESC LIMIT %s OFFSET %s",
            params + [limit_c, offset_c],
        )
        return {
            "entries": [
                {
                    "id": r["id"],
                    "bankId": r["bank_id"],
                    "questionId": r["question_id"],
                    "subpartLabel": r["subpart_label"],
                    "action": r["action"],
                    "actorId": r["actor_id"],
                    "actorUsername": r["actor_username"],
                    "before": r["before"],
                    "after": r["after"],
                    "note": r["note"],
                    "createdAt": r["created_at"].isoformat(),
                }
                for r in cur.fetchall()
            ]
        }
    finally:
        conn.close()


# ============================================================
# Admin — moderate/browse all comments across a bank.
# ============================================================

@app.get("/api/admin/comments")
def admin_list_comments(
    bankId: Optional[str] = None,
    search: Optional[str] = None,
    fromDate: Optional[str] = None,
    toDate: Optional[str] = None,
    pinned: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
    actor: dict = Depends(require_cap("comment.moderate")),
):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        clauses = ["c.deleted_at IS NULL"]
        params: List = []
        if bankId:
            clauses.append("c.bank_id = %s")
            params.append(bankId)
        if search:
            clauses.append("c.body ILIKE %s")
            params.append(f"%{search}%")
        if fromDate:
            clauses.append("c.created_at >= %s")
            params.append(fromDate)
        if toDate:
            clauses.append("c.created_at <= %s")
            params.append(toDate)
        if pinned is not None:
            clauses.append("c.is_pinned = %s")
            params.append(pinned)
        where = f"WHERE {' AND '.join(clauses)}"
        limit_c = max(1, min(limit, 500))
        offset_c = max(0, offset)
        cur.execute(
            f"SELECT c.*, u.username, u.display_name FROM question_comments c "
            f"JOIN users u ON u.id = c.user_id {where} "
            f"ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
            params + [limit_c, offset_c],
        )
        return {
            "comments": [
                {**comment_public(dict(r)), "bankId": r["bank_id"], "questionId": r["question_id"]}
                for r in cur.fetchall()
            ]
        }
    finally:
        conn.close()


# ============================================================
# Admin dashboard stats — headline counts + recent activity feed.
# ============================================================

@app.get("/api/admin/stats")
def admin_stats(actor: dict = Depends(require_cap("admin.stats"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT bank_id, status, count(*) AS n FROM question_reports GROUP BY bank_id, status")
        by_bank_status = cur.fetchall()
        cur.execute("SELECT issue_type, count(*) AS n FROM question_reports GROUP BY issue_type")
        by_issue_type = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM question_comments WHERE deleted_at IS NULL")
        total_comments = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM question_corrections")
        total_corrections = cur.fetchone()["n"]
        cur.execute(
            "SELECT u.id, u.username, "
            "(SELECT count(*) FROM question_reports r WHERE r.user_id = u.id) AS reports_filed, "
            "(SELECT count(*) FROM question_comments c WHERE c.user_id = u.id AND c.deleted_at IS NULL) AS comments_posted, "
            "(SELECT count(*) FROM question_corrections cc WHERE cc.updated_by = u.id) AS corrections_authored "
            "FROM users u ORDER BY u.created_at ASC"
        )
        per_user = cur.fetchall()
        cur.execute(
            "SELECT a.*, u.username AS actor_username FROM question_audit_log a "
            "JOIN users u ON u.id = a.actor_id ORDER BY a.created_at DESC LIMIT 20"
        )
        recent_activity = [
            {
                "id": r["id"], "bankId": r["bank_id"], "questionId": r["question_id"],
                "subpartLabel": r["subpart_label"], "action": r["action"],
                "actorUsername": r["actor_username"], "note": r["note"],
                "createdAt": r["created_at"].isoformat(),
            }
            for r in cur.fetchall()
        ]
        return {
            "byBankStatus": [{"bankId": r["bank_id"], "status": r["status"], "count": r["n"]} for r in by_bank_status],
            "byIssueType": [{"issueType": r["issue_type"], "count": r["n"]} for r in by_issue_type],
            "totalComments": total_comments,
            "totalCorrections": total_corrections,
            "perUser": [
                {
                    "id": r["id"], "username": r["username"],
                    "reportsFiled": r["reports_filed"], "commentsPosted": r["comments_posted"],
                    "correctionsAuthored": r["corrections_authored"],
                }
                for r in per_user
            ],
            "recentActivity": recent_activity,
        }
    finally:
        conn.close()


# ============================================================
# Admin — role assignment. Owner-only: the one capability that isn't
# rank-additive from admin, since it lets the caller change who else
# holds which rank.
# ============================================================

VALID_ROLES = ("learner", "moderator", "reviewer", "editor", "admin", "owner")


class RoleAssignIn(BaseModel):
    role: str


@app.post("/api/admin/users/{user_id}/role")
def admin_assign_role(user_id: int, body: RoleAssignIn, actor: dict = Depends(require_cap("user.role.assign"))):
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        cur2 = conn.cursor()
        cur2.execute("UPDATE users SET role = %s WHERE id = %s", (body.role, user_id))
        cur2.execute(
            "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, after, note) "
            "VALUES (%s, %s, 'role_assigned', %s, %s, %s, %s)",
            ("_admin", f"user:{target['username']}", actor["id"],
             _json.dumps({"role": target["role"]}), _json.dumps({"role": body.role}),
             f"{actor['username']} set {target['username']}'s role to {body.role}"),
        )
        conn.commit()
        return {"status": "ok", "userId": user_id, "role": body.role}
    finally:
        conn.close()


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, actor: dict = Depends(require_cap("user.reset_password"))):
    """Sets a random temp password and returns it once — same
    shown-once-not-stored convention as the originally seeded accounts
    (seed_accounts.py). No email exists to send it to; the admin/owner
    relays it to the user directly, out of band."""
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        temp_password = _secrets.token_urlsafe(9)
        cur2 = conn.cursor()
        cur2.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(temp_password), user_id))
        cur2.execute(
            "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, note) "
            "VALUES (%s, %s, 'password_reset', %s, %s)",
            ("_admin", f"user:{target['username']}", actor["id"], f"{actor['username']} reset {target['username']}'s password"),
        )
        conn.commit()
        return {"status": "ok", "userId": user_id, "tempPassword": temp_password}
    finally:
        conn.close()


# ============================================================
# TestDefinition (Phase 5) — a saved FILTER, not a materialized question
# list. `filter` stores the same query params /api/mpsc/questions accepts;
# a test "grows" as the bank grows rather than freezing a fixed set of
# question ids at creation time.
# ============================================================

_TEST_KINDS = ("real_paper", "full_sitting", "sectional", "adaptive", "sprint")
_VALID_NEGATIVE = (0, 0.25, 0.33, 0.5)


class TestDefIn(BaseModel):
    title: str
    kind: Literal["real_paper", "full_sitting", "sectional", "adaptive", "sprint"]
    filter: dict = {}
    n_questions: int
    duration_s: int
    negative: float = 0
    shuffle: bool = True
    is_published: bool = False

    @field_validator("negative")
    @classmethod
    def _validate_negative(cls, v):
        if v not in _VALID_NEGATIVE:
            raise ValueError(f"negative must be one of {_VALID_NEGATIVE}")
        return v


class TestDefPatchIn(BaseModel):
    title: Optional[str] = None
    kind: Optional[Literal["real_paper", "full_sitting", "sectional", "adaptive", "sprint"]] = None
    filter: Optional[dict] = None
    n_questions: Optional[int] = None
    duration_s: Optional[int] = None
    negative: Optional[float] = None
    shuffle: Optional[bool] = None
    is_published: Optional[bool] = None

    @field_validator("negative")
    @classmethod
    def _validate_negative(cls, v):
        if v is not None and v not in _VALID_NEGATIVE:
            raise ValueError(f"negative must be one of {_VALID_NEGATIVE}")
        return v


def test_def_public(r: dict) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "kind": r["kind"],
        "filter": r["filter"],
        "nQuestions": r["n_questions"],
        "durationS": r["duration_s"],
        "negative": float(r["negative"]),
        "shuffle": r["shuffle"],
        "isPublished": r["is_published"],
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


@app.get("/api/tests/")
def list_published_tests():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM test_definitions WHERE is_published = true ORDER BY created_at DESC")
        return {"tests": [test_def_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get("/api/admin/tests/")
def list_all_tests(user: dict = Depends(require_cap("test.publish"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM test_definitions ORDER BY created_at DESC")
        return {"tests": [test_def_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.post("/api/admin/tests/")
def create_test(body: TestDefIn, user: dict = Depends(require_cap("test.publish"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "INSERT INTO test_definitions "
            "(title, kind, filter, n_questions, duration_s, negative, shuffle, is_published, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (body.title, body.kind, _json.dumps(body.filter), body.n_questions, body.duration_s,
             body.negative, body.shuffle, body.is_published, user["id"]),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return test_def_public(row)
    finally:
        conn.close()


@app.patch("/api/admin/tests/{test_id}")
def update_test(test_id: int, body: TestDefPatchIn, user: dict = Depends(require_cap("test.publish"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM test_definitions WHERE id = %s", (test_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Test not found")

        updates = body.model_dump(exclude_unset=True)
        if updates:
            set_clauses, params = [], []
            for field, value in updates.items():
                column = "filter" if field == "filter" else field
                set_clauses.append(f"{column} = %s")
                params.append(_json.dumps(value) if field == "filter" else value)
            set_clauses.append("updated_at = now()")
            params.append(test_id)
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE test_definitions SET {', '.join(set_clauses)} WHERE id = %s", params)
            conn.commit()

        cur.execute("SELECT * FROM test_definitions WHERE id = %s", (test_id,))
        return test_def_public(dict(cur.fetchone()))
    finally:
        conn.close()


@app.delete("/api/admin/tests/{test_id}")
def delete_test(test_id: int, user: dict = Depends(require_cap("test.publish"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id FROM test_definitions WHERE id = %s", (test_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Test not found")
        cur2 = conn.cursor()
        cur2.execute("DELETE FROM test_definitions WHERE id = %s", (test_id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ============================================================
# StaticSet (Phase 5c) — registry metadata for the premade static HTML sets
# in Library. The files themselves are untouched, served as-is by the
# frontend's own registry.ts/EmbedPage — this table only carries counts,
# blurb, and publish state for the Library page to read.
# ============================================================

_SET_GROUPS = ("lab", "exam_guide", "quick_practice")


class StaticSetIn(BaseModel):
    title: str
    group: Literal["lab", "exam_guide", "quick_practice"]
    route: str
    n_items: Optional[int] = None
    unit: str = "Qs"
    blurb: str = ""
    is_published: bool = False


class StaticSetPatchIn(BaseModel):
    title: Optional[str] = None
    group: Optional[Literal["lab", "exam_guide", "quick_practice"]] = None
    route: Optional[str] = None
    n_items: Optional[int] = None
    unit: Optional[str] = None
    blurb: Optional[str] = None
    is_published: Optional[bool] = None


def static_set_public(r: dict) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "group": r["group"],
        "route": r["route"],
        "nItems": r["n_items"],
        "unit": r["unit"],
        "blurb": r["blurb"],
        "isPublished": r["is_published"],
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


@app.get("/api/static-sets/")
def list_published_static_sets():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute('SELECT * FROM static_sets WHERE is_published = true ORDER BY "group", title')
        return {"sets": [static_set_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get("/api/admin/static-sets/")
def list_all_static_sets(user: dict = Depends(require_cap("static_set.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute('SELECT * FROM static_sets ORDER BY "group", title')
        return {"sets": [static_set_public(dict(r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.post("/api/admin/static-sets/")
def create_static_set(body: StaticSetIn, user: dict = Depends(require_cap("static_set.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            'INSERT INTO static_sets (title, "group", route, n_items, unit, blurb, is_published) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (body.title, body.group, body.route, body.n_items, body.unit, body.blurb, body.is_published),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return static_set_public(row)
    finally:
        conn.close()


@app.patch("/api/admin/static-sets/{set_id}")
def update_static_set(set_id: int, body: StaticSetPatchIn, user: dict = Depends(require_cap("static_set.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM static_sets WHERE id = %s", (set_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Static set not found")

        updates = body.model_dump(exclude_unset=True)
        if updates:
            set_clauses, params = [], []
            for field, value in updates.items():
                column = f'"{field}"' if field == "group" else field
                set_clauses.append(f"{column} = %s")
                params.append(value)
            set_clauses.append("updated_at = now()")
            params.append(set_id)
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE static_sets SET {', '.join(set_clauses)} WHERE id = %s", params)
            conn.commit()

        cur.execute("SELECT * FROM static_sets WHERE id = %s", (set_id,))
        return static_set_public(dict(cur.fetchone()))
    finally:
        conn.close()


@app.delete("/api/admin/static-sets/{set_id}")
def delete_static_set(set_id: int, user: dict = Depends(require_cap("static_set.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id FROM static_sets WHERE id = %s", (set_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Static set not found")
        cur2 = conn.cursor()
        cur2.execute("DELETE FROM static_sets WHERE id = %s", (set_id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ============================================================
# FeatureFlag registry (Phase 6, AdminTable target #8 of 8). No optional-auth
# dependency exists anywhere in this codebase (every public GET here has zero
# Depends()) — rather than invent one, the public endpoint stays simple: only
# `audience = 'everyone'` flags that are on are visible to an anonymous
# caller. Anything role-gated is an admin-console-only concept for now.
# ============================================================

def flag_public(r: dict) -> dict:
    return {
        "key": r["key"],
        "describes": r["describes"],
        "audience": r["audience"],
        "isOn": r["is_on"],
        "rolledOut": r["rolled_out"].isoformat() if r.get("rolled_out") else None,
    }


_FlagAudience = Literal["everyone", "logged_in", "moderator", "reviewer", "editor", "admin"]


class FlagIn(BaseModel):
    key: str
    describes: str = ""
    audience: _FlagAudience = "everyone"
    is_on: bool = False
    rolled_out: Optional[str] = None


class FlagPatchIn(BaseModel):
    describes: Optional[str] = None
    audience: Optional[_FlagAudience] = None
    is_on: Optional[bool] = None
    rolled_out: Optional[str] = None


@app.get("/api/flags/")
def list_public_flags():
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM feature_flags WHERE is_on = true AND audience = 'everyone' ORDER BY key")
        return {"flags": [flag_public(r) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get("/api/admin/flags/")
def admin_list_flags(user: dict = Depends(require_cap("flag.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM feature_flags ORDER BY key")
        return {"flags": [flag_public(r) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.post("/api/admin/flags/")
def admin_create_flag(body: FlagIn, user: dict = Depends(require_cap("flag.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "INSERT INTO feature_flags (key, describes, audience, is_on, rolled_out) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING RETURNING *",
            (body.key, body.describes, body.audience, body.is_on, body.rolled_out),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=409, detail=f"Flag '{body.key}' already exists")
        conn.commit()
        return flag_public(dict(row))
    finally:
        conn.close()


@app.patch("/api/admin/flags/{key}")
def admin_update_flag(key: str, body: FlagPatchIn, user: dict = Depends(require_cap("flag.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM feature_flags WHERE key = %s", (key,))
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="Flag not found")

        updates = body.model_dump(exclude_unset=True)
        if updates:
            set_clauses, params = [], []
            for field, value in updates.items():
                set_clauses.append(f"{field} = %s")
                params.append(value)
            set_clauses.append("updated_at = now()")
            params.append(key)
            cur2 = conn.cursor()
            cur2.execute(f"UPDATE feature_flags SET {', '.join(set_clauses)} WHERE key = %s", params)
            cur2.execute(
                "INSERT INTO question_audit_log (bank_id, question_id, action, actor_id, before, after, note) "
                "VALUES (%s, %s, 'flag_toggled', %s, %s, %s, %s)",
                ("_admin", f"flag:{key}", user["id"], _json.dumps(flag_public(dict(before))),
                 _json.dumps({**flag_public(dict(before)), **{k: v for k, v in updates.items()}}),
                 f"{user['username']} updated flag {key}"),
            )
            conn.commit()

        cur.execute("SELECT * FROM feature_flags WHERE key = %s", (key,))
        return flag_public(dict(cur.fetchone()))
    finally:
        conn.close()


@app.delete("/api/admin/flags/{key}")
def admin_delete_flag(key: str, user: dict = Depends(require_cap("flag.write"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT key FROM feature_flags WHERE key = %s", (key,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Flag not found")
        cur2 = conn.cursor()
        cur2.execute("DELETE FROM feature_flags WHERE key = %s", (key,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ============================================================
# Import pipeline (Phase 6, the last AdminTable target). IMP-0138 lost 280
# questions silently because nothing compared parse-count-in to
# rows-written; that comparison is the entire feature here.
#
# Scope: accepts a pre-extracted JSON payload (paper metadata + parsed
# questions), not raw PDF bytes — see migrate_v8.sql's comment for why live
# PDF OCR/extraction isn't wired in here.
#
# Question ids are content-derived (md5 of paper_id + question text), which
# gives idempotent re-uploads for free: re-importing the same JSON produces
# the same ids, so ON CONFLICT DO NOTHING silently skips duplicates at the
# DB level — and the resulting written-count-below-parsed-count is exactly
# what the count check is designed to catch and fail loudly on, not paper
# over.
# ============================================================

def _slugify(*parts: str) -> str:
    s = "-".join(p for p in parts if p).lower()
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "paper"


class ImportPaperIn(BaseModel):
    examType: str
    examName: str
    post: Optional[str] = None
    paperNumber: Optional[str] = None
    paperSubject: str = ""
    year: Optional[int] = None
    sourceFile: Optional[str] = None


class ImportQuestionIn(BaseModel):
    paperIndex: Optional[int] = None  # index into the payload's papers[], or None = paper-less
    subject: str
    topic: str = ""
    topicLabel: str = ""
    difficulty: str = "medium"
    type: str = "mcq"
    question: str
    options: dict
    answerIndex: int
    explanation: str = ""
    source: Optional[str] = None
    year: Optional[int] = None
    tags: List[str] = []


class ImportPayload(BaseModel):
    papers: List[ImportPaperIn] = []
    questions: List[ImportQuestionIn]


@app.get("/api/admin/imports/")
def admin_list_imports(user: dict = Depends(require_cap("import.run"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT i.*, u.username AS actor_username FROM import_runs i "
            "JOIN users u ON u.id = i.actor_id ORDER BY i.created_at DESC LIMIT 100"
        )
        rows = cur.fetchall()
        return {
            "runs": [
                {
                    "id": r["id"], "filename": r["filename"], "status": r["status"],
                    "parsedPapers": r["parsed_papers"], "parsedQuestions": r["parsed_questions"],
                    "writtenQuestions": r["written_questions"], "error": r["error"],
                    "actorUsername": r["actor_username"], "createdAt": r["created_at"].isoformat(),
                    "appliedAt": r["applied_at"].isoformat() if r["applied_at"] else None,
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/api/admin/imports/")
async def admin_create_import(file: UploadFile = File(...), user: dict = Depends(require_cap("import.run"))):
    """Dry-run only — parses and validates the upload, writes nothing to
    papers/questions. Always the first step; apply() is separate and
    explicit."""
    raw = await file.read()
    try:
        data = _json.loads(raw)
        payload = ImportPayload(**data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse import file: {e}")

    for q in payload.questions:
        if q.paperIndex is not None and not (0 <= q.paperIndex < len(payload.papers)):
            raise HTTPException(status_code=400, detail=f"Question references paperIndex {q.paperIndex}, but only {len(payload.papers)} papers were provided")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO import_runs (filename, status, parsed_papers, parsed_questions, payload, actor_id) "
            "VALUES (%s, 'dry_run', %s, %s, %s, %s) RETURNING id",
            (file.filename, len(payload.papers), len(payload.questions), payload.model_dump_json(), user["id"]),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
        return {"id": run_id, "status": "dry_run", "parsedPapers": len(payload.papers), "parsedQuestions": len(payload.questions)}
    finally:
        conn.close()


@app.post("/api/admin/imports/{run_id}/apply/")
def admin_apply_import(run_id: int, user: dict = Depends(require_cap("import.run"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM import_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Import run not found")
        if run["status"] != "dry_run":
            raise HTTPException(status_code=409, detail=f"Run is '{run['status']}', not 'dry_run' — only a fresh dry-run can be applied")

        payload = ImportPayload(**run["payload"])
        cur2 = conn.cursor()

        paper_ids: List[Optional[str]] = []
        for p in payload.papers:
            paper_id = _slugify(p.examType, p.examName, str(p.year or ""), p.paperNumber or "")
            cur2.execute(
                "INSERT INTO papers (id, exam_type, exam_name, post, paper_number, paper_subject, year, source_file) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (paper_id, p.examType, p.examName, p.post, p.paperNumber, p.paperSubject, p.year, p.sourceFile),
            )
            paper_ids.append(paper_id)

        written_ids: List[str] = []
        for q in payload.questions:
            paper_id = paper_ids[q.paperIndex] if q.paperIndex is not None else None
            qid = _hashlib.md5(f"{paper_id or ''}:{q.question}".encode()).hexdigest()[:20]
            cur2.execute(
                "INSERT INTO questions (id, paper_id, subject, topic, topic_label, difficulty, question, "
                "options, answer_index, explanation, source, year, tags, type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING RETURNING id",
                (qid, paper_id, q.subject, q.topic, q.topicLabel or q.topic, q.difficulty, q.question,
                 _json.dumps(q.options), q.answerIndex, q.explanation, q.source, q.year, q.tags, q.type),
            )
            row = cur2.fetchone()
            if row:
                written_ids.append(row[0])

        parsed = len(payload.questions)
        written = len(written_ids)

        # The count check IS the feature: parse count in must equal rows
        # written, or the whole run rolls back and fails loudly.
        if written != parsed:
            conn.rollback()
            cur3 = conn.cursor()
            cur3.execute(
                "UPDATE import_runs SET status = 'failed', error = %s WHERE id = %s",
                (f"Count mismatch: parsed {parsed} questions but wrote {written} (duplicates or bad references). Rolled back — nothing was written.", run_id),
            )
            conn.commit()
            raise HTTPException(status_code=409, detail=f"Count mismatch: parsed {parsed}, wrote {written}. Rolled back, nothing was written.")

        cur2.execute(
            "UPDATE import_runs SET status = 'applied', written_questions = %s, written_question_ids = %s, applied_at = now() WHERE id = %s",
            (written, _json.dumps(written_ids), run_id),
        )
        conn.commit()
        return {"id": run_id, "status": "applied", "writtenQuestions": written}
    finally:
        conn.close()


@app.post("/api/admin/imports/{run_id}/rollback/")
def admin_rollback_import(run_id: int, user: dict = Depends(require_cap("import.run"))):
    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM import_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Import run not found")
        if run["status"] != "applied":
            raise HTTPException(status_code=409, detail=f"Run is '{run['status']}', not 'applied' — nothing to roll back")

        ids = run["written_question_ids"]
        cur2 = conn.cursor()
        if ids:
            cur2.execute("DELETE FROM questions WHERE id = ANY(%s)", (ids,))
        cur2.execute("UPDATE import_runs SET status = 'rolled_back' WHERE id = %s", (run_id,))
        conn.commit()
        return {"status": "ok", "deleted": len(ids)}
    finally:
        conn.close()
