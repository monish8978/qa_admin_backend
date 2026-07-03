from app.config import get_settings
from app.services.tenant_pool import get_tenant_pool
from sqlalchemy import text

tenant_id = "cmnn5tc65000isvaglid0rqd5"
pool = get_tenant_pool()

with pool.session(tenant_id) as ts:
    rows = ts.execute(text(
        "SELECT id, name, status, channels FROM form_definitions"
    )).fetchall()
    print("ALL FORMS IN DB:")
    for r in rows:
        print(f"ID: {r[0]}, Name: {r[1]}, Status: {r[2]}, Channels: {r[3]}")
