# Operational requirements

What the operational layer measures against. [`specs/operational/requirements.json`](../specs/operational/requirements.json)
holds every number with its source; this page explains it. The sources were read on 2026-11-25,
each at the version the file records.

`make check-requirements` checks the file: the operations are exactly those of the pinned specs,
each endpoint agrees with its frequency class, every block cites a declared source, and the
manual's worked examples hold with the rules as written. Each check must also notice a copy of
the file broken on purpose (22 of them).

## Sources

- **Manual de APIs 7.0** (IN BCB 615 of 2025-05-06), read in the Banco Central's PDF: frequency
  classes, response time, availability, limits, timeout. The portal listed it as in force on
  2026-10-02.
- **Manual de Monitoramento 3.0** (IN BCB 706 of 2026-01-29): what is monitored and the PCM
  reporting goals. Read in a third-party reproduction (LegisWeb), not in the original.
- **Portal do Open Finance**: the per-endpoint table (Referência) and the calculation pages 2.1.I
  to 2.1.III, 2.4 and the PCM's (Plataforma de Coleta de Métricas), with its rules per field.
- **The PCM's OpenAPI file**, at a fixed commit: the reports' format ([telemetry.md](telemetry.md)).

## Per endpoint

| Endpoint | Frequency | P95 (ms) | TPM per origin | Calls per month |
|---|---|---|---|---|
| `POST /consents`, `GET` and `DELETE /consents/{consentId}` | alta | 1500 | n/a | n/a |
| `POST .../extends`, `GET .../extensions` | baixa | 4000 | n/a | n/a |
| `GET /accounts`, `/accounts/{accountId}`, `.../transactions` | baixa | 4000 | 1000 | 8 |
| `GET .../balances`, `.../overdraft-limits` | alta | 1500 | 2500 or more | 420 |
| `GET .../transactions-current` | alta | 1500 | 2500 or more | 240 |

The limits are minimums: a transmitter may allow more, never less. The TPM of `alta` grows with
the active consents between the two institutions. The portal's table also has
`/reserved-balances`, which is not in the pinned Accounts 2.4.2.

## Rules

- **Response time**: P95 per endpoint, method and day, the value at index `round(0.95 n)` of the
  ascending order. 423, 429 and 529 are left out, as are `PAIRED_INCONSISTENT` and `DISCARDED`
  reports. The month is compliant when at least 90 % of the days with requests are within the
  SLA and the others are at most 20 % above it.
- **Availability**: valid requests are 2XX, 5XX, 408 and 422; 2XX and 422 are successes. A minute
  (Brasília time) is available at 95 % of successes or more, and undefined without valid requests.
  The day is available / (available + unavailable) and must reach 95 %. The 90-day moving average
  must reach 99.5 % on the month's last day.
- **Overload**: 529 below 0.5 % of the valid requests on at least 90 % of the days, and at most
  5 % on the others. The infrastructure must take 300 TPS.
- **Limits**: 429 above the traffic limit, 423 above the monthly one. Only 2XX count for the
  monthly limit, and the extra requests of a pagination do not.
- **Timeout**: 15 seconds. The server answers 504; a client that gives up reports 408.
- **PCM reporting**: 95 % of a day's reports by 08:00 of the next day and 99 % within 7 days. Up to
  three days a month may miss the first goal if they meet the second.

The status code and the time that count are the client's; the server's are used when the
client's report is missing.

## Where the sources disagree

Each case is in the file with the reading chosen.

| Case | Disagreement | Chosen |
|---|---|---|
| `consents-renewal-frequency` | The manual makes all Consents endpoints `alta`; the portal lists `extends` and `extensions` as `baixa` | The portal, given per endpoint |
| `p95-boundary` | At most the SLA (manual), or below it (its example and the portal) | At most |
| `daily-availability-denominator` | Available + unavailable minutes (manual), or the day's minutes (portal) | The manual |
| `availability-method` | The portal's 2024 page polls `GET /discovery/status` | The manual: real requests |
| `process-timespan-end` | First byte (field definition), or last byte (filling rule and manual) | Last byte |
| `client-408-counting` | The PCM page counts a client's 408 at 15000 ms or more, then below it | 15000 ms or more |
| `overload-in-availability` | 529 is 5XX, so literally an error in availability, but out of the response time | Literal |
| `response-time-day` | The time zone of the day is stated for availability only | Brasília for both |
| `pcm-spec-domains` | The PCM's OpenAPI file and the portal give other values for four `additionalInfo` fields | The portal ([telemetry.md](telemetry.md)) |
| `process-timespan-integer` | Whole milliseconds by definition, `120.000001` as example | Whole, rounded up |

The manual cuts its percentages after two decimals: 255/259 is 98.4556 %, published as 98.45 %.
That only matters when comparing with published figures; the rules use the exact values.
