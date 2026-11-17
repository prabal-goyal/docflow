# 0001: Production readiness of authentication (M1)

Date: 2026-11-17. Status: accepted.

## Production-grade as built

- **Passwords** hashed with argon2id (pwdlib defaults).
- **Login** returns one message for wrong password, unknown email and disabled account, and runs a
  password check even for unknown emails, so neither the response nor its timing reveals which
  emails exist.
- **JWT** holds only the user id, signed with HS256 and a required secret of at least 32 characters
  (no default in code). Role and `is_active` are read from the database on every request, so
  downgrades and deactivations take effect immediately rather than when the token expires.
- **Temporary passwords**: admin-created users must change their password before any other call,
  so the admin does not keep a working password for them.
- **Responses** use separate output models; `password_hash` is never serialised.
- **Migrations** run once in a dedicated step before the API starts, which stays safe with multiple
  API replicas. CI checks that migrations match the models and can be rolled back.
- **IDs** are UUIDs, so they cannot be enumerated.

## Acceptable for this product, with an upgrade path

| Choice | Limitation | Upgrade path |
|---|---|---|
| Email + password login | Enterprise customers usually require SSO | OIDC/SAML via the customer's identity provider |
| Admin creates users with a temporary password | Admin briefly knows the password | Email invite links, or SCIM provisioning |
| 30-minute access token, no refresh token | A browser UI would log users out every 30 minutes | Refresh tokens when a UI is added |
| HS256 shared secret | One key, no rotation | Key IDs (`kid`) with rotation, or RS256 if other services verify tokens |

## Must be addressed before running on the internet

| Gap | Risk | Planned |
|---|---|---|
| No login rate limiting | Unlimited password guessing | M6 |
| No HTTPS | Credentials and tokens sent in clear text | M6 (Caddy) |
| No password reset | Locked-out users need an admin | Documented; acceptable for small deployments |
