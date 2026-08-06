"""
One-off migration: load the full 73,405-question / 1,899-paper extraction
(mpsc_bank_converted.json) into the shiksha-dev droplet's existing
mpsc_study Postgres DB, additive to the 164 papers / 6,380 questions
already there (different ID scheme, non-overlapping — see WIP.md).

Run on the droplet itself:
    cd ~/mpsc_api && source venv/bin/activate
    python3 load_into_droplet.py mpsc_bank_converted.json
"""
import json
import os
import sys

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=mpsc_study user=mpsc_user password={}".format(
    os.environ.get("MPSC_DB_PASSWORD", "")
)


def main(json_file):
    with open(json_file) as f:
        data = json.load(f)

    papers = data.get("papers", [])
    questions = data.get("questions", [])
    print(f"Loading {len(papers)} papers and {len(questions)} questions...")

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    paper_rows = []
    for p in papers:
        year = p.get("year")
        try:
            year = int(year) if year not in (None, "", "None") else None
        except (TypeError, ValueError):
            year = None
        if year is None:
            continue  # papers.year is NOT NULL — skip the handful missing it
        paper_rows.append((
            p.get("id"),
            p.get("examType") or "",
            p.get("examName") or "",
            p.get("post") or None,
            p.get("paperNumber") if p.get("paperNumber") not in (None, "None") else None,
            p.get("paperSubject") or "",
            year,
            p.get("sourceFile") or None,
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO papers (id, exam_type, exam_name, post, paper_number, paper_subject, year, source_file)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
        """,
        paper_rows,
    )
    print(f"Papers inserted (or already present): {cur.rowcount}")

    question_rows = []
    skipped_no_paper = 0
    paper_ids_loaded = {p[0] for p in paper_rows}
    for q in questions:
        paper_id = q.get("paperId")
        if paper_id and paper_id not in paper_ids_loaded:
            # paper_id FK is ON DELETE SET NULL, not enforced on insert against
            # a set we didn't load (e.g. paper skipped for missing year) —
            # null it out rather than fail the whole batch.
            skipped_no_paper += 1
            paper_id = None
        question_rows.append((
            q.get("id"),
            paper_id,
            q.get("subject") or "gk",
            q.get("topic") or "",
            q.get("topicLabel") or q.get("topic") or "",
            q.get("difficulty") or "medium",
            q.get("question") or "",
            psycopg2.extras.Json(q.get("options") or []),
            q.get("answerIndex") if q.get("answerIndex") is not None else -1,
            q.get("explanation") or "",
            q.get("source") or "MPSC Old Questions",
            q.get("year"),
            q.get("type") or "mcq",
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO questions (id, paper_id, subject, topic, topic_label, difficulty, question, options, answer_index, explanation, source, year, type)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
        """,
        question_rows,
    )
    print(f"Questions inserted (or already present): {cur.rowcount}")
    print(f"Questions with unresolved paper_id (nulled): {skipped_no_paper}")

    conn.commit()

    cur.execute("SELECT count(*) FROM papers")
    print(f"Total papers now: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM questions")
    print(f"Total questions now: {cur.fetchone()[0]}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mpsc_bank_converted.json")
