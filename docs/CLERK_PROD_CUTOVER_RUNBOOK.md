# Håfa Recipes Clerk Production Cutover Runbook

## Status

- App Store release `2.3.0` is live.
- Current production mobile builds still use the existing Clerk development instance/key.
- Phase 2 is to move production auth to a Clerk production instance without losing user-owned data.

## Why this needs a runbook

Håfa Recipes stores ownership directly using Clerk user IDs (`user_...`). A new Clerk production application will issue different user IDs than the current Clerk development application. If we simply replace Clerk keys, returning users can authenticate successfully but may see an empty account because their recipes/plans/grocery rows still point at the old Clerk development `user_id`.

## Current user-owned data

The API currently stores Clerk user IDs in these tables/columns:

| Table | Column(s) | Purpose |
| --- | --- | --- |
| `recipes` | `user_id` | Recipe owner |
| `saved_recipes` | `user_id` | Public recipes saved by user |
| `collections` | `user_id` | User-created recipe collections |
| `recipe_notes` | `user_id` | Private notes on recipes |
| `recipe_versions` | `created_by` | User who edited/re-extracted |
| `meal_plan_entries` | `user_id` | User's meal plan |
| `grocery_items` | `user_id` | User-created grocery items |
| `grocery_list_members` | `user_id` | Shared grocery list membership |
| `grocery_list_invites` | `created_by`, `accepted_by` | Shared grocery invitations |
| `extraction_jobs` | `user_id` | Async extraction ownership |

## Recommended strategy

Use an email-based, first-login migration bridge.

This is safer than a hard cutover because:

- existing App Store builds can continue to use the current Clerk instance during the rollout;
- the API can temporarily accept both old and new Clerk issuers;
- each returning user can migrate their own data when they first sign in with the production Clerk account;
- we avoid a large manual one-time mapping for every user.

## Required Clerk production setup

In Clerk Dashboard, create/configure a production app for Håfa Recipes.

### Auth methods

Enable the same sign-in methods the iOS app exposes:

- Email/password
- Apple
- Google

If Google remains visible in mobile, it must be configured and working in the production Clerk app.

### Native mobile app

Configure the native application for the iOS bundle:

```text
Bundle ID: com.shimizutechnology.recipeextractor
App scheme: hafarecipes
OAuth redirect: hafarecipes://oauth-callback
```

Keep the current Expo deep-linking behavior unless code changes are made intentionally.

### JWT template

Recreate the JWT template used by mobile exactly:

```text
recipe-extractor-public-metadata
```

Minimum claim for current admin support:

```json
{
  "public_metadata": "{{user.public_metadata}}"
}
```

Preferred claims for migration/display support:

```json
{
  "email": "{{user.primary_email_address.email_address}}",
  "first_name": "{{user.first_name}}",
  "last_name": "{{user.last_name}}",
  "image_url": "{{user.image_url}}",
  "public_metadata": "{{user.public_metadata}}"
}
```

If the email claim is missing, the API migration endpoint can fetch the verified primary email from Clerk's Backend API when `CLERK_SECRET_KEY` is configured. Still prefer including email in the JWT template to avoid an extra network call.

If an `aud` claim is configured, set the same value in the API via `CLERK_JWT_AUDIENCE`.

### Session settings

Set production sessions intentionally long-lived, subject to Clerk plan limits:

- disable inactivity timeout if available;
- set maximum lifetime to the longest appropriate value, e.g. 365 days;
- understand device storage, sign-outs, password resets, and provider policies can still end sessions.

## API changes needed

### 1. Accept both Clerk issuers temporarily

Current API verifies one issuer from `CLERK_FRONTEND_API` / `CLERK_JWT_ISSUER`.

Add temporary support for both:

- old development Clerk issuer;
- new production Clerk issuer.

Suggested env shape:

```text
CLERK_JWT_ISSUERS=https://old-dev-issuer.clerk.accounts.dev,https://new-prod-issuer.clerk.accounts.dev
CLERK_JWT_ISSUER=https://new-prod-issuer.clerk.accounts.dev
CLERK_SECRET_KEY=sk_live_...
CLERK_SECRET_KEYS_BY_ISSUER=https://old-dev-issuer.clerk.accounts.dev=sk_test_...,https://new-prod-issuer.clerk.accounts.dev=sk_live_...
```

`CLERK_SECRET_KEYS_BY_ISSUER` lets account deletion and email fallback call the matching Clerk instance for the token issuer during the transition. `CLERK_SECRET_KEY` remains the fallback/default.

Do not remove old issuer support until enough users have updated to the production-Clerk mobile build.

### 2. Add migration tracking

Run the API migration that creates the mapping/tracking tables:

```bash
PYTHONPATH=. python migrations/016_add_clerk_user_migration_tables.py
```

This creates:

- `legacy_clerk_user_mappings` — maps hashed verified emails to old Clerk development user IDs.
- `clerk_user_migrations` — records completed old→new Clerk user ID migrations.

Use an email hash rather than storing plain emails.

### 3. Build a legacy user map

Export old Clerk development users with:

- old Clerk user ID;
- verified primary email;
- optional name.

Create a local CSV, not committed to git:

```csv
legacy_user_id,email
user_abc123,person@example.com
```

Set `CLERK_MIGRATION_EMAIL_HASH_SECRET` in the API environment and use the same value when importing the CSV.

Import with:

```bash
PYTHONPATH=. python scripts/import_legacy_clerk_users.py /path/to/legacy-clerk-users.csv --dry-run
PYTHONPATH=. python scripts/import_legacy_clerk_users.py /path/to/legacy-clerk-users.csv
```

Do not commit exported user data.

### 4. Migrate by verified email on first prod sign-in

When a user signs in with production Clerk:

1. Read `sub` as new production Clerk user ID.
2. Read verified primary email from JWT claim or Clerk Backend API fallback.
3. Normalize and hash email.
4. Look up old development Clerk user ID for that hash.
5. Call `POST /api/users/me/migrate-legacy` after sign-in/session restore.
6. If not already migrated, update all ownership columns from old ID to new ID inside one transaction.
7. Insert migration record.
8. Return success/no-op status.

Columns to update:

```sql
recipes.user_id
saved_recipes.user_id
collections.user_id
recipe_notes.user_id
recipe_versions.created_by
meal_plan_entries.user_id
grocery_items.user_id
grocery_list_members.user_id
grocery_list_invites.created_by
grocery_list_invites.accepted_by
extraction_jobs.user_id
```

Handle uniqueness conflicts carefully, especially:

- `saved_recipes` unique `(user_id, recipe_id)`;
- `recipe_notes` unique `(user_id, recipe_id)`;
- `grocery_list_members` primary key `(list_id, user_id)`.

### 5. Add tests

API tests should cover:

- old issuer token accepted during transition;
- prod issuer token accepted;
- unrelated issuer rejected;
- migration no-ops when email has no legacy mapping;
- migration updates every ownership table;
- migration is idempotent;
- uniqueness conflicts do not crash or duplicate data.

## Mobile changes needed

Changing `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` requires a new native build because Expo public env vars are baked into the binary.

Release a new app version, likely `2.3.1`, with:

- EAS production env set to the Clerk production publishable key (`pk_live_...`);
- same app scheme and OAuth callback;
- migration endpoint called after sign-in/session restore if the API exposes one;
- no user-visible redesign mixed into this PR.

## Deployment sequence

1. Verify production DB has all current migrations, especially `015_add_extraction_job_user_id.py`.
2. Create a fresh production DB backup/snapshot.
3. Configure Clerk production app.
4. Add API dual-issuer + migration bridge code.
5. Deploy API while still accepting the old Clerk issuer.
6. Smoke test current live App Store build still works.
7. Set EAS production env to `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...`.
8. Build TestFlight `2.3.1`.
9. Test new sign-in/signup through TestFlight:
   - email/password;
   - Apple;
   - Google;
   - account deletion;
   - extraction;
   - existing user data migration.
10. Submit `2.3.1` to App Review.
11. Monitor Sentry, Render logs, Clerk logs, and migration table.
12. After a transition window, remove old Clerk issuer support.

## Rollback plan

Before mobile `2.3.1` is live:

- keep old Clerk issuer support active;
- if API deploy misbehaves, rollback API to previous stable deploy.

After mobile `2.3.1` is live:

- do not delete the production Clerk app;
- do not remove old issuer support immediately;
- if migration has a bug, disable only the migration endpoint/path while preserving token validation.

## Manual checklist before starting

- [ ] Production extraction works again after migration `015` is applied.
- [ ] Production DB backup/snapshot is available.
- [ ] Current Clerk development app remains active.
- [ ] Production Clerk app exists.
- [ ] Email/password, Apple, and Google are configured in production Clerk.
- [ ] JWT template `recipe-extractor-public-metadata` exists in production Clerk.
- [ ] Production API env values are ready but not exposed in git/chat.
- [ ] `CLERK_SECRET_KEYS_BY_ISSUER` includes both old dev and new prod Clerk issuers during the transition.
- [ ] EAS production env is ready but not exposed in git/chat.
- [ ] Decide transition window length for old Clerk issuer support.
