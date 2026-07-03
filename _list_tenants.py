from app.config import get_settings
from sqlalchemy import create_engine, text
from app.db import _normalize_pg_url

settings = get_settings()
e = create_engine(_normalize_pg_url(settings.MASTER_DATABASE_URL))
with e.connect() as c:
    rows = c.execute(text(
        'SELECT slug, status, "dbHost", "dbPort", "dbName", "dbUser" FROM tenants WHERE slug=\'dev-tenant\''
    )).fetchall()
    for r in rows:
        print("TENANT:", r)
    rows = c.execute(text(
        'SELECT email, role, status FROM users u JOIN tenants t ON t.id=u."tenantId" WHERE t.slug=\'dev-tenant\' AND role=\'ADMIN\' LIMIT 5'
    )).fetchall()
    for r in rows:
        print("ADMIN:", r)
