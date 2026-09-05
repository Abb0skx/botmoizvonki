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

MONITORING_PRICE_BASE_URL=http://texnikach-price-web:8080
MONITORING_PRICE_SERVICE_TOKEN=
MONITORING_PRICE_ADMIN_SERVICE_TOKEN=
MONITORING_PRICE_INTERNAL_HOST=texnikach-price-web
# Explicit Telegram IDs allowed to publish/update the price catalogue.
MONITORING_PRICE_EDITOR_IDS=202134293

MONITORING_GO_API_URL=
MONITORING_GO_API_TOKEN=
```

Register the origin and redirect URI in BotFather's Telegram Login settings.
Use only the `openid profile` scopes. The main web process must be a single
replica while the session store is SQLite; use PostgreSQL or Redis before
adding replicas.

The delivery token belongs only in the main web service and `delivery-stats`.
The two price tokens belong only in the main web service and the standalone
price web service. The read token cannot perform publication actions; the
admin token is accepted only over the private Docker hostname. All service
tokens are separate random values. The delivery token must be explicitly
blanked in the Telegram delivery-bot container. If the dedicated price admin
credential is absent or invalid, the portal stays available and prices fall
back to read-only mode.

The GO adapter contract is documented in `docs/monitoring-go-api.md`.

## Data sources and access

| Portal section | Source | Manager access |
| --- | --- | --- |
| Overview / calls | existing calls SQLite and reporting functions | read-only |
| Reviews | existing reviews SQLite and analytics functions | read-only |
| Delivery | delivery web service internal API | read-only |
| Prices | standalone price service internal API | read-only by default; explicit price editors retain the original publication controls |
| GO site | OpenCart internal API | read-only; unavailable until that API is deployed |

`/dashboard` and `/admin/reviews` redirect to the matching portal section when
`MONITORING_ENABLED=true`. The old delivery pages also redirect after the
delivery service receives `MONITORING_BASE_URL`. Public `/rating` links and
webhooks are unchanged. Legacy Basic credentials stay available only as a
rollback path and must be removed after the migration is accepted.

The standalone `/price` page redirects to `/monitoring/prices` after its
`MONITORING_BASE_URL` is configured. Price mutations pass through a strict
portal allowlist, session and CSRF check, then use a dedicated write-only
service credential over the private Docker hostname. The browser never sees
that credential.

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
3. Generate separate random delivery and price service tokens. Put the
   delivery value in the main web service and `delivery-stats`; put the price
   value in the main web service and standalone price web service. Keep the
   delivery token blank in `delivery-bot` as enforced by
   `compose.delivery.yaml`.
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
