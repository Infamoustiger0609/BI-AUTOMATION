#!/usr/bin/env sh
set -eu

BACKUP_DIR="${BACKUP_DIR:-./backup}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
tar -czf "$BACKUP_DIR/prompt2pbi-config-$TIMESTAMP.tar.gz" \
  .env.dev .env.staging .env.prod docker deploy README.md

echo "Backup created at $BACKUP_DIR/prompt2pbi-config-$TIMESTAMP.tar.gz"

