# Runbook: SLOFastBurn

> The gateway availability SLO error budget is being burned at 14.4x normal.
> At this rate, 30 days of budget will be exhausted in ~2 days.

## What this means

This is a CALL-IN-THE-NIGHT alert. It indicates either a high-impact bug or
an active denial-of-service. Treat as a production incident: page the
secondary, open an incident channel.

## First three things to check

1. **Error rate panel** on the service-RED dashboard — which status code is
   spiking? 500 = server bug, 503 = upstream out, 429 = client storm /
   attack.
2. **Recent deploys** in `deploy-services.yml`. Roll back if a deploy
   matches the start of the burn.
3. **Identity in the 5xx logs** — single user/IP repeatedly? If yes, this
   is an attack, not a bug. Rate-limit the offender at the ingress.

## Mitigation

If the burn doesn't stop within 30 min:

- Engage incident response per company runbook.
- Consider freezing further deploys to the affected service until resolved.
