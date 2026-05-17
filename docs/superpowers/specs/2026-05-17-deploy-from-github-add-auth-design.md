# Deploy mealie-mcp from GitHub & add bearer auth

**Date:** 2026-05-17
**Status:** Approved

## Context

Two parallel GitHub repos hold overlapping content:

- `mhempstock/mealie-mcp` — source + GHA that publishes `ghcr.io/mhempstock/mealie-mcp:latest`.
- `mhempstock/mealie` — a Helm chart (mealie + postgres + mcp), a Woodpecker pipeline, and a duplicate copy of the mcp source. The pipeline builds an image inside the cluster (buildkit → `localhost:30500/mealie-mcp:<sha>`) and `helm upgrade`s.

The k8s deployment in namespace `mealie` runs `localhost:30500/mealie-mcp:80347639` (commit `8034763`, deployed 16 Apr 2026). `diff -r` confirms the two source trees are byte-identical — no local changes have been lost. The MCP server is currently exposed unauthenticated at `https://mealie-mcp.hempstock.it` via a cloudflare-tunnel ingress.

## Goal

1. Make GitHub (`mhempstock/mealie-mcp`) the single source of truth for code.
2. Have the cluster pull the prebuilt GHCR image.
3. Add bearer-token auth so Claude.ai can connect securely.
4. Switch transport from deprecated SSE to streamable-HTTP.

## Design

### Code: `mhempstock/mealie-mcp`

Add native FastMCP bearer auth in `src/mealie_mcp/server.py`:

- Implement `StaticTokenVerifier(TokenVerifier)` that uses `secrets.compare_digest` against `MCP_AUTH_TOKEN`.
- On startup, if `MCP_AUTH_TOKEN` is set, construct `FastMCP` with:
  - `token_verifier=StaticTokenVerifier(token)`
  - `auth=AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=["mealie"])`
    where the URLs come from `MCP_PUBLIC_URL` env (single var, used for both fields).
- If `MCP_AUTH_TOKEN` is unset (e.g., stdio dev mode), construct `FastMCP` without auth — preserves existing local-dev/Docker-compose behavior.

No new dependencies — `TokenVerifier`, `AccessToken`, `AuthSettings` are in `mcp` 1.27 already.

Transport defaults stay env-driven (`MCP_TRANSPORT`), so `streamable-http` is set in the helm chart, not hard-coded.

### Chart: `mhempstock/mealie`

In `helm/mealie/`:

- `values.yaml`: change `mcp.image.repository` from `localhost:30500/mealie-mcp` → `ghcr.io/mhempstock/mealie-mcp`, set `mcp.image.tag: latest`, add `mcp.image.pullPolicy: Always`. Add `mcp.publicUrl: https://mealie-mcp.hempstock.it`.
- `templates/mcp.yaml`:
  - Set `imagePullPolicy: {{ .Values.mcp.image.pullPolicy }}`.
  - Change `MCP_TRANSPORT` from `sse` → `streamable-http`.
  - Add env: `MCP_AUTH_TOKEN` from secret `mealie-secrets` key `MCP_AUTH_TOKEN`.
  - Add env: `MCP_PUBLIC_URL` from `.Values.mcp.publicUrl`.

Delete the duplicate top-level files in the `mealie` repo: `src/`, `Dockerfile`, `pyproject.toml`, `.env.example`, `.gitignore` (chart-only repo from here on).

### CI

In `mhempstock/mealie/.woodpecker.yaml`:

- Drop the `build-mcp` step entirely.
- Keep the `deploy` step (helm upgrade) so chart changes redeploy on push.
- The deploy step no longer needs `--set mcp.image.tag=...` — the chart's default tag (`latest`) is used.

`mhempstock/mealie-mcp`'s GHA already builds and publishes `:latest` on push to main — unchanged. To roll a new code release: push to `mealie-mcp` → GHA publishes new `:latest` → `kubectl -n mealie rollout restart deploy/mealie-mcp` (Always pull picks up new image).

### Secret

A new key `MCP_AUTH_TOKEN` is added to the existing `mealie-secrets` k8s secret. Token is a 32-byte URL-safe string generated locally via `secrets.token_urlsafe(32)`. Token value is provided to the user out-of-band; the Claude.ai custom connector is configured with the same token as bearer auth.

### Claude connector

- URL: `https://mealie-mcp.hempstock.it/mcp`
- Auth: bearer token = generated string

## Out of scope

- OAuth/OIDC integration (Google, etc.) — not requested.
- Token rotation tooling — single-token homelab use case.
- Multi-user scopes — single `["mealie"]` scope is sufficient.
- Moving the helm chart into `mealie-mcp` repo — explicitly rejected during brainstorming.

## Verification

1. After deploy, `curl https://mealie-mcp.hempstock.it/mcp` (no auth) returns 401 with `WWW-Authenticate` header.
2. `curl -H "Authorization: Bearer <token>" https://mealie-mcp.hempstock.it/mcp` returns a valid streamable-HTTP response (initialize request via SDK or 405/406 with valid Accept headers).
3. Claude.ai custom connector successfully lists Mealie tools and runs `search_recipes`.
