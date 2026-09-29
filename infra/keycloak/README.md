# Keycloak realm

`realm-anime-picker.json` (the realm export described in `infra/README.md#keycloak`)
is not committed yet; it lands with the API service. Until then Keycloak starts
with no realm, and the ingest worker's internal API rejects every token except
on `/healthz`. The admin site (`/admin`) uses local Django users and works without it.
