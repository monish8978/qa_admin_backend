# SmartQA Platform - Backend (qa_admin_backend)

Python FastAPI Backend for the **SmartQA Multi-Tenant Evaluation Suite**. Manages multi-tenant database provisioning, AI/QA evaluations, dynamic scorecards, analytics, and authentication.

---

## Tech Stack

| Concern        | Tech Stack                         |
| -------------- | ---------------------------------- |
| Framework      | FastAPI (Python 3.12)              |
| ORM            | SQLAlchemy 2.0 + Alembic           |
| Database       | PostgreSQL 16 (Master + Tenant DBs)|
| Cache & Queue  | Redis 7 + Celery                   |
| Authentication | JWT (HS256) + bcrypt + MFA OTP     |
| Validation     | Pydantic v2                        |
| CORS           | Dynamic Regex (allow_origin_regex) |

---

## Key Features

- **Multi-Tenant Architecture:** Dynamic tenant database provisioning and tenant-scoped connection pooling.
- **Dynamic CORS Handling:** Supports cross-origin credentialed requests from any host (https?://.*).
- **Flexible Auth:** Supports email/password, MFA OTP verification, and workspace slug resolution (tenantSlug).
- **Background Tasks:** Celery workers for async tenant provisioning and automated evaluation processing.

---

## Environment Configuration (.env)

Create .env inside qa_admin_backend/:

`env
NODE_ENV=production
API_URL=http://localhost:8005
WEB_URL=http://localhost:3001

# Master DB Connection
MASTER_DATABASE_URL=postgresql://qa_master:masterpass@localhost:5432/qa_master

# Tenant DB Superuser Credentials
TENANT_DB_HOST=localhost
TENANT_DB_PORT=5433
TENANT_DB_SUPERUSER=qa_superuser
TENANT_DB_SUPERUSER_PASSWORD=superpass

# Redis & Celery
REDIS_ENABLED=true
REDIS_HOST=localhost
REDIS_PORT=6379

# Security Secrets
JWT_SECRET=dev_jwt_secret_minimum_32_characters_here
JWT_EXPIRES_IN=15m
REFRESH_SECRET=dev_refresh_secret_minimum_32_characters_here
REFRESH_EXPIRES_IN=30d
`

---

## Running the Backend

### Option A: Docker Compose (Full Stack)

`ash
cd /Czentrix/apps/qa_admin_backend

# Start Postgres, Redis, API, and Celery worker
docker compose up -d

# Check running status
docker compose ps
`

### Option B: Local / Uvicorn Server

`ash
cd /Czentrix/apps/qa_admin_backend

# 1. Activate virtual environment
source .venv/bin/activate

# 2. Run FastAPI Uvicorn Server (Port 8005)
uvicorn app.main:app --host 0.0.0.0 --port 8005 --workers 4
`

---

## API & Documentation Links

- **API Base Endpoint:** http://localhost:8005/api/v1
- **Swagger Documentation:** http://localhost:8005/api/docs
- **Health Check:** http://localhost:8005/api/v1/health

---

## Directory Structure

`
qa_admin_backend/
+-- app/
|   +-- main.py              # FastAPI Application Entrypoint (CORS, Middlewares, Routes)
|   +-- config.py            # Pydantic Settings loader
|   +-- db.py                # SQLAlchemy engine & session factory
|   +-- security.py          # Password hashing, JWT signing/verification
|   +-- deps.py              # Dependencies (Auth guard, DB session)
|   +-- worker.py            # Celery worker configuration
|   +-- models/              # SQLAlchemy Models (Master & Tenant schemas)
|   +-- schemas/             # Pydantic Request/Response DTOs
|   +-- services/            # Core logic (AuthService, Evaluations, TenantPool, etc.)
|   +-- routers/             # API Endpoints (auth, evaluations, forms, analytics, etc.)
+-- alembic/                 # Alembic Database Migration scripts
+-- docker-compose.yml       # Production/Dev Docker orchestration
+-- Dockerfile               # Container build instructions
+-- pyproject.toml           # Project dependencies & build config
`

---

## Database Backup & Restore Guide

### 1. Data Safety & Persistence
The PostgreSQL database container maps its data directory to the host filesystem:
- Host path: `qa_admin_backend/postgres_data`
- Container path: `/var/lib/postgresql/data`

Even if `docker-compose down -v` is executed, the database files are preserved safely in the host directory `postgres_data`.

### 2. Manual PostgreSQL Backup
A backup script `backup.sh` is located in `qa_admin_backend/`. To run a manual point-in-time snapshot backup:
```bash
cd /Czentrix/apps/qa_admin_backend
./backup.sh
```
This script creates a complete SQL dump of the master and tenant databases inside `/Czentrix/apps/backup_postql_data/` and automatically deletes backup files older than 30 days.

### 3. Database Restore / Recovery
To restore a backup into a clean database instance:
1. Stop the containers, clean old data directories, and spin up fresh containers:
   ```bash
   cd /Czentrix/apps/qa_admin_backend
   docker-compose down
   rm -rf postgres_data
   docker-compose up -d
   ```
2. Restore the database using the target `.sql` dump file:
   ```bash
   docker exec -i qa_admin_postgres psql -U postgres < /Czentrix/apps/backup_postql_data/db_backup_YYYY-MM-DD_HHMMSS.sql
   ```

### 4. Automated Backup (Crontab Scheduler)
To automate daily backups at 12:00 AM:
1. Open the crontab scheduler:
   ```bash
   crontab -e
   ```
2. Append the following cron job definition:
   ```bash
   0 0 * * * /bin/bash /Czentrix/apps/qa_admin_backend/backup.sh > /Czentrix/apps/qa_admin_backend/backup.log 2>&1
   ```

---

## License
Internal Proprietary Software - C-Zentrix / Towards Vision Technologies.