#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="$(basename "$ROOT")"
VER="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || echo 1.0.0)"
OUT_DIR="$(dirname "$ROOT")"
OUT="$OUT_DIR/${NAME}-${VER}.tar.gz"

tar -C "$OUT_DIR" \
  --exclude='*.pyc' \
  --exclude='__pycache__' \
  --exclude='.git' \
  -czvf "$OUT" "$NAME"

echo "Created: $OUT"
ls -lh "$OUT"
