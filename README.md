# SmartQA Platform - Backend (`qa_admin_backend`)

Python FastAPI backend for the **SmartQA Multi-Tenant Evaluation Suite**. It manages multi-tenant database provisioning, AI/QA evaluations, dynamic scorecards, analytics, authentication, and background processing.

---

# Tech Stack

| Component | Technology |
|-----------|------------|
| Framework | FastAPI (Python 3.12) |
| ORM | SQLAlchemy 2.0 + Alembic |
| Database | PostgreSQL 16 (Master + Tenant Databases) |
| Cache & Queue | Redis 7 + Celery |
| Authentication | JWT (HS256), bcrypt, MFA OTP |
| Validation | Pydantic v2 |
| Background Tasks | Celery |
| CORS | Dynamic Regex (`allow_origin_regex`) |

---

# Features

- Multi-tenant architecture with automatic tenant database provisioning.
- Tenant-scoped database connection pooling.
- Dynamic CORS support (`https?://.*`) with credentials.
- Email & Password authentication.
- MFA OTP verification.
- Workspace (Tenant Slug) based authentication.
- Dynamic scorecards and evaluation workflows.
- Analytics and reporting APIs.
- Celery background workers for asynchronous processing.
- Redis caching and task queue.

---

# Environment Configuration

Create a `.env` file inside **qa_admin_backend/**

```env
NODE_ENV=production

API_URL=http://localhost:8005
WEB_URL=http://localhost:3001

# Master Database
MASTER_DATABASE_URL=postgresql://qa_master:masterpass@localhost:5432/qa_master

# Tenant Database
TENANT_DB_HOST=localhost
TENANT_DB_PORT=5433
TENANT_DB_SUPERUSER=qa_superuser
TENANT_DB_SUPERUSER_PASSWORD=superpass

# Redis
REDIS_ENABLED=true
REDIS_HOST=localhost
REDIS_PORT=6379

# JWT
JWT_SECRET=dev_jwt_secret_minimum_32_characters_here
JWT_EXPIRES_IN=15m

REFRESH_SECRET=dev_refresh_secret_minimum_32_characters_here
REFRESH_EXPIRES_IN=30d
```

---

# Running the Backend

## Option 1 - Docker Compose

```bash
cd /Czentrix/apps/qa_admin_backend

# Build & Start Services
docker compose up -d

# Check running containers
docker compose ps
```

---

## Option 2 - Local Installation

```bash
cd /Czentrix/apps/qa_admin_backend

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run database migrations
alembic upgrade head

# Start FastAPI
uvicorn app.main:app --host 0.0.0.0 --port 8005 --workers 4
```

---

# API Endpoints

| Service | URL |
|----------|-----|
| Base API | http://localhost:8005/api/v1 |
| Swagger UI | http://localhost:8005/api/docs |
| ReDoc | http://localhost:8005/api/redoc |
| Health Check | http://localhost:8005/api/v1/health |

---

# Project Structure

```text
qa_admin_backend/
│
├── app/
│   ├── main.py               # FastAPI Entry Point
│   ├── config.py             # Environment Configuration
│   ├── db.py                 # Database Configuration
│   ├── security.py           # JWT & Password Hashing
│   ├── deps.py               # Dependency Injection
│   ├── worker.py             # Celery Worker
│   │
│   ├── models/
│   ├── schemas/
│   ├── services/
│   └── routers/
│
├── alembic/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── backup.sh
└── README.md
```

---

# Service Status & Health Check

## 1. Check Docker Containers

```bash
docker compose ps
```

or

```bash
docker ps
```

Expected running containers:

- qa_admin_postgres
- qa_admin_redis
- qa_admin_backend_api
- qa_admin_worker

Status should be **Up**.

---

## 2. Check Backend API

```bash
curl http://localhost:8005/api/v1/health
```

Expected response:

```json
{
  "status": "healthy"
}
```

---

## 3. Check Redis

```bash
docker exec -it qa_admin_redis redis-cli ping
```

Expected output:

```text
PONG
```

---

## 4. Check PostgreSQL

```bash
docker exec -it qa_admin_postgres pg_isready
```

Expected output:

```text
accepting connections
```

---

## 5. Check Celery Worker

```bash
docker logs -f qa_admin_worker
```

or

```bash
docker compose logs -f worker
```

Healthy worker output:

```text
Connected to redis://...
Ready.
```

---

## 6. Check Backend Logs

```bash
docker logs -f qa_admin_backend_api
```

or

```bash
docker compose logs -f api
```

---

## 7. Check Local Processes (Without Docker)

Check FastAPI:

```bash
ps -ef | grep uvicorn
```

Check Celery:

```bash
ps -ef | grep celery
```

---

## 8. Check Listening Port

```bash
ss -tulnp | grep 8005
```

or

```bash
netstat -tulnp | grep 8005
```

Expected output:

```text
LISTEN 0 128 0.0.0.0:8005
```

---

# Database Backup & Restore

## Data Persistence

PostgreSQL data is mapped to the host system.

| Host Path | Container Path |
|-----------|----------------|
| `postgres_data/` | `/var/lib/postgresql/data` |

The database remains safe even if containers are recreated.

---

## Manual Backup

```bash
cd /Czentrix/apps/qa_admin_backend

./backup.sh
```

Backups are stored in:

```text
/Czentrix/apps/backup_postql_data/
```

Backups older than **30 days** are automatically deleted.

---

## Restore Database

Create a fresh PostgreSQL instance:

```bash
docker compose down

rm -rf postgres_data

docker compose up -d
```

Restore database:

```bash
docker exec -i qa_admin_postgres \
psql -U postgres \
< /Czentrix/apps/backup_postql_data/db_backup_YYYY-MM-DD_HHMMSS.sql
```

---

## Automatic Daily Backup

Open crontab:

```bash
crontab -e
```

Add the following entry:

```bash
0 0 * * * /bin/bash /Czentrix/apps/qa_admin_backend/backup.sh >> /Czentrix/apps/qa_admin_backend/backup.log 2>&1
```

This schedules a backup every day at **12:00 AM**.

---

# Useful Docker Commands

## Start Services

```bash
docker compose up -d
```

## Stop Services

```bash
docker compose down
```

## Restart All Services

```bash
docker compose restart
```

## Restart Backend API

```bash
docker compose restart api
```

## Restart Celery Worker

```bash
docker compose restart worker
```

## View All Logs

```bash
docker compose logs -f
```

## View API Logs

```bash
docker compose logs -f api
```

## View Worker Logs

```bash
docker compose logs -f worker
```

## View PostgreSQL Logs

```bash
docker compose logs -f postgres
```

## View Redis Logs

```bash
docker compose logs -f redis
```

---

# License

**Internal Proprietary Software**

**C-Zentrix / Towards Vision Technologies**
