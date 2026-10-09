# hubspot-audit

Read-only HubSpot portal audit extractor for Process Pro Consulting. It pulls a prospect's portal with a read-only Service Key or by installing our public app (OAuth), computes aggregate metrics locally, and writes an audit bundle that Claude turns into the internal audit report.

Status: v0.1.0. Built and unit-tested against a fake portal. **Not yet run against a live HubSpot portal**; see [Verification status](#verification-status).

## How it works

1. The prospect creates a read-only Service Key and fills in the screenshot checklist (`prospect/`).
2. `hubspot-audit run` probes the key, pulls ten categories, and writes `output/<prospect>_<date>/`.
3. Claude reads `audit_bundle.json` plus the intake and checklist using `prompts/bundle_audit_prompt.md` and writes the report.

Claude sees aggregates (for example, "Job Title populated on 23% of sampled contacts"), not names or emails. Contact records are held in memory only.

## Install

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Run it on a local machine or in Claude Code. The claude.ai sandbox cannot reach `api.hubapi.com`.

## Configure

Copy `.env.example` to `.env` (gitignored) and fill it in.

| Variable | Purpose |
| --- | --- |
| `HUBSPOT_SERVICE_KEY` | Read-only Service Key. Read only from the environment; never written or logged. |
| `PROSPECT_NAME` | Used in the output folder name and the bundle. |
| `ERROR_ROUTER_URL` | Central Error Router webhook. **Required**; the run refuses to start without it. |
| `FLOW_ID`, `FLOW_NAME` | Identify this tool in error payloads. |
| `PLATFORM_NAME` | Must be `LOCAL_SCRIPT` for this tool. |

## Run

```bash
hubspot-audit run                          # all 10 categories
hubspot-audit run --categories 1,3         # a subset
hubspot-audit run --sample-size 500 --output-dir output --prospect "Acme Co"
```

Contact-level metrics use a sample (default 1,000 most recently created contacts). Counts use search totals and cover the whole database.

## Output

`output/<prospect-slug>_<YYYY-MM-DD>/` (gitignored):

| File | Contents |
| --- | --- |
| `audit_bundle.json` | All metrics, validated against `schema/audit_bundle.schema.json` |
| `property_list.csv` | Property definitions (no record data) |
| `workflow_list.csv` | Workflow inventory |
| `open_deals.csv` | Open deals: deal name, owner, stage, amount, dates (no contact data) |
| `form_list.csv`, `list_inventory.csv`, `marketing_emails_90d.csv` | Forms, lists, recent emails |

Every metric has `key`, `value`, `unit`, `source`, `status` (`ok`, `empty`, `forbidden`, `unsupported`, `error`) and an optional `note`. A non-`ok` status means the value is `null`: a gap, not a zero. Gaps flow into the report's Open Questions. CSV cells are scrubbed of email-like strings (the count is in `meta.csv_email_redactions`).

## OAuth (public app) mode

`hubspot-audit run --auth oauth` authenticates through the Process Pro public app instead of a Service Key. The prospect's admin installs the app and approves the read scopes in `src/hubspot_audit/scopes.py`; send them `prospect/install_instructions_oauth.md`.

Setup, once, in the HubSpot developer account: create the app, set the redirect URI to match `HUBSPOT_REDIRECT_URI`, declare the scopes (required and optional), and put the client ID and secret in `.env`.

| Mode | When | How |
| --- | --- | --- |
| Interactive install | The approver is on a call with the person running the audit | `run --auth oauth` prints the install URL and listens on `http://localhost:8765/callback` for one redirect |
| Existing token | A hosted receiver already holds the token | Set `HUBSPOT_REFRESH_TOKEN`; no install step |
| URL only | Send the link ahead of time | `hubspot-audit install-url` |

Tokens are kept in memory for the run and never written to disk. In this mode the bundle's `meta.oauth_scopes_granted` lists the scopes HubSpot actually granted (more reliable than the probe-based `scopes_granted`).

### Hosted receiver (Netlify)

A prospect can install the app on their own: the Netlify receiver in `netlify/` catches HubSpot's redirect, exchanges the code, encrypts the refresh token to our public key and stores only the ciphertext in Netlify Blobs. `hubspot-audit run --auth oauth --state <state>` then fetches it once, decrypts it locally and deletes it from Netlify.

```
prospect admin --install link--> HubSpot --redirect--> Netlify /oauth/callback
                                                         | exchange code, encrypt, store (Blobs)
CLI: run --auth oauth --state S --> Netlify /api/token --> ciphertext (deleted on read, 24h expiry)
                                    decrypt with local private key, hold in memory for the run
```

Setup, once:

1. **Keys.** `hubspot-audit keygen` writes the private key to `~/.hubspot-audit/token_private.pem` (mode 600, never overwritten) and prints the public key. Back the private key up: without it stored tokens cannot be read.
2. **Secrets.** Generate two random values, for example `openssl rand -hex 32`: `STATE_SIGNING_SECRET` and `TOKEN_FETCH_SECRET`.
3. **Netlify site.** Create a site from this repo (`netlify.toml` is at the root). Set these environment variables: `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` (`https://<site>/oauth/callback`), `STATE_SIGNING_SECRET`, `TOKEN_FETCH_SECRET`, `TOKEN_PUBLIC_KEY_PEM` (the printed public key), `ERROR_ROUTER_URL`, `FLOW_ID`, `FLOW_NAME`, `PLATFORM_NAME=OTHER`. Mark the secrets as secret in Netlify.
4. **HubSpot app.** Register `https://<site>/oauth/callback` as the redirect URI.
5. **Local `.env`.** Set `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET`, `HUBSPOT_REDIRECT_URI` (same as `OAUTH_REDIRECT_URI`), `TOKEN_RECEIVER_URL` (`https://<site>`), `STATE_SIGNING_SECRET` and `TOKEN_FETCH_SECRET`.

Per prospect:

1. `hubspot-audit install-url` prints a signed install link and a state value. Send the link with `prospect/install_instructions_oauth.md`.
2. When the prospect confirms, `hubspot-audit run --auth oauth --state <state>`.
3. If the run fails after the token is fetched, the token is already deleted from Netlify; the prospect must reinstall (about 5 minutes).

Security properties: the state is HMAC-signed so the receiver rejects installs we did not create; Netlify holds only RSA-OAEP ciphertext, readable only with our private key; the fetch endpoint needs a bearer secret, reads once, and expires entries after 24 hours; no codes, tokens or HubSpot response bodies are logged or sent to the error router. During the callback the function briefly holds the plaintext token and client secret in memory. The prospect's access ends when they uninstall the app.

Local tests: `cd netlify && npm install && npm test`. The Python suite also checks that Node-encrypted tokens decrypt in Python and that both sides compute the same state signature.

## Error handling

Per-endpoint failures (a 403, an unsupported endpoint) are recorded in the bundle and the run continues. Failures that make the bundle untrustworthy (rejected key, schema validation failure, a Service Key found in output) go to the global handler in `cli.py`, which posts the Unified Error Payload to the Central Error Router (5 s timeout, one retry on network error or 5xx, never on 4xx) and re-raises.

## Test and lint

```bash
pytest
ruff check src tests && black --check src tests && isort --check-only src tests
pip-audit
```

## Known API gaps

Items with uncertain coverage are built, and fall back to the screenshot checklist if they fail. Status of each is tracked below and must be filled in from the live test.

| Item | Endpoint | Expected behavior if unavailable |
| --- | --- | --- |
| Account/portal ID | `/account-info/v3/details` | `meta.portal_id` is null |
| Role names | `/settings/v3/users/roles` (Enterprise) | Role ids shown instead of names |
| Form submission counts | `/form-integrations/v1/submissions/forms/{id}` (legacy) | Counts null in `form_list.csv` |
| Email stats shape | `/marketing/v3/emails?includeStats=true` | Rates null or zero emails counted |
| Sequences | `/automation/v4/sequences` (beta) | `sequences.inventory` is `unsupported`; use checklist |
| Workflow enrollments, errors, owners | not in `/automation/v4/flows` list | Checklist |
| Required fields per stage | not read | Checklist |
| Contacts with no activity | `notes_last_updated` NOT_HAS_PROPERTY | Verify count against UI |
| Renewals | proxy: open "Existing Business" deals closing in 60 days | Manual check for portals modelling renewals differently |
| Engagement counts | calls/meetings/emails search by owner | `forbidden` if scope absent |

Not available from the API at all: user last login, sandbox, dashboards and reports, connected apps, sync errors, ad accounts, consent settings.

Beta endpoints: `/automation/v4/flows`, `/automation/v4/sequences`.

## Verification status (OAuth)

OAuth flow and the Netlify receiver are tested against fakes only (refresh, 401 retry, state check, encryption round trip between Node and Python, no secret in errors). The function has not been deployed; the Netlify Blobs calls (`getStore`, `getWithMetadata`, `set`, `delete`) are written from memory of the API and unconfirmed against a live site. Unconfirmed against HubSpot: whether `http://localhost` is accepted as a redirect URI, how required versus optional scopes are declared in the current developer platform, who can approve the install, any install cap before marketplace listing, and that `/oauth/v1/access-tokens/{token}` is still the introspection endpoint.

## Verification status

Unit and integration tests pass against a fake portal (scopes denied, retries, pagination, schema, PII scrub, error router). The following still need a live portal and are **not done**:

- [ ] Run against a portal we control with all 10 categories
- [ ] Confirm scope names in the Service Key picker (`prospect/scopes.md`)
- [ ] Spot check at least 10 numbers against the HubSpot UI (table below)
- [ ] Remove one scope on the key and confirm a `forbidden` entry and a completed run
- [ ] Test the bundle through `prompts/bundle_audit_prompt.md` and review the report

### Spot-check table (fill in from the live run)

| Metric | Bundle key | UI value | Bundle value | Match |
| --- | --- | --- | --- | --- |
| Contact property count | `properties.contacts.total` | | | |
| Company property count | `properties.companies.total` | | | |
| Deal property count | `properties.deals.total` | | | |
| Open deals | `deals.open.total` | | | |
| Contacts with no owner | `contacts.no_owner` | | | |
| Total contacts | `contacts.total` | | | |
| Workflow count | `workflows.total` | | | |
| List count | `lists.total` | | | |
| Unsubscribed contacts | `contacts.unsubscribed` | | | |
| User count | `users.total` | | | |
| Super admins | `users.super_admin_count` | | | |
| Pipeline count | `pipelines.deals.count` | | | |

## Repository layout

```
netlify/             OAuth receiver (Netlify Functions, JavaScript) and its tests
src/hubspot_audit/   client.py, auth.py, receiver.py, metrics.py, context.py, bundle.py, cli.py, categories/
schema/              audit_bundle.schema.json
prompts/             live_audit_prompt.md (reference), bundle_audit_prompt.md
prospect/            setup_instructions.md, scopes.md, screenshot_checklist.md
tests/
```
