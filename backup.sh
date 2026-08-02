#!/bin/bash

# Configuration
BACKUP_DIR="/Czentrix/apps/backup_postql_data"
CONTAINER_NAME="qa_admin_postgres"
DB_USER="postgres"
DATE=$(date +%F_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/db_backup_${DATE}.sql"

# Create backup directory if it doesn't exist
mkdir -p "${BACKUP_DIR}"

# Perform PostgreSQL backup inside docker container
echo "Starting PostgreSQL backup for container: ${CONTAINER_NAME}..."
docker exec -t "${CONTAINER_NAME}" pg_dumpall -U "${DB_USER}" > "${BACKUP_FILE}"

if [ $? -eq 0 ]; then
  echo "Backup successfully created at: ${BACKUP_FILE}"
  # Keep only the last 30 backups to save disk space
  find "${BACKUP_DIR}" -name "db_backup_*.sql" -type f -mtime +30 -delete
  echo "Cleanup of backups older than 30 days completed."
else
  echo "Error: PostgreSQL backup failed!" >&2
  exit 1
fi
