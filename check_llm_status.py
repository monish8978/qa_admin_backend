from app.config import get_settings
from sqlalchemy import create_engine, text
from app.db import _normalize_pg_url
from app.common.encryption import decrypt
import traceback

settings = get_settings()
e = create_engine(_normalize_pg_url(settings.MASTER_DATABASE_URL))

#print("=== LLM CONFIGS ===")
with e.connect() as c:
    cfgs = c.execute(text('SELECT "tenantId", provider, model, endpoint, "apiKeyEnc", enabled FROM llm_configs')).fetchall()
    for cfg in cfgs:
        try:
            k = decrypt(cfg[4])
            k_masked = k[:6] + "..." + k[-4:] if len(k) > 10 else "too short"
        except Exception as err:
            k_masked = f"error decrypting: {err}"
        print(f"Tenant: {cfg[0]} | Provider: {cfg[1]} | Model: {cfg[2]} | Endpoint: {cfg[3]} | Key: {k_masked} | Enabled: {cfg[5]}")

print("\n=== RECENT EVALUATIONS ===")
# Let's list some tenants to find their DB connections
with e.connect() as c:
    tenants = c.execute(text('SELECT id, slug, "dbHost", "dbPort", "dbName", "dbUser", "dbPasswordEnc" FROM tenants')).fetchall()
    for t in tenants:
        print(f"Tenant: {t[1]} (id={t[0]})")
        try:
            t_pass = decrypt(t[6])
            # Connect to tenant DB
            t_url = f"postgresql://{t[5]}:{t_pass}@{t[2]}:{t[3]}/{t[4]}"
            te = create_engine(t_url)
            with te.connect() as tc:
                evals = tc.execute(text(
                    'SELECT e.id, e."workflowState", e."aiScore", e."aiMetadata", c.channel, e."createdAt" '
                    'FROM evaluations e JOIN conversations c ON c.id=e."conversationId" '
                    'ORDER BY e."createdAt" DESC LIMIT 5'
                )).fetchall()
                for ev in evals:
                    print(f"  Eval: {ev[0]} | State: {ev[1]} | Score: {ev[2]} | Channel: {ev[4]} | Created: {ev[5]}")
                    if ev[3]:
                        print(f"    Metadata: {ev[3]}")
        except Exception as err:
            print(f"  Error reading tenant DB: {err}")
