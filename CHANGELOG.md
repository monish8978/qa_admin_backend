# Changelog

All notable changes to the QA Admin Backend project will be documented in this file.

## [1.1.0] - Architecture & Database Migration Update

### Added
- **Dockerized PostgreSQL**: Integrated PostgreSQL directly into `docker-compose.yml` with persistent volumes (`pgdata`), completely removing the dependency on host-level database installations.
- **Python Native DB Management**: Completely removed the Node.js/Prisma dependency for database schema management. 
- **Automated Master Initialization**: The FastAPI application (`app/main.py`) now automatically creates and initializes the Master Database schema upon startup via `Base.metadata.create_all(engine)`.
- **Alembic Migrations**: Fully configured and documented Alembic for future database schema updates (adding/modifying columns) without data loss.
- **Rotating Loggers**: Implemented a custom rotating file logger outputting to `/var/log/czentrix/qa_smart_admin.log` with a 10MB limit and 5 backups for both API and Celery workers.

### Changed
- **Tenant Provisioning Engine**: Refactored `app/services/provision_task.py` to use SQLAlchemy for dynamic tenant database creation instead of spawning a `prisma migrate deploy` shell command.
- **Enum Handling**: Modified all PostgreSQL Enum bindings in `app/models/master.py` and `app/models/tenant.py` to `create_type=True` so Python creates them automatically.
- **Environment Variables**: Updated `.env` database URLs to route internal traffic to the `postgres` docker container instead of `host.docker.internal`.
- **Tenant Pool Resolution**: Updated `app/services/tenant_pool.py` to map local hostnames to the Docker `postgres` service to ensure cross-container connectivity.
- **Documentation**: Overhauled `README.md` to reflect the removal of Prisma, complete Docker instructions, and Alembic usage steps.

### Fixed
- **Bcrypt Compatibility**: Added a monkey-patch in `app/security.py` to resolve the `AttributeError: module 'bcrypt' has no attribute '__about__'` crash caused by outdated `passlib` assumptions.
- **Provisioning Race Condition**: Fixed a critical bug in `app/services/auth_service.py` where the signup endpoint failed to commit the Master DB transaction before sending the task to Celery, which previously caused `RuntimeError: Tenant missing` crashes.
- **Docker Build Error**: Removed deprecated `start.sh` references from the `Dockerfile` that were causing the image build to fail.

### Removed
- **Legacy Scripts**: Deleted outdated files including `start.sh`, `uvicorn.log`, the `apps` legacy directory, and python `.egg-info` build artifacts.
- **Host Dependencies**: Cleaned `install.sh` to remove all instructions for installing Redis and PostgreSQL directly on the host server.
