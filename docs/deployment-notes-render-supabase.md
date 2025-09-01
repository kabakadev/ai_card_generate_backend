# Flashlearn Backend – Deployment Notes

## 📌 Overview

This repo contains the Flask + SQLAlchemy backend for Flashlearn, deployed on Render with a Supabase Postgres database.

The deployment journey uncovered several challenges related to:

- Python 3.13 library compatibility
- Database connection parsing errors
- Supabase pooler quirks (transaction vs session)
- Migration conflicts

This README captures the issues and how we overcame them, so future developers don’t fall into the same traps.

---

## 🚀 Deployment Steps

### 1. Environment Setup

- Backend built with Flask, Flask-SQLAlchemy, Alembic/Flask-Migrate, Gunicorn.
- Database hosted on Supabase (Postgres 15).
- Deployment target: Render free web service.

### 2. Key Configuration

In Render → **Environment Variables**:

```bash
# App secrets
SECRET_KEY=your-secret-key
JWT_SECRET_KEY=your-jwt-secret

# Production DB (transaction pooler – for app runtime)
DATABASE_URL=postgresql+psycopg2://postgres.<project-ref>:<PASSWORD>@aws-1-eu-north-1.pooler.supabase.com:6543/postgres?sslmode=require
```

⚠️ Do not prefix the value with `DATABASE_URL=....` — Render already handles `KEY=VALUE`.

---

## 🛑 Challenges & Fixes

### ❌ 1. SQLAlchemy AssertionError on Python 3.13

**Error:**

```
AssertionError: Class SQLCoreOperations directly inherits TypingOnly...
```

**Cause:** SQLAlchemy 2.0.23 not fully compatible with Python 3.13.

**Fix:** Upgraded dependencies:

```txt
SQLAlchemy>=2.0.36
alembic>=1.16.5
Flask>=3.0.3
Werkzeug>=3.1.3
```

---

### ❌ 2. Could not parse SQLAlchemy URL

**Error:**

```
Could not parse SQLAlchemy URL from string 'DATABASE_URL=postgresql+psycopg2://...'
```

**Cause:** The env var value mistakenly included `DATABASE_URL=`.

**Fix:** In Render, set:

```bash
Key: DATABASE_URL
Value: postgresql+psycopg2://postgres.<project-ref>:<PASSWORD>@...:6543/postgres?sslmode=require
```

---

### ❌ 3. Supabase connection failures

**Error:**

```
psycopg2.OperationalError: could not translate host name "aws-1-eu-north-1.supabase.com"
```

**Cause:** Used the wrong host (`aws-1-eu-north-1.supabase.com`). Supabase only supports:

- `db.<project-ref>.supabase.co:5432` (direct, IPv6-only)
- `aws-1-eu-north-1.pooler.supabase.com:5432` (session pooler, safe for migrations)
- `aws-1-eu-north-1.pooler.supabase.com:6543` (transaction pooler, for app runtime)

**Fix:**

- For migrations: use the **session pooler (5432)**.
- For production runtime: use the **transaction pooler (6543)**.

---

### ❌ 4. Migration script failing (relation does not exist)

**Error:**

```
sqlalchemy.exc.ProgrammingError: relation "progress" does not exist
```

**Cause:** Alembic generated `ALTER TABLE` instead of `CREATE TABLE`, because the first migration was generated from an existing local DB.

**Fix:** Reset migrations:

```bash
rm -rf migrations/versions/*
flask db revision --autogenerate -m "initial schema"
flask db upgrade
```

This created tables from scratch in Supabase.

---

## ✅ Current Workflow

### Running Migrations

Always run migrations against the **session pooler (5432)**:

```bash
export DATABASE_URL="postgresql+psycopg2://postgres.<project-ref>:<PASSWORD>@aws-1-eu-north-1.pooler.supabase.com:5432/postgres?sslmode=require"
flask db upgrade
```

### Runtime

Deployed app uses the **transaction pooler (6543)** in Render.

### Healthcheck (optional)

Add a simple endpoint to test DB connectivity:

```python
from sqlalchemy import text

@app.get("/__dbcheck__")
def dbcheck():
    db.session.execute(text("SELECT 1"))
    return {"ok": True}, 200
```

---

## 🔐 Production Checklist

- CORS restricted to frontend domain
- `JWT_SECRET_KEY` set in Render
- Alembic migrations applied via session pooler
- Gunicorn logs enabled
- Tables verified in Supabase

---

## 🎉 Outcome

After resolving these issues:

- `/signup` and `/login` endpoints work correctly against Supabase.
- The backend is stable on Render using Python 3.13.
- Future migrations and deployments have a clear workflow.
