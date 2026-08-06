#!/bin/bash

# Configuration
BACKUP_DIR="/Czentrix/apps/backup_postql_data"
LOG_DIR="/var/log/czentrix"
CONTAINER_NAME="qa_admin_postgres"
DB_USER="postgres"
DATE=$(date +%F_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/db_backup_${DATE}.sql"

# Create directories if they don't exist
mkdir -p "${BACKUP_DIR}"
mkdir -p "${LOG_DIR}"

# Redirect all stdout/stderr to the log file (append mode)
exec >> "${LOG_DIR}/backup.log" 2>&1

# Perform PostgreSQL backup inside docker container
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting PostgreSQL backup for container: ${CONTAINER_NAME}..."
docker exec -t "${CONTAINER_NAME}" pg_dumpall -U "${DB_USER}" > "${BACKUP_FILE}"

if [ $? -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup successfully created at: ${BACKUP_FILE}"
  # Keep only the last 30 backups to save disk space
  find "${BACKUP_DIR}" -name "db_backup_*.sql" -type f -mtime +30 -delete
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup of backups older than 30 days completed."
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Error: PostgreSQL backup failed!"
  exit 1
fi
