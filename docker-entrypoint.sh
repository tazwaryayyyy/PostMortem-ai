#!/bin/sh
# Seed static incident files into the named volume on first container start.
# Built-in incidents (incident_a … incident_e) are preserved in /app/incidents_static
# during the Docker build. This entrypoint copies them into the mounted volume only
# when the file does not already exist, so user-generated incidents are never overwritten.
for f in /app/incidents_static/*.json; do
    [ -f "$f" ] || continue
    dest="/app/incidents/$(basename "$f")"
    [ -f "$dest" ] || cp "$f" "$dest"
done

exec "$@"
