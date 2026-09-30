# Keycloak realm

`realm-anime-picker.json` is imported on Keycloak's first start (`start-dev --import-realm`).
The settings it holds are described in `infra/README.md#keycloak`.

- The client secret is the placeholder `${KEYCLOAK_CLIENT_SECRET}`, and the SMTP host and
  port are `${KC_SMTP_HOST:mailpit}` / `${KC_SMTP_PORT:1025}`. Keycloak fills them in from
  its environment at import time.
- Import runs only when the realm doesn't exist yet. After changing the file locally, drop
  the realm (or the `keycloak` database) and restart Keycloak.
- After changing the realm in the admin console, re-export it
  (`kc.sh export --realm anime-picker --file ...`). Put the placeholders back in place of
  the exported secret, drop the generated ids and keys, and commit.
- `services/api` tests the realm end to end with `RealKeycloakTest`, which needs
  `API_TEST_KEYCLOAK_URL` and `API_TEST_KEYCLOAK_SECRET`.
