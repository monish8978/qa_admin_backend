import sys
import os
import argparse
from datetime import datetime, timezone

# Add parent directory to path so app can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import app components
from app.db import SessionLocal
from app.models.master import Tenant, User, Subscription
from app.security import hash_password

def create_super_admin(email, password, name, tenant_slug, tenant_name):
    session = SessionLocal()
    try:
        # Check if Tenant exists
        tenant = session.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not tenant:
            print(f"Creating workspace '{tenant_name}' (slug: {tenant_slug})...")
            tenant = Tenant(
                slug=tenant_slug,
                name=tenant_name,
                plan="ENTERPRISE",
                status="ACTIVE"
            )
            session.add(tenant)
            session.flush()

            # Create subscription
            now = datetime.now(timezone.utc)
            subscription = Subscription(
                tenantId=tenant.id,
                plan="ENTERPRISE",
                status="ACTIVE",
                currentPeriodStart=now,
                currentPeriodEnd=now,
            )
            session.add(subscription)
            session.flush()
        else:
            print(f"Workspace with slug '{tenant_slug}' already exists. Using it.")

        # Check if User exists in this tenant
        user = session.query(User).filter(User.tenantId == tenant.id, User.email == email).first()
        if user:
            print(f"User with email '{email}' already exists in workspace '{tenant_slug}'. Upgrading to Super Admin...")
            user.role = "ADMIN"
            user.status = "ACTIVE"
            if password:
                user.passwordHash = hash_password(password)
        else:
            print(f"Creating Super Admin user '{name}' ({email})...")
            user = User(
                tenantId=tenant.id,
                email=email,
                name=name,
                passwordHash=hash_password(password or "SuperAdmin123!"),
                role="ADMIN",
                status="ACTIVE"
            )
            session.add(user)

        session.commit()
        print("\nSUCCESS: Super Admin created successfully!")
        print(f"Workspace Slug: {tenant_slug}")
        print(f"Login Email   : {email}")
        print(f"Password      : {password or 'SuperAdmin123!'}")
        print("\nYou can now log in using these credentials.")
    except Exception as e:
        session.rollback()
        print(f"\nERROR: Failed to create Super Admin: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a Super Admin user and workspace.")
    parser.add_argument("--email", default="superadmin@qa.com", help="Email for the super admin")
    parser.add_argument("--password", default="SuperAdmin123!", help="Password for the super admin")
    parser.add_argument("--name", default="Super Admin", help="Name of the super admin user")
    parser.add_argument("--slug", default="admin", help="Tenant slug for the workspace (default: admin)")
    parser.add_argument("--workspace-name", default="Super Admin Workspace", help="Name of the workspace")

    args = parser.parse_args()
    create_super_admin(args.email, args.password, args.name, args.slug, args.workspace_name)
