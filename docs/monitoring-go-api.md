# TEXNIKACH GO monitoring API contract

The manager portal must not submit or retain the password used by the legacy
`information/go/stats` HTML form.  The OpenCart application should expose a
separate read-only endpoint for server-to-server use:

```http
GET /index.php?route=api/monitoring/go/stats&period=today
Authorization: Bearer <MONITORING_GO_API_TOKEN>
Accept: application/json
```

Supported query parameters are `period=today|yesterday|7d|30d|custom`, plus
`date_from=YYYY-MM-DD` and `date_to=YYYY-MM-DD` for a custom period.  Dates use
the `Asia/Tashkent` business timezone. Unknown parameters must be rejected.

Successful response:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-03T18:30:00+05:00",
  "period": {
    "type": "today",
    "date_from": "2026-09-03",
    "date_to": "2026-09-03"
  },
  "metrics": {},
  "series": [],
  "breakdowns": {}
}
```

`metrics`, `series`, and `breakdowns` must be populated from the existing GO
Statistics business logic. Their source-specific fields should be documented
in the OpenCart repository; the Python portal intentionally does not invent
them.  Responses must not contain session IDs, customer identifiers, raw IP
addresses, passwords, tokens, or database errors.

Security requirements:

- HTTPS only;
- a dedicated random service token, never an employee password;
- constant-time token comparison;
- optional source-IP allowlist at Nginx/Cloudflare;
- `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and no CORS;
- strict date validation and a bounded response size;
- `401`/`403` for missing or invalid service authentication;
- `503` when service authentication is not configured (never fail open).

The Python portal configuration is:

```dotenv
MONITORING_GO_API_URL=https://texnikach.uz/index.php?route=api/monitoring/go/stats
MONITORING_GO_API_TOKEN=<dedicated-random-token>
```

Until this endpoint exists, `/monitoring/api/site` deliberately reports
`go_source_not_configured`; HTML scraping is not an accepted fallback.

