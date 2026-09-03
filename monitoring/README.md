# TEXNIKACH manager monitoring

`/monitoring` is the single passwordless manager portal. Authentication uses
Telegram OpenID Connect and a numeric Telegram-ID allowlist. The browser gets
an opaque host-only server-side session. Downstream services never trust or
forward that cookie; they use separate service credentials.

## Required production configuration

```dotenv
MONITORING_ENABLED=true
MONITORING_BASE_URL=https://bot.texnikach.uz/monitoring
MONITORING_SESSION_DB_PATH=/app/data/monitoring_sessions.db
MONITORING_SESSION_TTL_SECONDS=43200
MONITORING_IDLE_TTL_SECONDS=7200
# Optional; when empty, DELIVERY_MANAGER_IDS is used.
MONITORING_MANAGER_IDS=202134293,7497097483
MONITORING_ADMIN_IDS=202134293

MONITORING_TELEGRAM_CLIENT_ID=
MONITORING_TELEGRAM_CLIENT_SECRET=
MONITORING_TELEGRAM_REDIRECT_URI=https://bot.texnikach.uz/monitoring/auth/callback

MONITORING_DELIVERY_BASE_URL=http://texnikach-delivery-stats:8080
MONITORING_DELIVERY_SERVICE_TOKEN=

MONITORING_GO_API_URL=
MONITORING_GO_API_TOKEN=
```

Register the origin and redirect URI in BotFather's Telegram Login settings.
Use only the `openid profile` scopes. The main web process must be a single
replica while the session store is SQLite; use PostgreSQL or Redis before
adding replicas.

The delivery token belongs only in the main web service and `delivery-stats`.
It must be explicitly blanked in the Telegram delivery-bot container.

The GO adapter contract is documented in `docs/monitoring-go-api.md`.

## Data sources and access

| Portal section | Source | Manager access |
| --- | --- | --- |
| Overview / calls | existing calls SQLite and reporting functions | read-only |
| Reviews | existing reviews SQLite and analytics functions | read-only |
| Delivery | delivery web service internal API | read-only |
| Prices | existing price repository | read-only; `/price` reuses the same session |
| GO site | OpenCart internal API | read-only; unavailable until that API is deployed |

`/dashboard` and `/admin/reviews` redirect to the matching portal section when
`MONITORING_ENABLED=true`. The old delivery pages also redirect after the
delivery service receives `MONITORING_BASE_URL`. Public `/rating` links and
webhooks are unchanged. Legacy Basic credentials stay available only as a
rollback path and must be removed after the migration is accepted.

Price mutations remain administrator-only. An authenticated manager opening
`/price` receives the catalogue without action controls; an administrator gets
the existing controls. Mutating requests require the portal CSRF token and an
exact same-origin request. If the portal cookie is absent, the legacy Basic
flow remains available during the rollback window.

## Manager identity registry

Cross-system aliases are explicit rather than inferred from display names:

```dotenv
MONITORING_MANAGER_REGISTRY_JSON=[{"canonical_id":"manager-olmas","name":"Olmas","telegram_id":202134293,"call_codes":["olmas"],"review_codes":["olmas"],"delivery_names":["Olmas"],"go_ids":["12"]}]
```

An empty registry is valid and is reported as `not_configured`; the portal
will not silently merge similarly named people.

If `MONITORING_MANAGER_IDS` is empty, access automatically uses the existing
numeric `DELIVERY_MANAGER_IDS` list. `MONITORING_ADMIN_IDS` never inherits a
fallback: administrator rights must always be granted explicitly.

## Rollout checklist

1. Create or choose the login bot in BotFather, register
   `https://bot.texnikach.uz` and the exact callback URL, and copy the OIDC
   Client ID/Secret into the main web-service secrets.
2. Add numeric employee IDs to the manager/admin allowlists. Do not use
   Telegram usernames as authorization identifiers.
3. Generate a separate random delivery service token and place the same value
   in the main web service and `delivery-stats`. Keep it blank in
   `delivery-bot` as enforced by `compose.delivery.yaml`.
4. Mount persistent storage for `MONITORING_SESSION_DB_PATH`, keep the main web
   service at one replica, and rate-limit `/monitoring/auth/start` at the edge.
5. Deploy the read-only OpenCart endpoint from `docs/monitoring-go-api.md`, then
   set its URL and dedicated token. Until then only the GO card is unavailable;
   the other sections continue to work.
6. Deploy with `MONITORING_ENABLED=false`, smoke-test health and the callback,
   then switch it to `true`. Verify manager, admin, removed-user, logout,
   mobile, and partial-source-outage scenarios.
7. After the acceptance window, rotate/delete old human Basic credentials and
   keep only service-to-service tokens.

Rollback is one switch: set `MONITORING_ENABLED=false` in the main service and
remove `MONITORING_BASE_URL` from delivery-stats. Existing Basic-protected
pages then keep their previous behavior. Do not delete the session database
during rollback; it contains the authorization audit trail.
