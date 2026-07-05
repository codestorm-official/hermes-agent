#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace \
  /data/.hermes/platforms/pairing

exec python /app/server.py
