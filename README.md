<<<<<<< HEAD
# qa_admin_backend



## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

- [ ] [Create](https://docs.gitlab.com/ee/user/project/repository/web_editor.html#create-a-file) or [upload](https://docs.gitlab.com/ee/user/project/repository/web_editor.html#upload-a-file) files
- [ ] [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://czscm.c-zentrix.com/c-zentrix/cz-voice/qa_module/qa_admin_backend.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

- [ ] [Set up project integrations](https://czscm.c-zentrix.com/c-zentrix/cz-voice/qa_module/qa_admin_backend/-/settings/integrations)

## Collaborate with your team

- [ ] [Invite team members and collaborators](https://docs.gitlab.com/ee/user/project/members/)
- [ ] [Create a new merge request](https://docs.gitlab.com/ee/user/project/merge_requests/creating_merge_requests.html)
- [ ] [Automatically close issues from merge requests](https://docs.gitlab.com/ee/user/project/issues/managing_issues.html#closing-issues-automatically)
- [ ] [Enable merge request approvals](https://docs.gitlab.com/ee/user/project/merge_requests/approvals/)
- [ ] [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

- [ ] [Get started with GitLab CI/CD](https://docs.gitlab.com/ee/ci/quick_start/)
- [ ] [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/ee/user/application_security/sast/)
- [ ] [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/ee/topics/autodevops/requirements.html)
- [ ] [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/ee/user/clusters/agent/)
- [ ] [Set up protected environments](https://docs.gitlab.com/ee/ci/environments/protected_environments.html)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
=======
# `apps/api-py` — Python (FastAPI) backend for the QA Platform

> Status: **Active backend — full port complete.** All 49 REST endpoints
> validated end-to-end (`pass=49 fail=0` via `manual_smoke.py`). The legacy
> NestJS service under `apps/api` is retained for reference only.
> **Database Architecture Update:** Prisma dependency has been entirely removed.
> Python SQLAlchemy fully manages the master and tenant PostgreSQL schemas, dynamically
> creating databases, tables, and enum types (`create_type=True`). The entire infrastructure
> (API, Celery Worker, Redis, and PostgreSQL) is containerized via Docker Compose.

## Stack

| Concern        | Choice                            |
| -------------- | --------------------------------- |
| HTTP framework | FastAPI                           |
| ORM            | SQLAlchemy 2.0 (sync) + Alembic   |
| DB driver      | psycopg 3                         |
| Cache / locks  | redis-py                          |
| Queues         | Celery (replaces BullMQ)          |
| Auth           | bcrypt (passlib) + JWT (HS256)    |
| Validation     | Pydantic v2                       |

## Layout

```
apps/api-py/
├─ app/
│  ├─ main.py             # FastAPI entrypoint (mirrors apps/api/src/main.ts)
│  ├─ config.py           # env via pydantic-settings (mirrors packages/config)
│  ├─ db.py               # SQLAlchemy engine/session
│  ├─ security.py         # bcrypt, JWT, SHA-256 helpers
│  ├─ deps.py             # FastAPI deps (DB, current user, role guard)
│  ├─ redis_client.py
│  ├─ celery_app.py
│  ├─ worker.py           # Celery worker entrypoint
│  ├─ common/             # enums, response envelope, exceptions
│  ├─ models/             # SQLAlchemy models for the master DB
│  ├─ schemas/            # Pydantic request/response schemas
│  ├─ services/           # AuthService, UsersService, NotifyService stub
│  └─ routers/            # auth, users, health
├─ alembic/               # Alembic migrations (Python now fully manages
│                         # the schema, autogenerate ready)
├─ alembic.ini
├─ Dockerfile
├─ pyproject.toml
└─ .env.example
```

## Setup & Run

The entire stack is containerized. To spin up the FastAPI backend, Celery worker, Redis, and PostgreSQL database with persistent volumes:

```bash
docker-compose down
docker-compose up -d --build
```

*Note: The Python FastAPI backend automatically initializes the master database schema on startup, and the Celery background worker automatically creates and initializes tenant databases dynamically upon provisioning.*

OpenAPI / Swagger UI: <http://127.0.0.1:8005/api/docs> (non-production only).

## Database Migrations (Alembic)

When you make changes to the database structure in your Python code (e.g., adding a new column to a table like `phone_number`), you need to run Alembic migrations so that the database structure updates without losing your existing data.

1. **Generate the migration script** (run this after modifying models):
```bash
docker-compose exec api alembic revision --autogenerate -m "added_new_column"
```
*(Alembic will automatically compare your new code with the running database and generate the appropriate update script).*

2. **Apply the migration**:
```bash
docker-compose exec api alembic upgrade head
```
*(This safely modifies the database tables while keeping all your old data completely intact).*

## Smoke test

A full HTTP smoke suite covering all 49 endpoints ships with the app:

```powershell
python apps/api-py/manual_smoke.py http://127.0.0.1:8005
```

Expected output: `RESULT: pass=49 fail=0`. The script logs in as the seeded
`admin@dev.local` / `DevAdmin123!` user against tenant slug `dev-tenant`,
falling back to signup when the seed is absent.

## What's ported

All NestJS modules are now available in Python. Endpoint inventory:

**Public / health**

- `GET  /api/v1/health` · `/health/ready` · `/health/metrics`
- `GET  /api/v1/openapi.json` · `/api/docs`

**Master-DB modules**

- `POST /api/v1/auth/signup` · `login` · `refresh` · `logout`
- `POST /api/v1/auth/forgot-password` · `reset-password` · `accept-invite`
- `GET  /api/v1/auth/me`
- `GET/POST/PATCH/DELETE /api/v1/users` (ADMIN; invite, last-admin guard)
- `GET/POST/PATCH/DELETE /api/v1/departments` (channel-overlap guard)
- `GET/PATCH /api/v1/settings/escalation`
- `GET/PATCH /api/v1/settings/blind-review`
- `GET/PATCH /api/v1/settings/email` · `POST /api/v1/settings/email/test`
- `GET  /api/v1/settings/onboarding-status`
- `GET/PUT  /api/v1/llm-config` · `POST /api/v1/llm-config/test`
  (OpenAI / Azure / Custom endpoint validation + 12 s healthcheck)
- `GET/POST/PATCH /api/v1/outbound-webhooks` · `GET .../deliveries`
- `GET/POST/PATCH /api/v1/routing/mappings` · `GET .../stats` · `/audits` · `/settings`
- `GET  /api/v1/billing/subscription` · `/billing/usage`
- `POST /api/v1/billing/stripe/webhook`

**Tenant-DB modules** (resolved via `TenantPool`, decrypts `dbPasswordEnc`)

- `GET/POST/PATCH /api/v1/forms` · `POST /forms/{id}/status`
- `GET/POST/PATCH /api/v1/conversations`
- `GET  /api/v1/evaluations` · `/evaluations/queue/{qa,audit,escalation,verifier}`
- `GET  /api/v1/evaluations/logs/prompt-audit`
- `GET  /api/v1/analytics/{overview,agent-performance,ai-usage-trends,conversation-volume,deviation-trends,escalation-stats,form-score-distribution,qa-reviewer-performance,question-deviations,rejection-reasons,score-trends,sla-report,verifier-overrides,verifier-report}`
- `POST /api/v1/webhooks/ingest`

**Cross-cutting**

- AES-256-GCM encryption util (`app/common/encryption.py`) — byte-compatible
  with `apps/api/src/common/utils/encryption.util.ts`.
- SQLAlchemy natively manages Postgres enum types (`PlanType`, `TenantStatus`,
  `UserRole`, `UserStatus`, `SubscriptionStatus`, `Channel`, `ConvStatus`,
  `FormStatus`, `WorkflowState`, `DeviationType`, `QueueType`) with
  `create_type=True, native_enum=True, validate_strings=True`.
- Per-tenant Postgres pool (`app/services/tenant_pool.py`) with
  30-min idle reaper + 1-h Redis heartbeat.
- Real SMTP notify (`app/services/notify_service.py`) — per-tenant
  `TenantEmailSettings` with platform-SMTP fallback, templates
  `tenant_ready` · `user_invited` · `password_reset`.
- Celery task wiring (`tenant.provision`, `eval.process`, `eval.escalate`,
  `notify.send`, `billing.usage.sync`, `report.export`) replaces BullMQ.
- Prometheus metrics + structlog wiring.
>>>>>>> e043e17 (Initial commit)
